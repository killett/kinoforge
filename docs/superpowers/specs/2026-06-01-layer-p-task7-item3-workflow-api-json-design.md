# Layer P — Task 7 item #3 — Real workflow API JSON for Wan 2.1 i2v

Sub-spec for the third sub-plan inside Layer P Task 7. Closes the open
items in `PROGRESS.md` lines 229–238: workflow-format conversion (item #3)
PLUS the four "remaining unknowns to surface via live iteration" listed
as item #4. Item #4 is absorbed into this sub-plan because the unknowns
can only be exercised by an actual end-to-end live run, and the run that
validates the new graph is the same run that surfaces them.

Parent spec: `docs/superpowers/specs/2026-06-01-layer-p-runpod-engine-integration-design.md`
Sub-spec precedents: item #1 `2026-06-01-layer-p-task7-item1-offer-retry-design.md`,
item #2 `2026-06-01-layer-p-task7-item2-warm-reuse-design.md`.

## 1. Goal

Land the first green MP4 from kinoforge end-to-end on real cloud
compute. Concretely:

- Replace the 166-byte placeholder `examples/configs/runpod-comfyui-wan.graph.json`
  with a real ComfyUI API-format workflow graph for Wan 2.1 i2v 14B 480P
  fp8 — 26 nodes, semantic string IDs, validated against a real ComfyUI
  server.
- Wire `examples/configs/runpod-comfyui-wan.yaml` to the real node IDs
  in `asset_node_ids` / `prompt_node_ids` / `node_overrides`, and fix
  the pre-existing list-vs-dict type mismatch on `prompt_node_ids`.
- Commit a captured `/object_info` schema dump as a test fixture so
  future graph edits have a static reference and so regression tests
  can lock node-class shape without a live pod.
- Add a unit test that loads the committed graph + YAML and locks down
  graph shape + cross-references between YAML node-ID maps and graph
  keys.
- Prove the stack by running the live smoke against the new graph,
  capturing a non-empty MP4 with valid `ftyp` magic, and recording
  evidence in `PROGRESS.md`.

## 2. Scope decisions (locked in brainstorm)

| Q | Decision | Reason |
|---|---|---|
| Q1 | Scope D — graph + YAML wiring + lockdown test + live MP4 (item #4 absorbed) | Item #4 is operationally inseparable from the live run that validates the new graph |
| Q2 | Hand-author + REST-validate against warm pod | No browser session needed; agent-driveable; produces canonical ground truth (server accepts graph) without manual UI clicks |
| Q3 | `/object_info`-grounded then `/prompt` parse-only | Highest signal-per-iteration. Dev-time schema capture only — engine never calls `/object_info` at runtime (compliant with parent spec §Out-of-scope line 400) |
| Q4 | Semantic string IDs (`load_image`, `wan_sampler`, etc) | Self-documenting YAML wiring; ComfyUI accepts any string IDs; mapping to kijai source IDs lives in commit message |
| Q5 | `/object_info` dump committed under `tests/engines/fixtures/comfyui/` | Acts as reproducibility anchor for future graph edits + regression-test seed; 1-2 MB acceptable |

Cost projection for live session: $0.50–$2.00 (covers item #3 graph
production + item #4 unknown surfacing). Budget cap remaining at
sub-plan start: $1.99.

## 3. Out of scope

- **Engine-side `/object_info` runtime call.** Forbidden by parent spec
  §Out-of-scope (line 400). `/object_info` is dev-time reference only;
  the committed graph is hand-authored. The fixture file is read by
  offline tests, never by `engines/comfyui/__init__.py` at runtime.
- **Generic desktop→API JSON converter.** One-shot artifact for one
  workflow. No tooling, no library, no automation. Future workflows
  repeat the same process.
- **Offline ComfyUI HTTP fixture refactor.** That's Task 8 of the main
  Layer P plan. This sub-plan captures whatever fixtures the live run
  produces via the existing `KINOFORGE_SAVE_FIXTURES=1` seam, then
  Task 8 consumes them.
- **ComfyUI shape-lockdown tests.** That's Task 9 of the main Layer P
  plan. This sub-plan ships only the graph-file lockdown
  (`tests/examples/test_runpod_comfyui_wan_graph.py`); ComfyUI
  backend-internal lockdowns are Task 9.
- **README + main-Layer-P merge to main.** That's Task 10. This
  sub-plan stays on `build/layer-p`.
- **A capture-script artifact (`scripts/capture_object_info.py`).** One
  curl command embedded in this sub-spec is enough; adding a Python
  script bloats the repo for a single-use dev tool.

## 4. Architecture

Three phases. Phase A and C are offline (no pod, no cost). Phase B is
one continuous live session on a single warm pod that absorbs item #4
unknowns.

```
Phase A (offline) ──────────► Phase B (warm pod, 10–30 min) ──────────► Phase C (offline)
  lockdown scaffold              boot ┬ /object_info → fixture            lockdown asserts
  type-regression test                ├ hand-author graph                 PROGRESS evidence
                                      ├ /prompt parse-validate            sub-plan close
                                      ├ YAML wiring
                                      └ full render → MP4 + fixtures
```

The pod's lifecycle uses Task 7 item #2's warm-reuse
(`orchestrator.generate(instance=…, tags=…)`); item #1's offer-retry
absorbs capacity errors during initial boot. No new orchestrator
behavior in this sub-plan.

## 5. Component / file map

### 5.1 Files this sub-plan creates or edits

| Path | Action | Size est | Purpose |
|---|---|---|---|
| `examples/configs/runpod-comfyui-wan.graph.json` | replace placeholder | ~150 lines | 26-node ComfyUI API-format graph, semantic IDs, hand-authored from kijai desktop + `/object_info` schema |
| `examples/configs/runpod-comfyui-wan.yaml` | edit | ~10-line diff | Real `asset_node_ids.init_image`, `prompt_node_ids.positive`, `node_overrides`; `prompt_node_ids` becomes dict |
| `tests/engines/fixtures/comfyui/object_info_wan21.json` | new | ~1–2 MB | Captured ComfyUI `/object_info` from warm pod with kijai nodes loaded |
| `tests/examples/test_runpod_comfyui_wan_graph.py` | new | ~80 LOC | Lockdown: graph shape, node count, class-type whitelist, YAML cross-references |
| `tests/engines/test_comfyui.py` | edit | ~+20 LOC | Add one test that loads the `/object_info` fixture and asserts Wan class entries exist |
| `PROGRESS.md` | edit | item #3 closure row | Evidence: MP4 path/bytes, cost, git SHA, bug-catch list, fixture provenance |

### 5.2 Files this sub-plan may edit during live shake-out

Each live-iteration bug-catch lands as its own commit with an offline
regression test against a captured fixture shape (Layer N pattern).
The five likely-touched files map to the four unknowns plus expected
edge cases:

| File | Bug class |
|---|---|
| `src/kinoforge/engines/comfyui/__init__.py` (`submit`, `result`, `_upload_init_image`) | `/upload/image` multipart shape against the real server; `/history/<id>` outputs key extraction (videos/gifs/images) |
| `src/kinoforge/engines/comfyui/nodes.py` | Custom-node `requirements.txt` install path on `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04` base image |
| `src/kinoforge/core/provision_state.py` | Marker registration under warm-tag-discovery pod (one of the item #4 unknowns) |
| `src/kinoforge/sources/huggingface/__init__.py` and/or ComfyUI `TARGET_TO_SUBDIR` | `text_encoder` target routing — verify `umt5-xxl-enc-fp8_e4m3fn.safetensors` lands in `models/text_encoders/` |

If a fifth distinct bug class blocks green MP4, abort sub-plan and
escalate (see §8 bug-catch budget).

## 6. Phase A: offline scaffolding (no pod)

### 6.1 Lockdown test scaffold

New file `tests/examples/test_runpod_comfyui_wan_graph.py`. Four tests:

```python
from pathlib import Path
import pytest
from kinoforge.core.config import load_config

YAML_PATH = Path("examples/configs/runpod-comfyui-wan.yaml")
EXPECTED_NODE_COUNT = 26
EXPECTED_CLASS_TYPES = {
    "LoadImage", "CLIPLoader", "CLIPTextEncode", "CLIPVisionLoader",
    "ImageResizeKJv2", "LoadWanVideoT5TextEncoder", "Note",
    "VHS_VideoCombine", "WanVideoBlockSwap", "WanVideoClipVisionEncode",
    "WanVideoDecode", "WanVideoImageToVideoEncode", "WanVideoLoraSelect",
    "WanVideoModelLoader", "WanVideoSampler", "WanVideoSetBlockSwap",
    "WanVideoTextEmbedBridge", "WanVideoTextEncode",
    "WanVideoTorchCompileSettings", "WanVideoVAELoader",
    "WanVideoVRAMManagement",
}


def test_graph_shape_api_format() -> None:
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    assert isinstance(graph, dict)
    assert len(graph) == EXPECTED_NODE_COUNT
    for node_id, node in graph.items():
        assert isinstance(node_id, str)
        assert "class_type" in node
        assert "inputs" in node
        assert isinstance(node["inputs"], dict)


def test_graph_class_types_within_expected_set() -> None:
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    actual = {node["class_type"] for node in graph.values()}
    assert actual <= EXPECTED_CLASS_TYPES, (
        f"unexpected class_types: {actual - EXPECTED_CLASS_TYPES}"
    )


def test_asset_node_ids_reference_existing_nodes() -> None:
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    asset_node_ids = cfg.spec["asset_node_ids"]
    for role, node_id in asset_node_ids.items():
        assert node_id in graph, (
            f"asset role {role!r} → node {node_id!r} not in graph"
        )


def test_prompt_node_ids_is_dict_and_references_existing_nodes() -> None:
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    prompt_node_ids = cfg.spec["prompt_node_ids"]
    assert isinstance(prompt_node_ids, dict), (
        "prompt_node_ids must be dict (Layer J), not list"
    )
    for role, node_id in prompt_node_ids.items():
        assert node_id in graph, (
            f"prompt role {role!r} → node {node_id!r} not in graph"
        )
```

At Phase A commit time these tests are RED — the placeholder graph
has 1 node, not 26, and `prompt_node_ids` is still a list. They go
GREEN at the end of Phase B when the real graph + YAML edits land.

### 6.2 Phase A commit

```
test(examples): lockdown scaffold for runpod-comfyui-wan graph (RED)
```

## 7. Phase B: warm-pod live session

### 7.1 Step 1 — Boot pod via existing live smoke

```bash
export KINOFORGE_LIVE_TESTS=1 RUNPOD_API_KEY=... RUNPOD_TERMINATE_KEY=... HF_TOKEN=...
export KINOFORGE_SAVE_FIXTURES=1 KINOFORGE_LIVE_KEEP_POD=1
pixi run pytest tests/live/test_comfyui_wan_live.py -v -s
```

Smoke boots pod, provisions ComfyUI + kijai custom nodes, downloads
Wan weights (~25 GB). Test will fail at the generate step (placeholder
graph) — that is expected and intentional. `KINOFORGE_LIVE_KEEP_POD=1`
keeps the pod warm.

### 7.2 Step 2 — Locate pod + proxy URL

```bash
pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml
```

Yields `POD_ID`. Proxy URL: `https://${POD_ID}-8188.proxy.runpod.net`.

### 7.3 Step 3 — Capture `/object_info`

```bash
curl -sS "https://${POD_ID}-8188.proxy.runpod.net/object_info" \
  | jq . > tests/engines/fixtures/comfyui/object_info_wan21.json
```

Assert non-empty + parses + contains `WanVideoSampler`/`WanVideoModelLoader`
keys. Commit:

```
test(fixtures): commit /object_info dump from warm pod for Wan 2.1 i2v graph reference
```

### 7.4 Step 4 — Hand-author graph

Reference `/tmp/wan21_i2v_workflow.json` (kijai desktop) + captured
`/object_info` schema. Per node:

- `class_type` ← desktop `node.type`
- API JSON node ID ← semantic name from the locked mapping below in this section
- `inputs` ← merge of:
  - resolved widget values (`node.widgets_values[i]` mapped to
    `INPUT_TYPES.required[name]` per `/object_info`)
  - resolved link inputs (`node.inputs[i]` resolved via
    `desktop.links` → `[src_node_semantic_id, src_port_index]`)

Output: 26-entry `examples/configs/runpod-comfyui-wan.graph.json`,
ASCII keys, alphabetised by semantic ID for diff stability.

Locked semantic ID mapping:

| Kijai node type | Semantic ID |
|---|---|
| `LoadImage` | `load_image` |
| `WanVideoModelLoader` | `wan_model` |
| `WanVideoVAELoader` | `wan_vae` |
| `LoadWanVideoT5TextEncoder` | `wan_t5_text` |
| `CLIPVisionLoader` | `wan_clip_vision_loader` |
| `WanVideoTextEncode` (positive) | `text_encode_pos` |
| `WanVideoTextEncode` (negative) | `text_encode_neg` |
| `ImageResizeKJv2` | `image_resize` |
| `WanVideoClipVisionEncode` | `clip_vision_encode` |
| `WanVideoImageToVideoEncode` | `i2v_encode` |
| `WanVideoSampler` | `wan_sampler` |
| `WanVideoDecode` | `wan_decode` |
| `VHS_VideoCombine` | `vhs_video_combine` |

Remaining ~13 nodes (block_swap, vram_mgmt, torch_compile, lora_select,
text_embed_bridge, plus duplicates and `Note` annotations) get short
semantic names assigned during authoring and listed in the commit
message.

No commit yet — graph commits once parse-validates in step 5.

### 7.5 Step 5 — Parse-only validate via `/prompt`

```python
import requests, json
graph = json.load(open("examples/configs/runpod-comfyui-wan.graph.json"))
r = requests.post(
    f"https://{POD_ID}-8188.proxy.runpod.net/prompt",
    json={"prompt": graph, "client_id": "kinoforge-dev"},
    timeout=30,
)
```

- 200 → `{"prompt_id": "..."}`. Cancel via `DELETE /queue` so the pod
  doesn't actually render. Graph is parse-valid.
- 4xx → response body is `{"error": {...}, "node_errors": {<id>: [...]}}`.
  Patch the listed nodes' `inputs` until error list empties.
- 5xx → restart ComfyUI on pod or destroy + reboot. Cost penalty ~$0.10.
  If recurring (>2 reboots) → escalate.
- Repeat until 200.

Commit:

```
feat(examples): real ComfyUI API-format graph for Wan 2.1 i2v 14B 480P fp8
```

Commit body lists the 26 semantic-ID-to-kijai-ID mapping and references
the captured `/object_info` SHA.

### 7.6 Step 6 — YAML wiring

Edit `examples/configs/runpod-comfyui-wan.yaml`:

```yaml
spec:
  graph_file: runpod-comfyui-wan.graph.json
  asset_node_ids:
    init_image: load_image
  prompt_node_ids:
    positive: text_encode_pos
  node_overrides: {}
```

`prompt_node_ids` flips from list `["8"]` to dict `{positive: text_encode_pos}` —
required by `engines/comfyui` Layer J code that calls `.items()` on
the value. The negative prompt is baked into the graph itself as the
`text_encode_neg` node's default — Layer J only routes one prompt
through `prompt_node_ids`.

Run Phase A lockdown tests → all four pass (RED → GREEN). Commit:

```
feat(examples): wire runpod-comfyui-wan.yaml to real node IDs (Layer J dict)
```

### 7.7 Step 7 — Full execution via `kinoforge generate`

Re-run live smoke. With `KINOFORGE_LIVE_KEEP_POD=1` set, the pod is
already warm; `orchestrator.generate(instance=…)` (Task 7 item #2)
short-circuits create.

Watch `/history/<prompt_id>` until `outputs` populated or
`status.completed=true`. On failure → patch per §8 failure table.

Each code patch lands as its own commit with an offline regression
test against the captured ComfyUI HTTP fixture shape:

```
fix(engines/comfyui): <one-line bug summary>
```

First green run produces `output/<run_id>/<filename>.mp4` (Layer O
sink). Validate magic bytes against `_MP4_FTYP_PREFIXES` from the
live smoke. Capture fixtures via `KINOFORGE_SAVE_FIXTURES=1` (no new
commit — captures land alongside any code patch commits).

### 7.8 Step 8 — Destroy pod

Smoke teardown destroys via `RUNPOD_TERMINATE_KEY`. Final cost noted
for Phase C.

## 8. Failure modes and abort criteria

### 8.1 Failure response table

| Failure | Phase | Response |
|---|---|---|
| Pod boot no-capacity | step 1 | Already absorbed by item #1 offer-retry. No new code. |
| Custom-node `requirements.txt` install fails | step 1 | Patch `engines/comfyui/nodes.py`. Regression test against captured stderr fixture. Continue. |
| HF gated repo 401 | step 1 | User action (accept gate). Pause sub-plan; document in PROGRESS. |
| `/object_info` empty or 4xx | step 3 | 3 × 30 s retry (ComfyUI lazy init). If still failing → abort, fall back to approach 3 (kijai source) on next attempt. |
| `/prompt` 422 with `node_errors` | step 5 | Read `node_errors[<id>]`. Patch graph. Iterate. |
| `/prompt` 422 with empty `node_errors` | step 5 | Schema drift from `/object_info`. Re-capture `/object_info`, diff, escalate if unstable. |
| `/prompt` 5xx | step 5 or 7 | Restart ComfyUI on pod, or destroy + reboot. Cost ~$0.10. If recurring → escalate. |
| `/history` execution_error | step 7 | Engine-fixable (multipart, target routing) → patch + regression. Weight-related → escalate (likely wrong weights manifest). |
| Pod idle-timeout autostop | any | Warm-tag rediscover (item #2 path). No code change. |
| Budget cap ($1.99) hit | any | `BudgetTracker.enforce` tears down. Commit landed work. Sub-plan partial-close. |
| MP4 invalid (0 bytes, wrong magic) | step 7 | `engine.result()` or download seam bug. Patch + regression. |

### 8.2 Bug-catch budget

Maximum **5 distinct production bug classes** absorbed into this
sub-plan. If a sixth distinct class surfaces:

1. Commit what's landed.
2. Record state in PROGRESS.md.
3. Abort sub-plan.
4. Escalate to user; remainder splits into item #3a / #3b / etc.

Matches item #1 (10 bugs in a verification-only sub-plan) precedent
inverted: this sub-plan's primary deliverable is the graph + green
MP4, not bug-fixing.

### 8.3 User checkpoints

Three points where the agent pauses for user confirmation before
continuing autonomously. These become user-gate task entries in the
implementation plan.

1. **Before pod boot.** Confirm `KINOFORGE_LIVE_TESTS=1` + creds set;
   OK to start spending. (Existing live smoke env gate already
   enforces this passively; this checkpoint is explicit.)
2. **After first `/prompt` 200.** Graph parses against real server.
   OK to proceed to full render (which is where most cost lands).
3. **After green MP4.** Validate magic bytes + file size. OK to close
   sub-plan + write PROGRESS evidence row.

## 9. Phase C: close-out (offline, no pod)

### 9.1 Lockdown tests finalize

Phase A's four tests now pass against the real graph + YAML. Confirm
via `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v`.

Add one test in `tests/engines/test_comfyui.py` that loads the
`/object_info` fixture and asserts presence of expected Wan class
entries — locks the fixture's utility for future graph edits.

### 9.2 PROGRESS update

Add item #3 closure row in PROGRESS.md following item #1 / item #2
template. Fields:

- Sub-spec path + commit SHA
- Sub-plan path + commit SHA
- Atomic commit list (sub-spec, sub-plan, fixture, graph, YAML, code
  patches, lockdown)
- MP4 evidence: path, bytes, ftyp magic, run ID, capability key, git
  SHA at capture
- Cost burn final
- Bug-catch list (zero or more entries)
- Test count delta
- Key design decisions block (mirror item #1/#2 shape)

Commit:

```
docs(progress): Layer P Task 7 item #3 — closure snapshot
```

### 9.3 Update "Pending Task 7 work" block

In PROGRESS.md lines 226–238, mark item #3 CLOSED (mirroring how
item #1 + item #2 are listed). Note that item #4 was absorbed.

## 10. Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `examples/configs/runpod-comfyui-wan.graph.json` is real ComfyUI API JSON: dict-of-dict, exactly 26 nodes, semantic string IDs per §7.4 mapping, every value has `class_type` + `inputs` keys |
| AC2 | `examples/configs/runpod-comfyui-wan.yaml` updated: `asset_node_ids.init_image` references an existing graph node ID; `prompt_node_ids` is a `dict` (not list) with at least `positive` key referencing an existing graph node ID |
| AC3 | `tests/engines/fixtures/comfyui/object_info_wan21.json` committed: ~1–2 MB, parses as JSON dict, contains entries for every `class_type` used in the graph |
| AC4 | `tests/examples/test_runpod_comfyui_wan_graph.py` exists with 4 tests; all pass against committed graph + YAML |
| AC5 | Live smoke `pixi run pytest tests/live/test_comfyui_wan_live.py -v -s` produces a non-empty MP4 whose first 12 bytes contain one of `_MP4_FTYP_PREFIXES` (`ftypisom`, `ftypiso5`, `ftypiso6`, `ftypmp42`) |
| AC6 | Cost burn for sub-plan ≤ $1.50 (single live session) |
| AC7 | Every live-iteration bug-catch code commit ships an offline regression test using captured fixtures (Layer N pattern); zero or more such commits |
| AC8 | `test_core_invariant.py` passes — no new `kinoforge.engines.*` import in `kinoforge.core.*` |
| AC9 | `pixi run mypy . && pixi run ruff check . && pixi run pre-commit run --all-files` clean after every commit |
| AC10 | `PROGRESS.md` item #3 closure row written with the fields listed in §9.2 |
| AC11 | Offline test count: net ≥ +5 (4 lockdown tests in §6.1 + 1 `/object_info` fixture parse test in §9.1; bug-catch regression tests add to this floor if any); final count ≥ 863 (was 858 pre-sub-plan) |

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| `/object_info` unavailable or unstable | Approach 3 fallback (kijai upstream Python `INPUT_TYPES` source code) documented in §2 Q3 + §8.1; abort criteria explicit |
| Hand-authoring 26 nodes blind in one session | `/object_info` capture + kijai desktop source give complete schema + connectivity. Parse-only `/prompt` iteration loop catches errors fast (seconds per round-trip) |
| Bug-catch sprawl | 5-class budget cap (§8.2). Abort with partial-close PROGRESS row if exceeded |
| Live cost overrun | Existing quadruple-locked cost guards (max_cost_rate, BudgetTracker, idle_timeout + selfterm, test finally-destroy) |
| Schema drift mid-session (kijai PR mid-iteration) | `/object_info` fixture committed BEFORE graph is authored — frozen reference for the session |
| `KINOFORGE_LIVE_KEEP_POD=1` left set after sub-plan | Existing pod's 10-min idle + selfterm tears it down even if test process dies; cost-bounded |

## 12. Commit shape

Matches item #1 / item #2 atomic commit precedent:

| Phase | Commit type | Subject |
|---|---|---|
| A | test | `test(examples): lockdown scaffold for runpod-comfyui-wan graph (RED)` |
| B step 3 | test | `test(fixtures): commit /object_info dump from warm pod for Wan 2.1 i2v graph reference` |
| B step 5 | feat | `feat(examples): real ComfyUI API-format graph for Wan 2.1 i2v 14B 480P fp8` |
| B step 6 | feat | `feat(examples): wire runpod-comfyui-wan.yaml to real node IDs (Layer J dict)` |
| B step 7 | fix × N | `fix(engines/comfyui): <bug>` (zero or more, each with regression test) |
| C | test | `test(engines/comfyui): /object_info fixture lockdown` |
| C | docs | `docs(progress): Layer P Task 7 item #3 — closure snapshot` |

Code-review fix and spec self-review polish commits follow the same
atomic pattern as items #1 / #2.

## 13. Open questions

None at sub-spec time. All design questions resolved during brainstorm
(Q1–Q5 in §2). Live-iteration unknowns (item #4 — multipart shape,
requirements.txt path, /history shape, marker registration) are
absorbed via the bug-catch budget; they are not "open questions" in
the spec-time sense — they are expected encounters with documented
response paths in §8.
