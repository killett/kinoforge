# Layer P Task 7 item #3 resume — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the first green MP4 from kinoforge end-to-end on real RunPod by replacing the placeholder ComfyUI workflow graph with a kijai-pinned 26-node Wan 2.1 i2v API JSON, wiring the example YAML, flipping the 4 RED lockdown tests GREEN, adding 2 new lockdown invariants (kijai SHA cross-reference + env_required), and closing item #3 in PROGRESS — all under Layer Q's `render_provision` / `wait_for_ready` cold-boot path.

**Architecture:** Phase A (Tasks 1–5) is offline + hermetic — zero live spend. Phase B (Task 6) exercises Layer Q's cold-boot path on real RunPod, env-gated, ≤$1.50 cap, absorbs up to 5 item #4 bug-catches as additional in-task commits with Layer-N-pattern regression tests. Phase C (Task 7) writes the PROGRESS closure block. No interactive USER-GATEs; env-gate-only authorization per spec §14.G.

**Tech Stack:** Python 3.13 / pixi / pytest / ruff / mypy / pre-commit. `kijai/ComfyUI-WanVideoWrapper` (offline pull at pinned SHA). RunPod compute via existing kinoforge adapter. ComfyUI engine via Layer Q `render_provision` + `wait_for_ready` ABC. Source spec: `docs/superpowers/specs/2026-06-01-layer-p-task7-item3-workflow-api-json-design.md` (amended 2026-06-02 at `23b1501`, polished at `744575c`).

---

## Task ordering and dependencies

```
T1 (kijai pull + _meta strip in core/config) ─┬─► T2 (drop xfail) ─► T3 (wire YAML) ─┬─► T4 (AC12 SHA cross-ref test) ─┬─► T6 (live MP4) ─► T7 (PROGRESS closure)
                                              │                                       └─► T5 (AC13 env_required test) ──┘
                                              └─► (lockdown tests still xfail, but graph now real — graph-shape test would PASS even with xfail-strict=False; T2 makes it canonical)
```

T2, T3, T4, T5 each depend on T1 (need the kijai-pulled graph in place). T6 depends on T2/T3/T4/T5 (full Phase-A green before any live spend). T7 depends on T6.

---

## Task 1: Pull kijai upstream + commit real graph + add `_meta` strip in `core/config`

**Goal:** Replace the 8-byte placeholder graph at `examples/configs/runpod-comfyui-wan.graph.json` with the real 26-node Wan-i2v API-format graph pulled from `kijai/ComfyUI-WanVideoWrapper` at a pinned SHA; add a single-source-of-truth `_meta` strip in `core/config._resolve_spec_graph_file` so runtime + tests both see a `_meta`-free `cfg.spec["graph"]`.

**Files:**
- Modify: `src/kinoforge/core/config.py` (function `_resolve_spec_graph_file` around line 645; add one-line pop after JSON inline)
- Modify: `examples/configs/runpod-comfyui-wan.graph.json` (replace placeholder with real 26-node graph + `_meta` header)
- Create: `tests/core/test_config_graph_meta.py` (new test for `_meta` strip behaviour)

**Acceptance Criteria:**
- [ ] Graph file is valid JSON: top-level dict with `_meta` key + exactly 26 stringified-int node keys.
- [ ] Each node entry (excluding `_meta`) has `class_type` (str) and `inputs` (dict).
- [ ] `_meta` carries: `source_repo`, `source_sha` (40-hex), `source_path` (relative), `captured_at_local` (ISO-ish local-TZ string), `format` ("api"), `converter` ("verbatim" or `tools/comfyui_ui_to_api.py@<sha>`).
- [ ] `core/config._resolve_spec_graph_file` pops `_meta` from the inlined graph dict (the popped key never appears in `cfg.spec["graph"]`).
- [ ] New test in `tests/core/test_config_graph_meta.py` asserts: (a) `load_config` returns `cfg.spec["graph"]` with NO `_meta` key when the source file has one; (b) raw `json.loads(graph_file_path.read_text())` still exposes `_meta` for AC12 consumers.

**Verify:** `pixi run pytest tests/core/test_config_graph_meta.py -v` → 2 passed; `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v` → 4 xfailed (xfail marker still in place; passing inside the xfail block is fine — markers come off in T2).

**Steps:**

- [ ] **Step 1: Survey kijai upstream + pick pinned SHA + select example workflow.**

The YAML currently pins `kijai/ComfyUI-WanVideoWrapper` at `088128b224242e110d3906c6750e9a3a348a659b` (`examples/configs/runpod-comfyui-wan.yaml:32`). Stick with this SHA unless kijai has tagged a newer release whose `example_workflows/` directory contains a clean `_api.json` for Wan 2.1 i2v 14B 480P.

```bash
TMPDIR=/tmp/kijai-pull
mkdir -p "$TMPDIR"
git clone --depth 1 https://github.com/kijai/ComfyUI-WanVideoWrapper "$TMPDIR/wrapper" 2>&1 | tail -3
cd "$TMPDIR/wrapper"
# resolve the most recent tag (or HEAD if no tags)
LATEST_TAG=$(git tag --sort=-creatordate | head -1)
if [ -z "$LATEST_TAG" ]; then
  PINNED_SHA=$(git rev-parse HEAD)
  echo "no tags; pinning HEAD: $PINNED_SHA"
else
  PINNED_SHA=$(git rev-list -n 1 "$LATEST_TAG")
  echo "pinning tag $LATEST_TAG → $PINNED_SHA"
fi
git checkout "$PINNED_SHA"
# survey for i2v API-format examples
ls example_workflows/ | rg -i 'i2v|i_2_v|image.*video' || ls example_workflows/
ls example_workflows/ | rg -i 'api'
```

Pick the candidate whose filename matches the YAML's model family (`Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors` per `examples/configs/runpod-comfyui-wan.yaml:37`). Filename containing `_api` is preferred. If only UI-format is present, branch to Step 1b (UI→API converter). If neither, abort the task and re-spec.

Record the picked file path (relative to the kijai repo root) and the pinned SHA in a scratch note — both go into the `_meta` header in Step 3.

- [ ] **Step 1b (CONDITIONAL — only if kijai ships UI format only):** Pause sub-plan and brainstorm the converter before writing code.

Skip this step if the picked file is already API format (the common case — kijai typically ships both `*.json` UI-format AND `*_api.json` API-format in `example_workflows/`).

If only UI format is present at the pinned SHA, the converter's exact shape depends on kijai's `INPUT_TYPES` classmethods at that SHA, which cannot be pre-specified at plan-write time. The right move is NOT to ad-hoc the converter inline; it is to PAUSE this sub-plan, invoke the `superpowers-extended-cc:brainstorming` skill with the concrete problem (one UI JSON file at known SHA → API JSON via offline kijai source walk), and emerge with a focused mini-spec + mini-plan for `tools/comfyui_ui_to_api.py`. The mini-plan lives under `docs/superpowers/plans/2026-06-XX-comfyui-ui-to-api.md`, ships its own atomic commits, and feeds its output (the emitted API JSON + the converter binary at a known git SHA) back into T1 Step 3.

Resume rules for Step 3 after the converter mini-plan ships:
- `_meta.converter` value: `"tools/comfyui_ui_to_api.py@<converter-commit-sha>"`.
- Graph JSON content: produced by running `python tools/comfyui_ui_to_api.py --ui-json <kijai-ui-file> --kijai-src-dir <pinned-repo-path> --out examples/configs/runpod-comfyui-wan.graph.json`.
- All other `_meta` fields unchanged.

Do NOT attempt to write the converter inline in this sub-plan even if the UI-only path is hit — the brainstorm → spec → plan loop is cheaper than throwing away half-baked converter code.

