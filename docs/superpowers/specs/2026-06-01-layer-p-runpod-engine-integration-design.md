# Layer P — RunPod engine integration (ComfyUI + Wan i2v)

**Status:** validated 2026-06-01, awaiting plan
**Branch:** `build/layer-p` off `main@7788f93`
**Closes:** PROGRESS Layer-O carry-forward #1 ("Engine-integration live smoke — ComfyUI/Diffusers/Hosted deployed on a real RunPod pod producing a real MP4")
**Defers:** Diffusers / Hosted engine integration on real RunPod, batch CLI live smoke, serverless mode (each a separate future layer)

## Why this layer

Layer N (Phase 24) closed the first real-cloud verification gap by validating `RunPodProvider.find_offers` + the pod lifecycle (`create_instance` → poll ready → `destroy_instance`) against the real RunPod GraphQL API. It deliberately pivoted to a bare pod smoke (~$0.001/run) because the original architecture couldn't capture engine HTTP traffic across a subprocess boundary — engine integration was punted to a follow-up layer.

Layer O (Phase 25) shipped the user-facing output directory but explicitly deferred engine integration as "Layer P candidate."

Layer P is that follow-up. It produces the first real MP4 from kinoforge end-to-end on real cloud compute: ComfyUI + Wan 2.2 i2v on RunPod, driven through `orchestrator.generate()`, published under `output/` via the Layer O sink, validated by a single in-process live test.

The same in-process recording-seam pattern Layer N proved (Layer N caught 10 production bugs against fixtures the offline tests had silently passed against) is extended to wrap ComfyUI's HTTP traffic. Layer P inherits the cost-safety quadruple-lock from Layer N and adds a warm-pod reuse semantic (`KINOFORGE_LIVE_KEEP_POD=1` + tag-based discovery) so the iteration-shake-out loop stays cheap even when provisioning errors take multiple attempts to surface.

## Architecture

Eight new/modified artifacts on `build/layer-p` from `main@7788f93`:

**New files:**
- `examples/configs/runpod-comfyui-wan.graph.json` — hand-authored Wan 2.2 i2v ComfyUI workflow (~10–20 nodes).
- `tests/engines/fixtures/comfyui/prompt_submit.json` — captured `POST /prompt` response.
- `tests/engines/fixtures/comfyui/history_done.json` — captured `GET /history/{id}` response (terminal poll wins).
- `tests/engines/fixtures/comfyui/view.json` — captured `GET /view?...` response metadata (not the MP4 bytes).
- `tests/live/test_comfyui_wan_live.py` — single-function live E2E smoke, phase-marker structured.

**Modified files:**
- `src/kinoforge/core/config.py` — `spec.graph_file` loader convention (relative-to-YAML-parent or absolute path; inline file content into `cfg.spec.graph` at load time; mutually exclusive with `spec.graph`).
- `src/kinoforge/engines/comfyui/__init__.py` — verify (and extend if absent) `ComfyUIEngineConfig.nodes[*].ref` field for git SHA pinning; `provision` runs `git checkout <ref>` when set.
- `src/kinoforge/providers/runpod/__init__.py` — add `find_instance_by_tag(key, value) -> Instance | None` helper for warm-pod reuse.
- `tests/providers/conftest_runpod.py` — extend `_RecordingHTTPSeam` to support a second instance wrapping ComfyUI HTTP traffic via per-endpoint dispatch table.
- `tests/engines/test_comfyui.py` — 23 existing tests refactored to load via `_load_comfy_fixture("<name>.json")`. New shape-lockdown tests added.
- `examples/configs/runpod-comfyui-wan.yaml` — fill `spec.graph_file`, `engine.comfyui.nodes` (with SHA pins), refine `models:` to the real Wan 2.2 i2v file set.
- `README.md` — extend "Real providers — RunPod" with engine-integration sub-section (env vars, quickstart, KEEP_POD dev loop, cost shape).
- `PROGRESS.md` — Phase 26 entry; close Layer-O carry-forward #1.

**Invariants preserved:**
- Core never imports a concrete adapter (the `spec.graph_file` loader reads `Path` only; no engine awareness).
- Recording HTTP seam stays injectable (smoke owns both seam instances directly; production code path unchanged).
- Cost guards quadruple-locked (Layer N parity: max_cost_rate, budget, finally-destroy, idle selfterm).
- Offline tests pass with no live creds; live test gates via `pytest.skip(..., allow_module_level=True)`.

## `spec.graph_file` loader convention

YAML sugar that inlines a graph JSON file into `cfg.spec.graph` at config load. Engine-agnostic — any engine that reads `cfg.spec.graph` benefits; ComfyUI is the first consumer. Lives in `core/config.py`, mirroring Layer L's `prompt_file` precedent in `batch.py`.

**YAML surface:**

```yaml
spec:
  graph_file: runpod-comfyui-wan.graph.json   # relative to YAML parent dir
  # OR
  graph_file: /abs/path/to/graph.json
  # OR (existing, no change)
  graph:
    nodes: {...}
```

**Resolution rules:**
- `spec.graph_file` and `spec.graph` are mutually exclusive — both set raises `ValidationError`.
- Relative `graph_file` resolves against the YAML file's parent directory (not cwd).
- Absolute `graph_file` is used verbatim.
- File contents are JSON-parsed and assigned to `cfg.spec.graph`. The `graph_file` key is dropped from `cfg.spec` post-load — the engine sees a normal `graph` dict.
- File-not-found or JSON-parse error raises `ValidationError` with the file path + underlying cause.

**Tests** (3 new in `tests/core/test_config.py`):
- Round-trip: YAML with `graph_file` → loaded `cfg.spec.graph` matches the JSON contents.
- Both `graph_file` and `graph` set raises `ValidationError`.
- Relative path resolves against YAML parent dir, not cwd.

## ComfyUI custom-node SHA pinning

Verify current `ComfyUIEngineConfig.nodes[*]` shape. If `ref` field absent, extend. No behavior change for existing configs that omit `ref`.

**Expected YAML surface:**

```yaml
engine:
  kind: comfyui
  comfyui:
    version: "0.3.10"
    nodes:
      - repo: "https://github.com/kijai/ComfyUI-WanVideoWrapper"
        ref: "abc1234..."        # commit SHA, pinned at first live run
      - repo: "https://github.com/kijai/ComfyUI-KJNodes"
        ref: "def5678..."
```

**Provision behavior:**
- `git clone <repo>` into ComfyUI's `custom_nodes/<dirname>`.
- `git checkout <ref>` if `ref` set; absent `ref` falls back to clone-HEAD (back-compat).
- If `requirements.txt` exists in the cloned dir, install via `pip install -r` against ComfyUI's venv (existing behavior).

**Plan task:** SHAs are placeholders in YAML at branch start. After first successful live provision, capture real SHAs via a one-shot diagnostic command (`git rev-parse HEAD` per node dir, exposed by the smoke), commit them.

**Tests** (2 new in `tests/engines/test_comfyui.py`):
- `ref` field present → `provision` calls `run_cmd(["git", "checkout", ref])` after clone.
- `ref` absent → no checkout call (back-compat).

## Tag-discovery helper

Enables warm-pod reuse semantics. The smoke calls this helper at start; if a previously-tagged ready pod exists, it's reused instead of creating a fresh one.

**New method on `RunPodProvider`:**

```python
def find_instance_by_tag(self, key: str, value: str) -> Instance | None:
    """Return first 'ready' instance whose tag dict contains key=value, else None.

    Args:
        key: Tag dict key to match (e.g. "kinoforge.layer").
        value: Required value at that key (e.g. "layer-p-smoke").

    Returns:
        Newest matching Instance in status="ready", or None.
    """
```

**Implementation:**
- Calls existing `list_instances()`.
- Filters: `inst.status == "ready"` AND `inst.tags.get(key) == value`.
- Multiple matches (defensive) → return newest by `created_at`.
- Logs DEBUG `"tag-reuse: found {pod_id} (gpu={gpu_type}, age={delta}s)"` on hit; nothing on miss.

**Tag scheme used by Layer P smoke:**