- [ ] **Step 2 (TDD red): Write failing test for `_meta` strip behaviour.**

```python
# tests/core/test_config_graph_meta.py
"""Lock down config-load behaviour for graph _meta header strip.

The committed graph JSON has a top-level _meta key carrying provenance
(source_repo, source_sha, source_path, captured_at_local, format,
converter). ComfyUI's /prompt endpoint validates every top-level key as a
node ID and rejects unrecognized keys, so _meta must NOT survive into
cfg.spec["graph"]. Single source of truth: strip happens at config-load
time in _resolve_spec_graph_file; runtime ComfyUIBackend.submit and
offline tests both see a _meta-free dict.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kinoforge.core.config import load_config


def test_load_config_strips_meta_from_inlined_graph(tmp_path: Path) -> None:
    """load_config must pop _meta from the graph dict it inlines under cfg.spec["graph"]."""
    graph_path = tmp_path / "test.graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "_meta": {
                    "source_repo": "https://example.test/repo",
                    "source_sha": "0" * 40,
                    "source_path": "example_workflows/x.json",
                    "captured_at_local": "2026-06-02T12:00-0700",
                    "format": "api",
                    "converter": "verbatim",
                },
                "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
            }
        )
    )
    yaml_path = tmp_path / "test.yaml"
    yaml_path.write_text(
        "engine:\n  kind: fake\n"
        "models:\n  - ref: 'http://example.test/x.bin'\n    kind: base\n    target: base\n"
        f"spec:\n  graph_file: {graph_path.name}\n"
    )
    cfg = load_config(yaml_path)
    graph = cfg.spec["graph"]
    assert "_meta" not in graph, (
        f"_meta survived inline strip: {sorted(graph.keys())}"
    )
    assert "1" in graph, f"node key dropped: {sorted(graph.keys())}"


def test_raw_json_still_carries_meta_for_ac12_consumers(tmp_path: Path) -> None:
    """AC12's cross-reference test reads the raw JSON file directly — _meta must remain on disk."""
    graph_path = tmp_path / "test.graph.json"
    payload = {
        "_meta": {"source_sha": "deadbeef" * 5},
        "1": {"class_type": "LoadImage", "inputs": {}},
    }
    graph_path.write_text(json.dumps(payload))
    loaded = json.loads(graph_path.read_text())
    assert loaded["_meta"]["source_sha"] == "deadbeef" * 5
```

Run to confirm RED: `pixi run pytest tests/core/test_config_graph_meta.py -v` → both tests FAIL (`_meta` is currently inlined verbatim because `_resolve_spec_graph_file` doesn't strip it).

- [ ] **Step 3: Edit `core/config._resolve_spec_graph_file` to strip `_meta` after inlining.**

Current code (around `src/kinoforge/core/config.py:700-710`) ends roughly like:

```python
spec["graph"] = json.loads(raw_json)
del spec["graph_file"]
```

Change to:

```python
graph_payload = json.loads(raw_json)
graph_payload.pop("_meta", None)  # provenance header — strip before runtime/validation
spec["graph"] = graph_payload
del spec["graph_file"]
```

Re-run `pixi run pytest tests/core/test_config_graph_meta.py -v` → both tests PASS.

- [ ] **Step 4: Write the real graph JSON to `examples/configs/runpod-comfyui-wan.graph.json`.**

Replace the placeholder content with the kijai-pulled API JSON, prefixed by the `_meta` header. Structure:

```json
{
  "_meta": {
    "source_repo": "https://github.com/kijai/ComfyUI-WanVideoWrapper",
    "source_sha": "<PINNED_SHA from Step 1>",
    "source_path": "example_workflows/<PICKED_FILE>",
    "captured_at_local": "<YYYY-MM-DDTHH:MM-TZ via Python datetime.now()>",
    "format": "api",
    "converter": "verbatim"
  },
  "<node_id_1>": { "class_type": "...", "inputs": {...} },
  "<node_id_2>": { "class_type": "...", "inputs": {...} },
  ...
}
```

Capture timestamp via `python -c "from datetime import datetime; print(datetime.now().astimezone().isoformat(timespec='minutes'))"` — LOCAL TZ only, never UTC (per CLAUDE.md durability rules + `feedback_local_timezone_only` memory).

If Step 1b's converter was used, set `"converter": "tools/comfyui_ui_to_api.py@<git-sha-after-T1b-commit>"` instead of `"verbatim"`.

- [ ] **Step 5: Verify lockdown tests now see a real graph + that count is 26.**

```bash
pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v
```

Expected: 4 tests still report `xfailed` (the module-level `pytestmark = pytest.mark.xfail(strict=False, ...)` marker is still in place — that comes off in T2). But internally each test's assertions should PASS against the real graph (xfail-strict=False reports as `xfailed` for both pass-inside-xfail and fail-inside-xfail; the goal here is "no exception leaked", confirmed by the absence of `XPASS` in the output if Python's xfail accounting is strict, or by tinkering with `--runxfail` for one diagnostic run).

For T1 verification, prefer the explicit:

```bash
pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v --runxfail
```

Expected: 4 passed (the `--runxfail` flag ignores the marker; each assertion succeeds because the graph is now real). If `test_graph_shape_api_format` fails at `len(graph) == 26`, the kijai pull is wrong or `_meta` survived — re-check Step 3 and Step 4.

- [ ] **Step 6: Commit T1.**

```bash
git add src/kinoforge/core/config.py \
        tests/core/test_config_graph_meta.py \
        examples/configs/runpod-comfyui-wan.graph.json
# also include tools/comfyui_ui_to_api.py if Step 1b ran
git status --short
pixi run pre-commit run --files src/kinoforge/core/config.py \
                                tests/core/test_config_graph_meta.py \
                                examples/configs/runpod-comfyui-wan.graph.json
git commit -m "$(cat <<'COMMIT'
feat(examples): real kijai-pinned Wan i2v API graph + config _meta strip

T1 of Layer P Task 7 item #3 resume (sub-plan
docs/superpowers/plans/2026-06-02-layer-p-task7-item3-resume.md).

- examples/configs/runpod-comfyui-wan.graph.json: 8-byte placeholder
  replaced with 26-node Wan 2.1 i2v 14B 480P fp8 API-format graph
  pulled verbatim from kijai/ComfyUI-WanVideoWrapper at SHA <PINNED_SHA>
  (example_workflows/<PICKED_FILE>). _meta header carries provenance for
  AC12's offline cross-reference test.
- src/kinoforge/core/config.py: _resolve_spec_graph_file now pops _meta
  from the inlined graph dict so ComfyUI /prompt (which validates every
  top-level key as a node ID) sees a clean dict at submit time, and so
  offline lockdown tests don't need a defensive pop.
- tests/core/test_config_graph_meta.py: 2 tests — load_config strips
  _meta from cfg.spec["graph"]; raw json.loads still exposes _meta for
  AC12 consumers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T1 status → completed.

---

## Task 2: Drop xfail markers + extend EXPECTED_CLASS_TYPES whitelist

**Goal:** Strip the module-level `pytest.mark.xfail` block from `tests/examples/test_runpod_comfyui_wan_graph.py` so the 4 RED lockdown tests transition to plain GREEN; extend `EXPECTED_CLASS_TYPES` ONLY if the pulled graph contains classes not yet in the whitelist (each addition justified in the commit message).

**Files:**
- Modify: `tests/examples/test_runpod_comfyui_wan_graph.py:24-33` (drop `pytestmark = pytest.mark.xfail(...)` block) and `:35-57` (extend `EXPECTED_CLASS_TYPES` if needed)

**Acceptance Criteria:**
- [ ] Module-level `pytestmark = pytest.mark.xfail(strict=False, ...)` block deleted.
- [ ] `import pytest` line still present (kept if T4/T5 use `pytest` fixtures; remove only if no remaining usage).
- [ ] `EXPECTED_CLASS_TYPES` is a superset of the actual class types in the pulled graph (commit message justifies each addition with a one-line rationale per class).
- [ ] All 4 existing tests PASS — `test_graph_shape_api_format`, `test_graph_class_types_within_expected_set`, `test_asset_node_ids_reference_existing_nodes`, `test_prompt_node_ids_is_dict_and_references_existing_nodes`.

**Verify:** `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v` → 4 passed, 0 xfailed, 0 failed.

**Steps:**

- [ ] **Step 1: Determine what class types the pulled graph contains.**

```bash
python -c "
import json
from pathlib import Path
graph = json.loads(Path('examples/configs/runpod-comfyui-wan.graph.json').read_text())
graph.pop('_meta', None)
ctypes = sorted({node['class_type'] for node in graph.values()})
for c in ctypes:
    print(c)