```python
spec = InstanceSpec(
    image="...",
    offer=offers[0],
    lifecycle=Lifecycle(idle_timeout_s=600),
    tags={
        "mode": "pod",                     # Layer N back-compat
        "kinoforge.layer": "layer-p-smoke",
        "kinoforge.git_sha": git_sha,      # forensic/debugging
    },
)
```

**Why namespaced `kinoforge.*`:** future layers can carve out their own tag namespaces without collision. `mode` stays for Layer N back-compat.

**Tests** (3 new in `tests/providers/test_runpod.py`):
- List returns matching ready pod → method returns it.
- List returns matching pod in `starting` (not-ready) status → returns `None`.
- List returns no match → returns `None`.

The smoke uses this helper only for dev iteration. Production code paths don't call it.

## ComfyUI HTTP recording seam extension

Layer N's `_RecordingHTTPSeam` in `tests/providers/conftest_runpod.py` wraps RunPod GraphQL only. Layer P extends it to wrap ComfyUI's `http_post` / `http_get` injected on `ComfyUIBackend`.

**Two-instance shape (Q2=B + Q6=A locked in brainstorming):**

```python
class _RecordingHTTPSeam:
    def __init__(
        self,
        post: HttpPostJsonFn,
        get: HttpGetJsonFn,
        fixtures_dir: Path,
        *,
        dispatch: dict[str, str],   # url-substring → fixture filename
    ) -> None: ...
```

Smoke constructs two seams:

```python
runpod_seam = _RecordingHTTPSeam(
    authed_post, authed_get,
    Path("tests/providers/fixtures/runpod"),
    dispatch=_RUNPOD_DISPATCH,    # existing Layer N table
)
comfy_seam = _RecordingHTTPSeam(
    plain_post, plain_get,
    Path("tests/engines/fixtures/comfyui"),
    dispatch=_COMFY_DISPATCH,
)
```

**ComfyUI dispatch table:**

| URL fragment match | Fixture filename |
|---|---|
| `POST .*/prompt$` | `prompt_submit.json` |
| `GET .*/history/[^/]+$` | `history_done.json` (last poll wins; earlier polls overwritten) |
| `GET .*/view\?` | `view.json` |

**No-match policy:** unknown URL → recorder logs WARNING + writes `unknown_<sha>.json` (Layer N parity).

**Provider/engine wiring in the smoke:**

```python
provider = RunPodProvider(creds=creds, http_post=runpod_seam.http_post, http_get=runpod_seam.http_get)
engine = ComfyUIEngine(http_post=comfy_seam.http_post, http_get=comfy_seam.http_get)
```

`ComfyUIEngine` is instantiated directly by the smoke (bypassing the zero-arg registry factory). Production code path (CLI → registry → zero-arg factory) is untouched. Layer N established the precedent of smoke-owned instances when HTTP seams need injection.

**Fixture file shape** (Layer N parity):

```json
{
  "_meta": {
    "captured_at": "2026-06-01T..-07:00",
    "git_sha": "<sha at capture>",
    "operation": "prompt_submit",
    "request_url": "http://...:8188/prompt",
    "request_body": { "prompt": {...}, "client_id": "..." }
  },
  "response": { "prompt_id": "...", "number": 1, "node_errors": {} }
}
```

**Redaction policy:** same regex `r"(?i)(token|key|secret|password)"` → `<REDACTED>` (Layer N's scrub function reused). Applied over both `_meta.request_body` and `response`.

**Tests** (2 new in extended conftest test or new `tests/providers/test_recording_seam.py`):
- ComfyUI `POST /prompt` writes `prompt_submit.json` with request body in `_meta`.
- ComfyUI `GET /history/{id}` polled 3 times → last write wins in `history_done.json`.

## Live smoke control flow

Single test function: `test_runpod_comfyui_wan_live_e2e_smoke` in `tests/live/test_comfyui_wan_live.py`. In-process. Phase-marker structured (`_log.info("[phase=...]", ...)` between blocks) so iteration debug reads cleanly. Mirrors `test_runpod_live.py` skeleton.

**Module-level gates:**

```python
if not (
    os.getenv("KINOFORGE_LIVE_TESTS") == "1"
    and os.getenv("RUNPOD_API_KEY")
    and os.getenv("RUNPOD_TERMINATE_KEY")
    and os.getenv("HF_TOKEN")
):
    pytest.skip("...", allow_module_level=True)
```

**Constants:**

```python
_TAG_KEY = "kinoforge.layer"
_TAG_VALUE = "layer-p-smoke"
_READY_TIMEOUT_S = 600    # 10 min for cold start (pod creation + image pull)
_GEN_TIMEOUT_S = 900      # 15 min for download + provision + generate
_POLL_INTERVAL_S = 10
```

**Phases:**

1. `[phase=setup]` — load YAML, build `_RecordingHTTPSeam` pair if `KINOFORGE_SAVE_FIXTURES=1`, read `keep_pod = os.getenv("KINOFORGE_LIVE_KEEP_POD") == "1"`.
2. `[phase=reuse_check]` — `existing = provider.find_instance_by_tag(_TAG_KEY, _TAG_VALUE)`; if non-None, `pod_id = existing.id`, `warm = True`, log reuse + cost-saved estimate. Else `warm = False`.
3. `[phase=find_offers]` (skipped when warm) — `provider.find_offers(reqs)`; assert non-empty; assert every offer ≤ cost cap; pick `offers[0]`.
4. `[phase=create_instance]` (skipped when warm) — `provider.create_instance(spec)` with Layer P tags; `pod_id = instance.id`.
5. `[phase=poll_ready]` (skipped when warm) — loop until `status=="ready"` or `_READY_TIMEOUT_S`.
6. `[phase=provision]` — construct `ComfyUIEngine` with comfy seams; `provisioner.provision(instance, cfg)` downloads weights, installs nodes, launches ComfyUI. Skipped automatically by `provision_state.py` marker when warm. Ping `/system_stats` via engine's `http_get` to confirm reachable.
7. `[phase=generate]` — build `GenerationRequest(prompt="...", assets=[Asset(role="init_image", path=...PNG)])`; `orchestrator.generate(cfg, request, provider=provider, engine_factory=lambda *_: engine)`; assert artifact file exists, `100 KB ≤ size ≤ 50 MB`, MP4 ftyp magic-bytes match, capability_key persisted in profile cache JSON, published path under `output/` exists (Layer O sink).
8. `[phase=destroy]` — `if keep_pod`: log `"*** POD %s KEPT (KINOFORGE_LIVE_KEEP_POD=1) ***"` + skip destroy. Else: `provider.destroy_instance(pod_id)`.
9. `[phase=cleanup_finally]` (always runs) — flush both seams; if `pod_id` set and `not keep_pod`, last-resort destroy with copy-pasteable `curl` block on failure (Layer N pattern).
10. `[phase=record]` (only on green) — write `tests/engines/fixtures/comfyui/last_smoke.json` with `pod_id`, `gpu_type`, `cost_rate`, `elapsed_seconds`, `artifact_sha`, `artifact_size`, `capability_key`.

**Cost-guard quadruple lock (Layer N parity):**

- a) `max_cost_rate_usd_per_hr=0.50` in YAML → `find_offers` filter upstream of `create_instance`.
- b) `budget=2.00` in YAML → `BudgetTracker.enforce` tears down mid-run if estimated spend crosses cap.
- c) `finally:` always destroys when `KEEP_POD` unset.
- d) `idle_timeout=10m` + selfterm script → pod self-destructs 10 min after last heartbeat even if the test process is killed mid-run.

**Warm-pod adjustment:** when `warm=True`, phases 3/4/5 are skipped, phase 6's `provision()` short-circuits via existing Layer I `provision_state.py` marker, iteration cost ≈ generate-only.

**Asset routing for i2v:** `tests/providers/fixtures/runpod/sample_init_frame.png` (committed in Layer N) is reused as the init image. `Asset(role="init_image", path=...)` is uploaded via `ComfyUIBackend`'s Layer F asset-wiring code (multipart `POST /upload/image`), the returned filename is plugged into the graph's `LoadImage` node via `asset_node_ids`.

## Wan i2v graph + model set