"
```

Compare against the current `EXPECTED_CLASS_TYPES` set (lines 35–57 of the test file). Note any classes in the pulled graph that are NOT in the whitelist. Each unexpected class needs a one-line justification (typical kijai-family class name, or known dependency like `Note`).

- [ ] **Step 2: Drop the xfail block + extend whitelist if needed.**

Remove these exact lines from `tests/examples/test_runpod_comfyui_wan_graph.py`:

```python
pytestmark = pytest.mark.xfail(
    strict=False,
    reason=(
        "Layer P Task 7 item #3 RED lockdown — awaits real workflow API "
        "JSON graph + YAML wiring. See PROGRESS.md 'Layer P Task 7 item "
        "#3' section and "
        "docs/superpowers/specs/2026-06-01-layer-p-task7-item3-"
        "workflow-api-json-design.md."
    ),
)
```

For any unexpected classes from Step 1, add them to `EXPECTED_CLASS_TYPES` in alphabetical order. Example:

```python
EXPECTED_CLASS_TYPES = {
    # ... existing entries, alphabetised ...
    "WanVideoNewClassA",   # kijai sampler stage (introduced upstream <date/PR>)
    "WanVideoNewClassB",   # kijai loader for fp8 quantization (added 2026-XX)
}
```

If `pytest` is no longer used in the file (no `@pytest.fixture`, `pytest.raises`, `pytest.mark.*`, etc.), drop the `import pytest` line too. T4 + T5 will land more tests — check whether their additions need pytest before final pruning.

- [ ] **Step 3: Verify GREEN.**

```bash
pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v
```

Expected: `4 passed in <time>s`. Any failure here means the kijai pull is wrong (graph shape mismatch), the YAML still has placeholder node IDs in `asset_node_ids` (T3 hasn't run yet — expected to fail temporarily until T3 + T2 land together), or `EXPECTED_CLASS_TYPES` is missing a class.

If `test_asset_node_ids_reference_existing_nodes` fails with `node "12" not in graph`, that's expected — T3 hasn't wired the real node ID yet. In that case, defer the commit for T2 until after T3 and commit them together as one "drop xfail + wire YAML to real node IDs" change. Either approach is acceptable; sequential commits are cleaner.

If sequential: temporarily leave the xfail marker in place, run T3, then come back to T2 for the marker drop.

If combined (one commit covering both T2 and T3 logical work): proceed to T3 first, then T2's marker drop, then a single commit.

- [ ] **Step 4: Commit T2 (sequential path).**

```bash
git add tests/examples/test_runpod_comfyui_wan_graph.py
pixi run pre-commit run --files tests/examples/test_runpod_comfyui_wan_graph.py
git commit -m "$(cat <<'COMMIT'
test(examples): drop xfail block — lockdown tests GREEN against real graph

T2 of Layer P Task 7 item #3 resume.

The 4 RED lockdown tests landed by 9d2a9bf and most recently xfailed
by b101104 (Phase 27) now pass against the real kijai-pinned graph
committed in T1. Module-level pytest.mark.xfail(strict=False) block
removed; EXPECTED_CLASS_TYPES extended where the pulled graph
introduced previously-unknown kijai classes (justification per class
in the diff).

Closes spec AC4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T2 status → completed.

---

## Task 3: Wire example YAML — kijai ref bump + boot_timeout_s + asset/prompt node IDs

**Goal:** Edit `examples/configs/runpod-comfyui-wan.yaml` per spec §14.C (4 substantive edits — `spec.graph_file` value is unchanged per the 2026-06-02 polish): kijai `custom_nodes[*].ref` matches the graph's `_meta.source_sha`; add `compute.lifecycle.boot_timeout_s: 1800`; replace `spec.asset_node_ids.init_image` placeholder with the real LoadImage-class node ID; flip `spec.prompt_node_ids` from list to dict pointing at the real positive TextEncode node.

**Files:**
- Modify: `examples/configs/runpod-comfyui-wan.yaml` (specifically `:32` kijai ref, `:60-65` lifecycle block, `:69-72` spec.asset/prompt)

**Acceptance Criteria:**
- [ ] `engine.comfyui.custom_nodes[<kijai>].ref` equals the `_meta.source_sha` value in `examples/configs/runpod-comfyui-wan.graph.json` (string equality).
- [ ] `compute.lifecycle.boot_timeout_s: 1800` present, sibling of `idle_timeout`, `job_timeout`, `max_lifetime`, `budget`; YAML comment explains the Wan cold-boot profile.
- [ ] `spec.asset_node_ids.init_image` value is the real LoadImage-class (or kijai equivalent) node ID from the graph, NOT the placeholder `"12"`.
- [ ] `spec.prompt_node_ids` is a YAML mapping (`positive: <node_id>`), NOT a list; value is the real positive-side TextEncode-class node ID.
- [ ] `spec.graph_file` value still equals `runpod-comfyui-wan.graph.json` (unchanged — resolves correctly via YAML-parent dir).
- [ ] After this task, `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v` → 4 passed.

**Verify:** `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v` → 4 passed; `pixi run python -c "from kinoforge.core.config import load_config; from pathlib import Path; c = load_config(Path('examples/configs/runpod-comfyui-wan.yaml')); print(c.lifecycle().boot_timeout_s)"` → `1800`.

**Steps:**

- [ ] **Step 1: Identify the real node IDs from the pulled graph.**

```bash
python -c "
import json
from pathlib import Path
g = json.loads(Path('examples/configs/runpod-comfyui-wan.graph.json').read_text())
g.pop('_meta', None)
for nid, n in sorted(g.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
    print(f'{nid:>4}  {n[\"class_type\"]}')
"
```

Find:
- The `LoadImage`-class (or `LoadImageFromBase64`, or whatever kijai's i2v graph uses for the initial frame) node ID → goes into `asset_node_ids.init_image`.
- The positive-side TextEncode-class node ID. kijai's Wan workflows typically have two `WanVideoTextEncode` nodes — the one whose `inputs.positive_prompt` (or whose connection topology) feeds the sampler's positive conditioning is the "positive" node. Inspect node-by-node if ambiguous. → goes into `prompt_node_ids.positive`.

Capture both IDs in a scratch note.

- [ ] **Step 2: Read kijai's _meta.source_sha for the ref bump.**

```bash
python -c "
import json
from pathlib import Path
g = json.loads(Path('examples/configs/runpod-comfyui-wan.graph.json').read_text())
print(g['_meta']['source_sha'])
"
```

Capture the SHA.

- [ ] **Step 3: Edit `examples/configs/runpod-comfyui-wan.yaml`.**

Four edits.

**Edit 3a** — kijai `custom_nodes[*].ref` (around line 32):

```yaml
# before
    custom_nodes:
      - git: "https://github.com/kijai/ComfyUI-WanVideoWrapper"
        ref: "088128b224242e110d3906c6750e9a3a348a659b"

# after (substitute the SHA captured in Step 2)
    custom_nodes:
      - git: "https://github.com/kijai/ComfyUI-WanVideoWrapper"
        ref: "<NEW_PINNED_SHA>"
```

If the SHA chosen in T1 matches the existing `088128b…` literally, no edit needed for this line — but T4 still requires the equality test to pass, which it will trivially.

**Edit 3b** — `compute.lifecycle.boot_timeout_s` (insert under the existing `lifecycle:` block, around line 60-65):

```yaml
# before
  lifecycle:
    idle_timeout: 10m
    job_timeout: 15m
    time_buffer: 5m
    max_lifetime: 30m
    budget: 2.0

# after
  lifecycle:
    idle_timeout: 10m
    job_timeout: 15m
    time_buffer: 5m
    max_lifetime: 30m
    budget: 2.0
    # Wan diffusion (~30 GB) + VAE + text encoder cold-boot empirically 5-15 min;
    # 1800 s leaves headroom over Layer Q's 900 s default.
    boot_timeout_s: 1800
```

**Edit 3c** — `spec.asset_node_ids.init_image` (around line 69-70):

```yaml
# before
spec:
  graph_file: runpod-comfyui-wan.graph.json
  asset_node_ids:
    init_image: "12"           # rewritten when exported workflow lands

# after
spec:
  graph_file: runpod-comfyui-wan.graph.json
  asset_node_ids:
    init_image: "<REAL_LOADIMAGE_NODE_ID>"
```

**Edit 3d** — `spec.prompt_node_ids` list→dict (around line 71-72):

```yaml
# before
  prompt_node_ids:
    - "8"

# after
  prompt_node_ids:
    positive: "<REAL_POSITIVE_TEXT_ENCODE_NODE_ID>"
```

Leave `node_overrides: {}` unchanged.

- [ ] **Step 4: Verify lockdown tests pass against the wired YAML.**

```bash
pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v
```

Expected: `4 passed`. If `test_asset_node_ids_reference_existing_nodes` or `test_prompt_node_ids_is_dict_and_references_existing_nodes` fails with `node "<X>" not in graph`, the IDs captured in Step 1 are wrong — re-inspect the graph and fix.

- [ ] **Step 5: Verify config object exposes the new lifecycle field.**

```bash
pixi run python -c "
from pathlib import Path
from kinoforge.core.config import load_config
cfg = load_config(Path('examples/configs/runpod-comfyui-wan.yaml'))
lc = cfg.lifecycle()
print(f'boot_timeout_s={lc.boot_timeout_s}')
"
```

Expected: `boot_timeout_s=1800`.

- [ ] **Step 6: Type-check + lint clean.**

```bash
pixi run mypy . 2>&1 | tail -5
pixi run ruff check examples/configs/runpod-comfyui-wan.yaml || true  # YAML not linted by ruff; check fully via pre-commit instead
pixi run pre-commit run --files examples/configs/runpod-comfyui-wan.yaml
```

- [ ] **Step 7: Commit T3.**

```bash
git add examples/configs/runpod-comfyui-wan.yaml
git commit -m "$(cat <<'COMMIT'
feat(examples): wire runpod-comfyui-wan.yaml to real node IDs + Wan boot timeout

T3 of Layer P Task 7 item #3 resume.

- engine.comfyui.custom_nodes[kijai].ref bumped to <NEW_PINNED_SHA>
  (matches _meta.source_sha in the graph file committed in T1; locks
  the kijai workflow + custom_nodes implementation to the same SHA;
  drives AC12 cross-reference invariant).
- compute.lifecycle.boot_timeout_s: 1800 added — Wan 30 GB cold-boot
  profile-specific override over Layer Q's 900 s default.
- spec.asset_node_ids.init_image: "<REAL_LOADIMAGE_NODE_ID>" replaces
  placeholder "12".
- spec.prompt_node_ids: list ["8"] → dict {positive: <real_id>} —
  closes pre-existing Layer J list-vs-dict bug.

The 4 RED lockdown tests in tests/examples/test_runpod_comfyui_wan_graph.py
now pass (xfail marker dropped in T2).

Closes spec AC2 (extended), helps AC12.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T3 status → completed.

---

## Task 4: Add AC12 kijai SHA cross-reference test

**Goal:** Add `test_kijai_sha_pin_cross_reference` to `tests/examples/test_runpod_comfyui_wan_graph.py` that reads `_meta.source_sha` from the raw graph JSON and `engine.comfyui.custom_nodes[<kijai>].ref` from the YAML via `load_config`, then asserts string equality. Locks AC12.

**Files:**
- Modify: `tests/examples/test_runpod_comfyui_wan_graph.py` (append one new test function)

**Acceptance Criteria:**
- [ ] New function `test_kijai_sha_pin_cross_reference` exists in the file.
- [ ] Reads raw JSON via `json.loads(Path("examples/configs/runpod-comfyui-wan.graph.json").read_text())` (not `load_config`) so it can see `_meta`.
- [ ] Reads YAML via `load_config(YAML_PATH)` and walks `cfg.engine.comfyui.custom_nodes` looking for the kijai entry (URL contains `kijai/ComfyUI-WanVideoWrapper`).
- [ ] Asserts `_meta["source_sha"] == kijai_entry.ref` with a failure message identifying both values.
- [ ] Test passes against the current committed state (graph + YAML).

**Verify:** `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py::test_kijai_sha_pin_cross_reference -v` → 1 passed.

**Steps:**

- [ ] **Step 1: Write the test (RED first — confirm it fails with a wrong SHA, then make it pass).**

To force RED for verification, temporarily mutate the YAML's `ref:` to something obviously wrong (e.g. `"0000000000000000000000000000000000000000"`). Then add the test. Run. Confirm FAIL. Revert YAML. Run again. Confirm PASS.

Skip the RED-confirmation if confident — the assertion shape (string equality with clear failure message) is verified-by-construction. Sub-plan executor's choice.

Append to `tests/examples/test_runpod_comfyui_wan_graph.py`:

```python
import json  # add to existing imports if not already present

GRAPH_PATH = Path("examples/configs/runpod-comfyui-wan.graph.json")
KIJAI_REPO_HINT = "kijai/ComfyUI-WanVideoWrapper"


def test_kijai_sha_pin_cross_reference() -> None:
    """The kijai custom_nodes ref in the YAML must match _meta.source_sha in the graph file.

    Both are pinned at the same SHA to lock the workflow JSON to the
    custom_nodes implementation that produced it. Drift between them
    is silent at runtime but corrupts the offline lockdown — caught here.
    """
    # Read graph JSON raw so we see _meta (config-load strips it).
    raw_graph = json.loads(GRAPH_PATH.read_text())
    meta = raw_graph.get("_meta")
    assert meta is not None, (
        f"{GRAPH_PATH} missing _meta header (set by T1)"
    )
    graph_sha = meta.get("source_sha")
    assert isinstance(graph_sha, str) and len(graph_sha) == 40, (
        f"_meta.source_sha must be 40-char SHA, got {graph_sha!r}"
    )

    # Read YAML via load_config; walk custom_nodes for the kijai entry.
    cfg = load_config(YAML_PATH)
    custom_nodes = cfg.engine.comfyui.custom_nodes
    kijai_entries = [
        cn for cn in custom_nodes if KIJAI_REPO_HINT in cn.git
    ]
    assert len(kijai_entries) == 1, (
        f"expected exactly one kijai entry in custom_nodes, got "
        f"{[cn.git for cn in custom_nodes]}"
    )
    yaml_sha = kijai_entries[0].ref

    assert graph_sha == yaml_sha, (
        f"kijai SHA drift between graph _meta ({graph_sha!r}) and YAML "
        f"custom_nodes[kijai].ref ({yaml_sha!r}); rerun graph capture or "
        f"bump YAML"
    )
```

NOTE: this assumes `cfg.engine.comfyui.custom_nodes[i].git` and `.ref` are accessible attributes on the pydantic model. If `custom_nodes` is a list of dicts rather than typed models, switch to `cn["git"]` / `cn["ref"]` or `cn.get("git")`. Check the actual `EngineConfig.comfyui` schema in `src/kinoforge/core/config.py` if there's any doubt — should be ~30 LOC of inspection.

- [ ] **Step 2: Verify GREEN.**

```bash
pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py::test_kijai_sha_pin_cross_reference -v
```

Expected: 1 passed. Failure means either (a) `_meta.source_sha` is wrong in the graph file (re-check T1), (b) the YAML `custom_nodes[kijai].ref` doesn't match (re-check T3), or (c) the attribute access pattern is wrong (use dict access).

- [ ] **Step 3: Type-check + commit.**

```bash
pixi run mypy tests/examples/test_runpod_comfyui_wan_graph.py 2>&1 | tail -5
pixi run pre-commit run --files tests/examples/test_runpod_comfyui_wan_graph.py
git add tests/examples/test_runpod_comfyui_wan_graph.py
git commit -m "$(cat <<'COMMIT'
test(examples): AC12 kijai SHA cross-reference lockdown

T4 of Layer P Task 7 item #3 resume.

test_kijai_sha_pin_cross_reference reads _meta.source_sha from the
raw graph JSON and engine.comfyui.custom_nodes[kijai].ref from the
YAML via load_config; asserts equality. Drift between the workflow
JSON's provenance and the pinned custom_nodes implementation is
silent at runtime (the graph would technically still POST), but the
workflow is only guaranteed to render correctly against the kijai
nodes at the same SHA.

Closes spec AC12.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T4 status → completed.

---

## Task 5: Add AC13 env_required lockdown test

**Goal:** Add `test_yaml_env_required_locked_to_hf_token` to `tests/examples/test_runpod_comfyui_wan_graph.py` that loads the YAML via `Config`, calls `ComfyUIEngine().render_provision(cfg.model_dump())`, and asserts `rendered.env_required == ["HF_TOKEN"]` (exact list). Locks AC13. Catches future YAML edits that silently drop or expand the cred-declaration surface.

**Files:**
- Modify: `tests/examples/test_runpod_comfyui_wan_graph.py` (append one new test function + necessary imports)

**Acceptance Criteria:**
- [ ] New function `test_yaml_env_required_locked_to_hf_token` exists in the file.
- [ ] Imports `kinoforge._adapters` for side-effect registration (HF source must be registered for `source_for_ref` to resolve).
- [ ] Imports `ComfyUIEngine` from `kinoforge.engines.comfyui`.
- [ ] Calls `engine.render_provision(cfg.model_dump())` — model_dump produces the engine-facing dict.
- [ ] Asserts `rendered.env_required == ["HF_TOKEN"]` (exact list, not subset).
- [ ] Test passes against the current committed YAML + graph state.

**Verify:** `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py::test_yaml_env_required_locked_to_hf_token -v` → 1 passed.

**Steps:**

- [ ] **Step 1: Write the test.**

Append to `tests/examples/test_runpod_comfyui_wan_graph.py`:

```python
import kinoforge._adapters  # noqa: F401 — side-effect: register HF/RunPod/ComfyUI adapters
from kinoforge.engines.comfyui import ComfyUIEngine


def test_yaml_env_required_locked_to_hf_token() -> None:
    """The example YAML must declare exactly HF_TOKEN as env_required.

    All three model entries pull from hf:Kijai/WanVideo_comfy:* refs;
    HuggingFaceSource attaches `Authorization: Bearer $HF_TOKEN` headers,
    which render_provision converts to env_required=["HF_TOKEN"]. Any
    future YAML edit that drops an HF-gated model OR adds a non-HF
    cred-bearing model would silently change this list; catch it here.
    """
    cfg = load_config(YAML_PATH)
    engine = ComfyUIEngine()
    rendered = engine.render_provision(cfg.model_dump())
    assert rendered.env_required == ["HF_TOKEN"], (
        f"YAML env_required drift: expected ['HF_TOKEN'], got "
        f"{rendered.env_required!r}"
    )
```

- [ ] **Step 2: Verify GREEN.**

```bash
pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py::test_yaml_env_required_locked_to_hf_token -v
```

Expected: 1 passed.

Failure modes:
- `UnknownAdapter: no model source handles ref: 'hf:Kijai/...'` → `import kinoforge._adapters` is missing or not effective; double-check ordering at file top.
- Asserts fail with `env_required` containing more/fewer entries → check `_extract_env_var` logic in `src/kinoforge/engines/comfyui/__init__.py` and HuggingFaceSource header construction.
- `render_provision` raises on a missing cfg key → check `cfg.model_dump()` vs `cfg.model_dump_json()` shape; render_provision reads `cfg["engine"]["comfyui"]` and `cfg["models"]`.

- [ ] **Step 3: Run full lockdown-file suite to confirm no regression on T2/T4.**

```bash
pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v
```

Expected: 6 passed (4 from T2 + 1 from T4 + 1 from T5).

- [ ] **Step 4: Type-check + commit.**

```bash
pixi run mypy tests/examples/test_runpod_comfyui_wan_graph.py 2>&1 | tail -5
pixi run pre-commit run --files tests/examples/test_runpod_comfyui_wan_graph.py
git add tests/examples/test_runpod_comfyui_wan_graph.py
git commit -m "$(cat <<'COMMIT'
test(examples): AC13 env_required surfaced from YAML lockdown

T5 of Layer P Task 7 item #3 resume.

test_yaml_env_required_locked_to_hf_token loads the example YAML,
runs ComfyUIEngine.render_provision(cfg.model_dump()), and asserts
rendered.env_required == ["HF_TOKEN"] exactly. The three HF-gated
model refs in the YAML each carry Authorization: Bearer $HF_TOKEN
headers via HuggingFaceSource; render_provision deduplicates them
into a single-element env_required list. Future YAML edits that
silently change the cred-declaration shape (drop an HF model,
add a non-HF cred-bearing model) fail this test.

Closes spec AC13.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T5 status → completed.

---

## Task 6: Live cold-boot via Layer Q surface + first green MP4 + absorb item #4 bug-catches

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Run the live RunPod smoke against the kijai-pinned graph + wired YAML; produce a non-empty MP4 with valid `ftyp` magic; absorb up to 5 item #4 bug-catch classes (multipart shape, requirements install path, /history outputs key, marker registration under warm-tag, text_encoder routing) as additional in-task forward-fix commits with offline regression tests against captured fixtures (Layer N pattern); stay under the $1.50 AC6 cost cap.

**Files:**
- Read-only: `tests/live/test_comfyui_wan_live.py` (existing smoke, already Layer-Q-compatible per recon — runs `generate(instance=None)` cold-boot path).
- May modify (per bug-catch): `src/kinoforge/engines/comfyui/__init__.py`, `src/kinoforge/engines/comfyui/nodes.py`, `src/kinoforge/core/provision_state.py`, `src/kinoforge/sources/huggingface/__init__.py`, `src/kinoforge/engines/comfyui/__init__.py` (`TARGET_TO_SUBDIR`). Each touched file gets its own commit with a regression test.
- May create (per bug-catch): `tests/engines/fixtures/comfyui/<fixture_name>.json` (captured shape via `KINOFORGE_SAVE_FIXTURES=1` from the smoke).
- Read-only at smoke time: `examples/configs/runpod-comfyui-wan.yaml`, `examples/configs/runpod-comfyui-wan.graph.json` (already finalized by T1–T5).

**Acceptance Criteria:**
- [ ] Live smoke runs with `KINOFORGE_LIVE_TESTS=1` + `RUNPOD_API_KEY` + `RUNPOD_TERMINATE_KEY` + `HF_TOKEN` env vars all set (env-gate-only auth per §14.G).
- [ ] Smoke completes through Layer Q's `render_provision` → `validate env_required` → `create_instance` → `engine.provision` (with `wait_for_ready` against `/system_stats`, `boot_timeout_s=1800`) → `backend.submit` → `/history` poll → MP4 artifact.
- [ ] MP4 lands at `output/<run_id>/<published_name>.mp4` (Layer O sink); file size > 0; first 12 bytes contain one of `_MP4_FTYP_PREFIXES` (typical brands: `ftypisom`, `ftypiso5`, `ftypiso6`, `ftypmp42`).
- [ ] Each bug-catch (zero or more, capped at 5 classes) lands as its own commit: production-code edit + offline regression test against a fixture captured via `KINOFORGE_SAVE_FIXTURES=1`. The commit subject names the bug class.
- [ ] Total live spend ≤ $1.50 (AC6). RunPod pod cost recorded by the smoke's lifecycle handler; sub-plan executor reads from the smoke's stdout or from `tests/live/test_comfyui_wan_live.py`'s final-cost print.
- [ ] No pod left running at task close — smoke's finally-clause destroys via `RUNPOD_TERMINATE_KEY`. Independently verify via `pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml` returning zero rows.
- [ ] If a 6th distinct bug class blocks green MP4, ABORT the task per §14.F: commit what's landed, leave the AC5 + AC6 criteria UNCHECKED, record the abort in the closure block (T7), do not silently expand scope.

**Verify:**
1. `pixi run pytest tests/live/test_comfyui_wan_live.py -v -s` → final test status PASSED; smoke stdout contains an MP4 path under `output/`.
2. `ls -la output/<run_id>/` → MP4 file present with non-zero size.
3. `head -c 12 output/<run_id>/<name>.mp4 | python -c "import sys; data = sys.stdin.buffer.read(); print('ftyp' in data, data[:12].hex())"` → `True ...`.
4. `pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml` → zero pods running.
5. Smoke stdout cost line → ≤ $1.50.

**Steps:**

- [ ] **Step 1: Pre-flight env check.**

```bash
echo "KINOFORGE_LIVE_TESTS=${KINOFORGE_LIVE_TESTS:-<unset>}"
echo "RUNPOD_API_KEY=${RUNPOD_API_KEY:+<set>}"
echo "RUNPOD_TERMINATE_KEY=${RUNPOD_TERMINATE_KEY:+<set>}"
echo "HF_TOKEN=${HF_TOKEN:+<set>}"
```

All four must show `1` / `<set>`. If any is `<unset>`, the smoke auto-skips (Layer N convention, `tests/live/test_comfyui_wan_live.py:39-49`). Setting them IS the consent gate per §14.G — no interactive checkpoint here.

If you're unsure whether you've authorized this run, STOP and ask the user — but do not introduce a USER-GATE prompt mid-task. The env-gate is canonical.

- [ ] **Step 2: Pre-flight offline gate — confirm T1–T5 green.**

```bash
pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py tests/core/test_config_graph_meta.py -v
```

Expected: 8 passed (4 from T2, 1 from T4, 1 from T5, 2 from T1). Any failure here means Phase A is broken; do not spend live money — fix offline first.

- [ ] **Step 3: Pre-flight ledger check — confirm no leftover paid pods.**

```bash
pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml
```

Expected: zero rows. Any leftover row from a prior aborted run must be destroyed first:

```bash
pixi run python -m kinoforge destroy <pod_id> --config examples/configs/runpod-comfyui-wan.yaml
```

- [ ] **Step 4: First live attempt — cold-boot, capture fixtures, expect to surface bugs.**

```bash
export KINOFORGE_SAVE_FIXTURES=1
export KINOFORGE_LIVE_KEEP_POD=1   # so post-failure iteration reuses the warm pod
pixi run pytest tests/live/test_comfyui_wan_live.py -v -s 2>&1 | tee /tmp/smoke-attempt-1.log
```

Possible outcomes:

**A) PASS first attempt.** MP4 produced; ACs met. Jump to Step 7.

**B) FAIL with one of the 5 absorbed bug classes.** Go to Step 5 (bug-catch loop).

**C) FAIL with a 6th distinct class.** Per §14.F: abort task. Skip to Step 7's abort-close path.

**D) FAIL with infra (capacity, auth, network).** Iterate per spec §8 failure-table. Capacity is already absorbed by item #1 offer-retry. Auth/network → escalate to user.

The pod stays warm (`KINOFORGE_LIVE_KEEP_POD=1`); subsequent iterations short-circuit boot. Cost accumulates against AC6's $1.50.

- [ ] **Step 5: Bug-catch loop (repeat per class, max 5).**

For each bug surfaced in Step 4 or in a follow-up attempt:

1. **Categorize.** Match against the 5 absorbed classes:
   - Multipart shape (`/upload/image` body in `ComfyUIBackend.submit` — `src/kinoforge/engines/comfyui/__init__.py` around line 450-470).
   - Requirements install path (custom_nodes `requirements.txt` resolution — `src/kinoforge/engines/comfyui/nodes.py` or render_provision logic).
   - `/history` outputs key (`ComfyUIBackend.result` extraction — videos/gifs/images key under `outputs[<node>]`).
   - Marker registration under warm-tag (`src/kinoforge/core/provision_state.py` — checks whether previous provision marker survives reboot).
   - text_encoder routing (`TARGET_TO_SUBDIR` in ComfyUI render_provision OR `HuggingFaceSource` resolution for `text_encoders` target).

   If the bug doesn't match any of the five → STOP. Per §14.F: 6th class aborts task. Commit what's landed; jump to Step 7's abort-close path.

2. **Capture the fixture.** The smoke writes fixtures under `tests/engines/fixtures/comfyui/` when `KINOFORGE_SAVE_FIXTURES=1` is set. Find the captured payload that exhibits the bug:

   ```bash
   ls -la tests/engines/fixtures/comfyui/ | head -20
   git status --short tests/engines/fixtures/comfyui/
   ```

   Decide which captured fixture(s) belong to the bug-catch; one fixture file per bug-catch.

3. **Write the failing offline regression test.** Layer N pattern: load the fixture, call the production code path that consumes it, assert the new correct behaviour. The test FAILS before the production fix.

   File: `tests/engines/test_comfyui.py` (existing — append new test) OR `tests/engines/test_comfyui_bug<N>.py` (new) per executor's judgment.

4. **Write the production fix.** Minimal change in `src/kinoforge/engines/comfyui/__init__.py` (or nodes.py / provision_state.py / huggingface source) that makes the regression test pass.

5. **Run the regression test offline.** Confirm GREEN.

   ```bash
   pixi run pytest tests/engines/test_comfyui.py -v 2>&1 | tail -10
   ```

6. **Re-run the live smoke.** With the warm pod still up, the next attempt should clear the just-fixed bug:

   ```bash
   pixi run pytest tests/live/test_comfyui_wan_live.py -v -s 2>&1 | tee /tmp/smoke-attempt-N.log
   ```

7. **Commit the bug-catch atomically.**

   ```bash
   git add src/kinoforge/engines/comfyui/__init__.py \
           tests/engines/test_comfyui.py \
           tests/engines/fixtures/comfyui/<new_fixture>.json
   pixi run pre-commit run --files <staged-files>
   git commit -m "$(cat <<'COMMIT'
   fix(engines/comfyui): <one-line bug class summary>

   T6 bug-catch <N>/5 from Layer P Task 7 item #3 resume live smoke.
   <2-3 lines describing root cause + fix shape>
   Regression test against fixture <fixture_name>.json captured via
   KINOFORGE_SAVE_FIXTURES=1 from the warm pod.

   Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
   COMMIT
   )"
   ```

   Repeat Step 5 for each subsequent bug. Track total absorbed count against the 5-class cap.

- [ ] **Step 6: First green MP4 — capture evidence.**

When the smoke completes PASSED:

```bash
# Extract MP4 path + size from smoke output.
grep -E "(\.mp4|first artifact|output/)" /tmp/smoke-attempt-N.log | tail -5
# Independently verify magic bytes.
MP4_PATH=$(find output -name "*.mp4" -newer /tmp/smoke-attempt-1.log | head -1)
ls -la "$MP4_PATH"
head -c 12 "$MP4_PATH" | xxd
```

Expected: a 4–8 MB MP4 file at `output/<run_id>/<published_name>.mp4` with `ftyp` brand in the first 12 bytes.

Capture: MP4 path, file size, `ftyp` brand bytes (hex), run_id, capability_key (from smoke stdout), git SHA at moment of capture, total live cost, bug-catch list (zero or more). All goes into PROGRESS in T7.

- [ ] **Step 7: Destroy any remaining warm pod + close T6.**

```bash
# Independent destroy — belt + suspenders even though smoke's finally already did it.
unset KINOFORGE_LIVE_KEEP_POD
pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml
# if any pods remain:
# pixi run python -m kinoforge destroy <pod_id> --config examples/configs/runpod-comfyui-wan.yaml
```

If the task succeeded (PASS path):
- Update tasks.json: T6 status → completed.
- Capture the evidence package described in Step 6 for T7's PROGRESS write.

If the task aborted (6th bug class OR cost cap hit OR infra-blocked):
- Commit any landed bug-catches with full subject + body.
- Update tasks.json: T6 status → completed (with abort note in description).
- Capture: which bug-catches landed, what blocked, current cost spend, current MP4 state (none, partial, etc.). All goes into a "partial-close" variant of T7's PROGRESS block.

No commit on T6 itself beyond the bug-catch commits — the live smoke run doesn't produce code artifacts; its output is the MP4 (not committed; lives under `output/` which is `.gitignore`'d per `.gitignore` Layer O) + the captured fixtures (committed as part of bug-catch commits).

The captured fixtures THAT DIDN'T trigger a bug-catch (i.e. the happy-path /prompt, /history, /upload/image fixtures from the green run) belong to Layer P T8 / T9. Stage them under `tests/engines/fixtures/comfyui/` but do NOT commit in this sub-plan; T8 consumes them. Optionally commit a manifest pointing to their existence if the executor prefers a clean handoff state.

Update tasks.json: T6 status → completed.

---

## Task 7: PROGRESS closure block + tasks.json final sync

**Goal:** Append the item #3 closure block to `PROGRESS.md` following the item #1 / item #2 template; update the "Pending Task 7 work" section (mark item #3 CLOSED, note absorbed item #4); update the "Single next action" block to point at Layer P T8; final pre-commit + pytest gate; final tasks.json sync.

**Files:**
- Modify: `PROGRESS.md` (append closure block; update "Pending Task 7 work" + "Single next action")
- Modify: `docs/superpowers/plans/2026-06-02-layer-p-task7-item3-resume.md.tasks.json` (final state — all tasks completed or abort-noted)

**Acceptance Criteria:**
- [ ] PROGRESS closure block contains: sub-spec path + commit SHAs; sub-plan path + commit SHAs; atomic commit list (T1–T7 + any bug-catch commits); MP4 evidence (path, bytes, ftyp brand, run_id, capability_key, git SHA at capture, live cost) OR abort details if applicable; bug-catch list (zero or more entries); test count delta; key design decisions block mirroring item #1 / #2 shape.
- [ ] PROGRESS "Pending Task 7 work" item #3 marked CLOSED with link to closure block.
- [ ] PROGRESS "Single next action" updated to point at Layer P T8 (refactor 23 `tests/engines/test_comfyui.py` tests onto captured fixtures).
- [ ] `pixi run pytest` total count reflects: baseline 979 + 2 new (T1's `_meta` strip tests) + 2 new (T4 + T5 lockdown additions) + N bug-catch regression tests = 983 + N (where N is 0–5). The 4 xfailed tests are now plain pass.
- [ ] `pixi run pre-commit run --all-files` clean.
- [ ] tasks.json on disk matches the in-memory state.

**Verify:**
1. `pixi run pytest 2>&1 | tail -5` → output line of shape `983 passed, 3 skipped` (+ N regression tests if any landed; no xfailed; no failed).
2. `pixi run pre-commit run --all-files 2>&1 | tail -10` → all hooks Passed.
3. `cat PROGRESS.md | grep -A2 "Layer P Task 7 item #3"` → closure block headers visible.
4. Cross-read PROGRESS "Single next action" → mentions Layer P T8 with the refactor-23-tests description.

**Steps:**

- [ ] **Step 1: Run the full offline suite — confirm count + cleanliness.**

```bash
pixi run pytest 2>&1 | tail -3
```

Expected: pass count = 979 (Phase 27 baseline) + 2 (T1's `_meta` strip tests) + 2 (T4 + T5) + N (bug-catch regressions, 0–5) = 983 + N. Skip count 3. Xfail count 0 (the 4 RED scaffold tests are now plain pass).

If counts don't match, investigate before committing PROGRESS — a missing or extra test is a real signal.

```bash
pixi run pre-commit run --all-files 2>&1 | tail -10
```

Expected: every hook reports `Passed` or `Skipped`. No `Failed`.

- [ ] **Step 2: Compose the PROGRESS closure block.**

Use this template, filling placeholders from T1–T6 evidence:

```markdown
**Layer P Task 7 item #3 (workflow API JSON + first green MP4) — ✅ CLOSED 2026-06-XX at HEAD `<final-sha>`.**