**Graph file:** `examples/configs/runpod-comfyui-wan.graph.json` — hand-authored, sourced from the Kijai `ComfyUI-WanVideoWrapper` reference workflow (de-facto Wan 2.x i2v community workflow). 10–20 nodes typical:

```
LoadImage → WanVideoModelLoader → WanVideoVAELoader → WanVideoTextEncode (pos+neg)
                                       ↓
                            WanVideoImageToVideoEncode (init frame conditioning)
                                       ↓
                                   WanVideoSampler
                                       ↓
                                WanVideoDecode → VideoCombine (MP4 output)
```

Specific node class names + topology determined at first live run against deployed ComfyUI (export workflow API JSON from ComfyUI desktop with the chosen Wan version, commit).

**`asset_node_ids` and `prompt_node_ids` (Layer F + Layer J wiring, already shipped):**

```yaml
spec:
  graph_file: runpod-comfyui-wan.graph.json
  asset_node_ids:
    init_image: "12"      # node id of the LoadImage node
  prompt_node_ids:
    - "8"                 # WanVideoTextEncode positive-prompt node
  node_overrides: {}      # smoke leaves empty; engine merges per-job
```

**Model set:** Wan 2.2 i2v on ComfyUI typically needs 3–4 files (UNet, VAE, text encoder, optional CLIP vision). The current YAML lists 1 file as scaffolding. The plan task fills the real set after first live provision surfaces "model not found" errors from ComfyUI:

```yaml
models:
  - ref: "hf:Wan-AI/Wan2.2-I2V-A14B:<actual-unet-file>.safetensors"
    kind: base
    target: checkpoints
  - ref: "hf:Wan-AI/Wan2.2-I2V-A14B:<vae-file>.safetensors"
    kind: vae
    target: vae
  - ref: "hf:Wan-AI/Wan2.2-I2V-A14B:<text-encoder-file>.safetensors"
    kind: text_encoder
    target: clip
```

`HuggingFaceSource.handles()` resolves `hf:repo:path` to a single file URL per entry — no new source logic required.

**Sharded-weight concern:** if the Wan 2.2 14B UNet ships as multi-part safetensors (e.g. `model-00001-of-00003.safetensors`), each shard becomes its own model entry. YAML grows but no new code is needed. Plan task flags this risk; live run decides.

**Smoke generation params** (pinned for fastest signal):

```yaml
params:
  fps: 16
  num_frames: 81      # ~5 seconds at 16 fps
  steps: 20
  width: 480
  height: 480
```

## Offline ComfyUIBackend test refactor

Mirrors Layer N's RunPod refactor (Phase 24). 23 existing `tests/engines/test_comfyui.py` tests currently build hand-crafted response dicts inline. After Layer P's live capture: dicts come from JSON.

**Shared helper** (new or extension of `tests/engines/conftest.py`):

```python
import json
from pathlib import Path
from typing import Any

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "comfyui"

def _load_comfy_fixture(name: str) -> dict[str, Any]:
    with (_FIXTURE_DIR / name).open() as f:
        return dict(json.load(f)["response"])
```

**Per-test refactor pattern:** existing tests doing

```python
http_post = lambda url, body: {"prompt_id": "abc-123"}
```

become

```python
http_post = lambda url, body: _load_comfy_fixture("prompt_submit.json")
```

Where the hand-crafted shape diverges from the real shape, the test changes to match real. Every value change is a potential masked bug — reviewer scans each refactor commit (Layer N policy).

**New shape-lockdown tests** (3):

- `test_comfyui_real_shape_required_keys` — asserts the real `/history/{id}` fixture contains the keys production code reads (`outputs`, `outputs.<node_id>.<gifs|videos>` or equivalent). Locks the contract; future ComfyUI schema upgrade breaking this fails loudly.
- `test_comfyui_prompt_submit_shape` — asserts `prompt_submit.json` response has `prompt_id` (the field `ComfyUIBackend.submit` returns).
- `test_comfyui_view_url_shape` — asserts `view.json` `_meta.request_url` matches the `/view?filename=...&type=output&subfolder=...` pattern built by `extract_last_frame` (Layer E) and final-artifact fetch (Layer F).