Phase 27 left item #3 xfailed pending resume against Layer Q HEAD. Sub-spec (initial 2026-06-01, amended 2026-06-02) + sub-plan + 7 atomic task commits (+ N bug-catch commits inline in T6) closed it.

- Sub-spec: `docs/superpowers/specs/2026-06-01-layer-p-task7-item3-workflow-api-json-design.md` (amend `23b1501` + polish `744575c`)
- Sub-plan: `docs/superpowers/plans/2026-06-02-layer-p-task7-item3-resume.md` (+ `.tasks.json`) (initial `<sub-plan-sha>`)
- T1 `<sha>` — `feat(examples): real kijai-pinned Wan i2v API graph + config _meta strip` (graph file + `_resolve_spec_graph_file` strip + 2 unit tests)
- T2 `<sha>` — `test(examples): drop xfail block — lockdown tests GREEN against real graph`
- T3 `<sha>` — `feat(examples): wire runpod-comfyui-wan.yaml to real node IDs + Wan boot timeout`
- T4 `<sha>` — `test(examples): AC12 kijai SHA cross-reference lockdown`
- T5 `<sha>` — `test(examples): AC13 env_required surfaced from YAML lockdown`
- T6 bug-catch `<sha>` × N (zero or more — see per-commit subjects for class)
- T7 (this commit) — PROGRESS closure block + tasks.json sync