**Backward compat:** production code stays unchanged unless live smoke surfaces a bug (handled separately as a live-bug commit per AC13).

**Test count delta:** −23 inline dicts / +23 fixture loads (net 0) + 3 shape-lockdown tests. Net `+3` for the refactor proper. Total Layer P additions tracked in AC11.

## Acceptance criteria

All must pass before merge.

1. **AC1 — Graph file convention shipped.** `spec.graph_file` works: relative path resolves against YAML parent dir; absolute used verbatim; both `graph_file` + `graph` set raises `ValidationError`; file-not-found raises `ValidationError` with path + cause.
2. **AC2 — Wan i2v graph committed.** `examples/configs/runpod-comfyui-wan.graph.json` exists, is valid JSON, contains the node set used in the green live smoke. SHA recorded in PROGRESS Phase 26.
3. **AC3 — Node SHA pinning honored.** `ComfyUIEngineConfig.nodes[*].ref` field present; `provision` runs `git checkout <ref>` when set; absent `ref` falls back to clone-HEAD (back-compat). Two new tests assert both paths.
4. **AC4 — Tag-discovery helper.** `RunPodProvider.find_instance_by_tag(key, value)` returns ready instance on match, `None` otherwise. Three new tests cover ready / not-ready / no-match.
5. **AC5 — ComfyUI HTTP capture works.** `_RecordingHTTPSeam` wraps ComfyUI `http_post` / `http_get`; per-endpoint dispatch writes `prompt_submit.json`, `history_done.json`, `view.json`; redaction scrubs `token|key|secret|password`-named fields in `_meta.request_body` and `response`; unknown URL writes `unknown_<sha>.json` + WARNING.
6. **AC6 — Live smoke produces real MP4.** `KINOFORGE_LIVE_TESTS=1 RUNPOD_API_KEY=... RUNPOD_TERMINATE_KEY=... HF_TOKEN=... pixi run pytest tests/live/test_comfyui_wan_live.py::test_runpod_comfyui_wan_live_e2e_smoke` produces an MP4 ≥ 100 KB on real RunPod via ComfyUI + Wan i2v, then destroys the pod cleanly. SHA + path + size + capability_key recorded in PROGRESS Phase 26.
7. **AC7 — Fixtures captured + offline tests refactored.** `tests/engines/fixtures/comfyui/{prompt_submit,history_done,view}.json` committed with `_meta` blocks. 23 existing `test_comfyui.py` tests load via `_load_comfy_fixture`. `rg "prompt_id|history|outputs" tests/engines/test_comfyui.py` shows zero hits in test bodies (only in fixture JSON).
8. **AC8 — Real-shape lockdown.** `test_comfyui_real_shape_required_keys` + `test_comfyui_prompt_submit_shape` + `test_comfyui_view_url_shape` pass against committed fixtures. Future ComfyUI schema upgrades breaking any of these fail loudly.
9. **AC9 — Warm-pod reuse via `KINOFORGE_LIVE_KEEP_POD=1`.** Verified during dev: first run creates fresh pod tagged `kinoforge.layer=layer-p-smoke`; `KEEP_POD=1` skips destroy; second run finds + reuses warm pod (provision skipped via Layer I `provision_state.py` marker); cost-rate logged. PROGRESS Phase 26 records dev iteration count + total spend.
10. **AC10 — Cost safety quadruple-locked.** YAML pins `max_cost_rate_usd_per_hr=0.50`, `budget=2.00`, `idle_timeout=10m` (selfterm fallback). `finally:` block always destroys when `KEEP_POD` unset. All four guards verified by reading YAML + live-test source diff.
11. **AC11 — CI green offline-only.** Full suite (existing 823 + Layer-P additions) passes without `KINOFORGE_LIVE_TESTS` set on Linux + macOS CI. Live test marked `pytest.skip` at module level, shows `skipped` in report.
12. **AC12 — README + PROGRESS updated.** README "Real providers — RunPod" section extended with engine-integration sub-section (env vars, quickstart, KEEP_POD dev loop, cost shape). PROGRESS Phase 26 entry committed; Layer-O carry-forward #1 closed.
13. **AC13 — Production fixes folded back.** Any production bug surfaced during live iteration is committed on `build/layer-p` with a regression test against the captured fixture. Each documented in PROGRESS Phase 26 as "Live-smoke bug catches integrated" (Layer N + Layer I precedent).
14. **AC14 — Core-import invariant preserved.** `test_core_invariant.py` still passes. No new core import of `kinoforge.engines.*` / `kinoforge.providers.*`. `spec.graph_file` loader lives in `core/config.py`, reads `Path` only — no engine awareness.

## Non-goals (explicitly out of scope)

- Serverless RunPod mode live smoke — pod-only this layer.
- Diffusers / Hosted engine integration on real RunPod — separate future layers.
- Batch CLI live smoke — single-generate only (locked Q8=A); batch-on-RunPod is a Layer P+1 candidate.
- Multi-GPU / multi-pod smoke — single GPU, single pod.
- Recorded-fixture replay for HuggingFace model downloads — passthrough.
- ComfyUI `/object_info` schema introspection or graph synthesis — committed hand-authored graph only.
- Custom RunPod docker image with Wan pre-baked — `runpod/pytorch:*` base + provisioner-time install only.
- Sharded-weight multi-file resolver in `HuggingFaceSource` — if Wan 2.2 ships sharded, list each shard as a separate model entry.
- Frame-content validation beyond MP4 file shape (Q5=A) — no ffprobe, no perceptual hash.
- Smoke that runs in CI by default — costs money + needs shared RunPod account.

## Risk register

| Risk | Likelihood | Blast | Mitigation |
|---|---|---|---|
| Live smoke leaks money via dangling pod | low | high | Quadruple-locked cost guards (AC10); `finally:` last-resort destroy; selfterm fallback; KEEP_POD dev-only ergonomic |
| Wan 2.2 model files moved / renamed on HF mid-iteration | low | medium | HF ref pins to specific repo + path; if 404 surfaces, plan task updates path |
| Wan i2v graph hand-authored wrong → ComfyUI submits but generates garbage | medium | low | File-shape AC catches sub-100KB outputs; correct graph emerges from live iteration before fixtures commit |
| Custom-node upstream breaks between SHA-pin and live run | low | low | SHA pin defends after first capture; first-iteration breakage caught + commit advances pin |
| Wan 14B disk usage > pod disk budget (80 GB in YAML) | medium | medium | If disk pressure surfaces, bump YAML `disk_gb`; cost rate unchanged |
| ComfyUI HTTP capture catches secrets in `_meta.request_body` | low | high | Redaction regex runs over `_meta` too, not just `response`; reviewer scans first fixture commit |
| KEEP_POD dev iteration runs accumulate orphan pods | low | low | 10-min idle selfterm catches abandoned pods after ~$0.05; `kinoforge gc --config runpod-comfyui-wan.yaml` catches the rest |
| Wan i2v generation > 15-min `_GEN_TIMEOUT_S` on slow GPU | medium | low | Pick faster offer (RTX 4090 / A5000 over 3090 in `gpu_preference`); bump timeout in plan if needed |
| Sharded-weight UNet (multi-part safetensors) blows up `models:` list | medium | low | Each shard becomes its own entry; YAML grows but no new code |
| Live iteration spend > $5 during shake-out | medium | low | KEEP_POD + warm-pod reuse caps generation-iter cost at ~$0.05; documented in PROGRESS for next layer |

## Open knobs (resolved during plan-writing or first live run)

- ComfyUI image tag — picked at first live run; YAML pins whatever ships.
- Wan 2.2 specific model file names + count — determined when first ComfyUI provision surfaces "missing model" errors. Documented in plan as a live-iteration task.
- Custom-node SHAs — placeholders at branch start; real SHAs captured on first green provision.
- ComfyUI graph topology — exported from ComfyUI desktop on first green generation; committed verbatim.

## Decisions log (from brainstorming, 2026-06-01)