**First real artifact (ComfyUI + Wan on RunPod):** `<output/<run_id>/<filename>.mp4>` — `<bytes>` bytes, MP4 (`ftyp <brand>`), produced by ComfyUIEngine + RunPodProvider via `examples/configs/runpod-comfyui-wan.yaml` (capability_key `<hash>`, git SHA `<sha>` at smoke time). Cost ≈ $`<cost>`. Run completed at `<local-tz timestamp>`.

**Live-smoke bug catches integrated (`<N>` production fixes):**

`<one numbered list entry per bug-catch, each citing its commit SHA + one-line summary>`

**Key design decisions:**

- Light amendment in place to the prior item #3 sub-spec rather than fresh rewrite (Q1 in 2026-06-02 brainstorm) — Layer Q surface is final and stable; original 11 ACs survive in shape.
- Kijai pinned-pull (Q2) for the 26-node API JSON — hermetic, zero live spend for graph capture, SHA-pinned alongside `custom_nodes` ref already in YAML. `_meta` provenance header carries `source_repo`/`source_sha`/`source_path`/`captured_at_local`/`format`/`converter`; AC12 cross-references the SHA at test time. `_meta` is stripped at config-load time by `core/config._resolve_spec_graph_file` so runtime and offline tests see a `_meta`-free graph.
- `prompt_node_ids` exposes `positive` only (Q3) — negative is graph-baked default; non-breaking expansion later.
- `compute.lifecycle.boot_timeout_s: 1800` in the example YAML only (Q4) — Layer Q's 900 s default unchanged for engines/workflows with lighter cold-boot.
- Item #4 unknowns absorbed at AC6's $1.50 cap (Q5) — `<N>` of 5 absorbed classes triggered during the live run.
- Env-gate-only authorization (Q6) — `KINOFORGE_LIVE_TESTS=1` + `RUNPOD_API_KEY` + `RUNPOD_TERMINATE_KEY` + `HF_TOKEN` together IS consent. The 3 interactive USER-GATE checkpoints in the original sub-spec §8.3 dropped per §14.G.

**Test count:** baseline 979 + 2 (T1 unit tests) + 2 (T4 + T5 lockdown additions) + `<N>` (bug-catch regressions) = `<final>` passed + 3 skipped + 0 xfailed. The 4 prior xfails transitioned to plain pass; no new xfail/skip introduced.

**Cost burn (item #3 resume):** $`<cost>` against $1.50 AC6 cap. Cumulative Layer P spend: $0.013 (Layer P prior) + $0.25 (item #3 earlier attempt — see PROGRESS:280) + $`<cost>` = $`<total>` / $1.99 cap. `<%>`% budget remaining.

**Unblocks Layer P T8** (refactor 23 `tests/engines/test_comfyui.py` tests onto fixtures captured during this sub-plan's live run) + Layer P T9 (3 ComfyUI shape-lockdown tests) + Layer P T10 (Layer P closure + `--no-ff` merge).
```

- [ ] **Step 3: Update PROGRESS "Pending Task 7 work" block.**

In `PROGRESS.md` find the "Pending Task 7 work" section (currently around lines 375-389 per recon). Update item #3 entry:

```markdown
4. ~~Workflow API JSON conversion + first green MP4 (item #3 resume).~~ **CLOSED** 2026-06-XX. See item #3 closure block above. Item #4 absorbed; `<N>` bug-catch classes integrated.
```

Update item #4 entry similarly to note absorption.

Pending items 5 (Layer P T8/T9/T10) become the next live work — promoted in the Single-Next-Action block below.

- [ ] **Step 4: Update PROGRESS "Single next action" block.**

The current block (lines 150-178 per recon) covered Phase 27 closure (CI green recovery). Replace with:

```markdown
## Single next action
**Layer P T8 — refactor `tests/engines/test_comfyui.py` onto captured fixtures.**

23 tests in `tests/engines/test_comfyui.py` currently use fake HTTP seams. Item #3's live smoke (now closed at HEAD `<sha>`) committed real-shape ComfyUI HTTP fixtures under `tests/engines/fixtures/comfyui/` (`<list of fixture files>`). T8 lifts each of the 23 tests onto a fixture-loaded seam — Layer N pattern. Net: tests exercise real ComfyUI server shape; offline + hermetic; surface for shape lockdowns in Layer P T9.

Spec: not yet written (Layer P plan, original Task 8 description).
Plan: not yet written.
Estimated scope: ~150 LOC test refactor + 1 conftest fixture-loading helper; offline-only; no new live spend.
```

- [ ] **Step 5: Sync tasks.json final state.**

```bash
python - <<'PY'
import json
from pathlib import Path
from datetime import datetime

p = Path("docs/superpowers/plans/2026-06-02-layer-p-task7-item3-resume.md.tasks.json")
data = json.loads(p.read_text())
# Mark all 7 tasks completed (T6 may be partial-close — check status from prior runs)
for t in data["tasks"]:
    if t["status"] == "in_progress":
        t["status"] = "completed"
data["lastUpdated"] = datetime.now().astimezone().isoformat(timespec="minutes")
p.write_text(json.dumps(data, indent=2))
print("synced")
PY
```

- [ ] **Step 6: Final gate — pre-commit + pytest.**

```bash
pixi run pre-commit run --all-files 2>&1 | tail -20
pixi run pytest 2>&1 | tail -5
```

Both must be clean. If pre-commit auto-fixes a file (whitespace, EOF), re-stage and re-run.

- [ ] **Step 7: Commit T7.**

```bash
git add PROGRESS.md docs/superpowers/plans/2026-06-02-layer-p-task7-item3-resume.md.tasks.json
git commit -m "$(cat <<'COMMIT'
docs(progress): Layer P Task 7 item #3 — closure snapshot

T7 of Layer P Task 7 item #3 resume.

Closure block per item #1 / item #2 / item #3 (prior attempt) shape:
sub-spec + sub-plan refs, atomic commit list T1–T7 + N bug-catch
commits, MP4 evidence (path / bytes / ftyp brand / run_id /
capability_key / git SHA / cost / local-tz timestamp), key design
decisions block, test count delta, cost burn vs $1.50 AC6 cap +
$1.99 Layer P cap, T8/T9/T10 unblocking.

PROGRESS "Pending Task 7 work" item #3 marked CLOSED; item #4
absorbed. "Single next action" promoted to Layer P T8 (test_comfyui.py
refactor onto captured fixtures).

tasks.json synced — all 7 sub-plan tasks completed.

Closes spec AC10. Resume item #3 ships.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T7 status → completed.

---

## Post-plan checklist (sub-plan executor)

- [ ] Local main is 3+ commits ahead of `origin/main` at sub-plan close (depending on push state at execute time). Push when convenient outside the container: `git push origin main`.
- [ ] No Layer P merge in this sub-plan — Layer P stays open until T8/T9/T10 ship. Per §14.H the sub-plan closure block APPENDS to Layer P's existing on-`main` history; no `--no-ff` merge step.
- [ ] All 4 prior xfails are GREEN.
- [ ] PROGRESS "Single next action" points at Layer P T8.
- [ ] No paid pods running (`pixi run python -m kinoforge list ...` returns zero rows).
- [ ] Spec self-review re-confirmed: zero TBDs in this plan; all code blocks exact; all file paths exact; all verify commands exact.