- **Q1=A** — Cheap iteration, `KINOFORGE_LIVE_KEEP_POD=1` opt-in. Re-running the test reuses the warm pod via tag-discovery; finally-block default behavior unchanged.
- **Q2=B** — Recording-seam scope: RunPod GraphQL + ComfyUI HTTP. HuggingFace downloads passthrough.
- **Q3=A** — Wan i2v graph source: hand-authored `runpod-comfyui-wan.graph.json` beside YAML, new `spec.graph_file` loader convention.
- **Q4=A** — Custom-node pinning: git URL + commit SHA per node. Reviewable, reproducible.
- **Q5=A** — MP4 success criteria: file-shape only (path exists, size 100 KB–50 MB, MP4 ftyp magic bytes, capability_key persisted). Layer I parity.
- **Q6=A** — ComfyUI fixture dispatch: per-endpoint, one-shot capture (3 files). 23 offline ComfyUIBackend tests get fixture-replay refactor in this layer.
- **Q7=A** — Keep-pod re-run semantics: tag + reuse via `list_instances` + Layer I `provision_state.py` marker.
- **Q8=A** — Smoke scope: single-generate only. Batch live smoke deferred.
- **Approach=1** — Single branch, single test, autonomous iteration. Mid-stream USER GATE dropped; agent drives the live smoke directly using committed creds in `.env`.

## File / diff inventory

**New files:**

| Path | Purpose | Approx LOC / size |
|---|---|---|
| `examples/configs/runpod-comfyui-wan.graph.json` | Wan i2v workflow | ~200–500 lines |
| `tests/engines/fixtures/comfyui/prompt_submit.json` | POST /prompt capture | ~15 |
| `tests/engines/fixtures/comfyui/history_done.json` | GET /history/{id} capture | ~40 |
| `tests/engines/fixtures/comfyui/view.json` | GET /view metadata | ~10 |
| `tests/live/test_comfyui_wan_live.py` | live E2E gate | ~280 |

**Modified files:**

| Path | Change | Approx LOC delta |
|---|---|---|
| `src/kinoforge/core/config.py` | `spec.graph_file` loader convention | ~+50 |
| `src/kinoforge/engines/comfyui/__init__.py` | `nodes[*].ref` git-checkout extension (if absent) | ~+15 |
| `src/kinoforge/providers/runpod/__init__.py` | `find_instance_by_tag` helper | ~+20 |
| `tests/providers/conftest_runpod.py` | `_RecordingHTTPSeam` extension for ComfyUI dispatch + redaction over `_meta` | ~+40 |
| `tests/engines/test_comfyui.py` | dicts → `_load_comfy_fixture`; +3 shape-lockdown tests | ~+30 / −60 net |
| `tests/core/test_config.py` | 3 `graph_file` round-trip tests | ~+50 |
| `tests/engines/test_comfyui.py` | 2 SHA-pin tests | ~+30 |
| `tests/providers/test_runpod.py` | 3 tag-discovery tests | ~+45 |
| `examples/configs/runpod-comfyui-wan.yaml` | `spec.graph_file`, `engine.comfyui.nodes` SHA pins, real `models:` set | ~+30 / −10 net |
| `README.md` | engine-integration sub-section under "Real providers — RunPod" | ~+50 |
| `PROGRESS.md` | Phase 26 entry + close Layer-O carry-forward #1 | ~+45 |

## Test count expectation

Pre-Layer-P: 823 passed + 1 skipped.

Layer P net additions:
- +3 `test_config.py` (graph_file)
- +2 `test_comfyui.py` (SHA pin)
- +3 `test_runpod.py` (tag discovery)
- +2 `conftest_runpod.py` / recording-seam (ComfyUI capture)
- +3 `test_comfyui.py` (shape lockdown)
- 0 net from the 23-test fixture-replay refactor
- +1 live test (skipped in CI)

Post-Layer-P: ~836 passed + 2 skipped.

## Branch + merge

- Branch: `build/layer-p` off `main@7788f93`.
- Merge: `--no-ff` with substantive body referencing AC state, per-task commits, GitHub issue trailer if applicable, live-smoke spend total, MP4 path/size/SHA.
- Backfill PROGRESS merge-commit SHA after merge (Layer C–O precedent).
