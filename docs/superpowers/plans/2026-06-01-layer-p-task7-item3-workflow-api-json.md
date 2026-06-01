# Layer P Task 7 item #3 — Real workflow API JSON Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 166-byte placeholder `runpod-comfyui-wan.graph.json` with a real hand-authored ComfyUI API-format graph for Wan 2.1 i2v 14B 480P fp8, wire the example YAML to real node IDs, lock the graph shape with a unit test, and prove the stack end-to-end with a green MP4 on a real warm RunPod pod. Absorbs item #4 (live-iteration unknowns) into the same live session.

**Architecture:** Three phases. Phase A is offline lockdown-test scaffolding (RED tests). Phase B is one continuous live session on a single warm pod: capture `/object_info`, hand-author the graph against captured schema, parse-validate via `/prompt`, wire YAML, run full Wan render. Phase C is offline close-out (fixture-lockdown test + PROGRESS evidence). Item #1 offer-retry + item #2 warm-reuse kwargs are already shipped — this plan re-uses them, adds no orchestrator code.

**Tech Stack:** `pixi run pytest` for testing, ComfyUI HTTP API on RunPod proxy URL, `/object_info` + `/prompt` + `/history` endpoints, kijai upstream desktop workflow + custom-node Python `INPUT_TYPES` as schema source, semantic string node IDs in the committed API JSON.

**Sub-spec:** `docs/superpowers/specs/2026-06-01-layer-p-task7-item3-workflow-api-json-design.md` (commit `e2f25df`).

---

## File Structure

Files this plan creates or modifies:

| Path | Role | Created by |
|---|---|---|
| `examples/configs/runpod-comfyui-wan.graph.json` | 26-node ComfyUI API-format graph, semantic IDs, hand-authored from kijai desktop + `/object_info` | Task 3 |
| `examples/configs/runpod-comfyui-wan.yaml` | Real `asset_node_ids` / `prompt_node_ids` (dict) / `node_overrides` wiring | Task 4 |
| `tests/engines/fixtures/comfyui/object_info_wan21.json` | Captured ComfyUI `/object_info` dump | Task 2 |
| `tests/examples/test_runpod_comfyui_wan_graph.py` | Lockdown unit test (4 tests) | Task 1 |
| `tests/engines/test_comfyui.py` | +1 test loading the `/object_info` fixture | Task 6 |
| `PROGRESS.md` | Item #3 closure row | Task 7 |

Files this plan may modify during Task 5 live shake-out (zero or more, depends on bug-catch surface):

| Path | Bug class |
|---|---|
| `src/kinoforge/engines/comfyui/__init__.py` | `/upload/image` multipart shape, `/history` outputs key extraction |
| `src/kinoforge/engines/comfyui/nodes.py` | Custom-node `requirements.txt` install path on `runpod/pytorch:2.4.0` |
| `src/kinoforge/core/provision_state.py` | Marker registration under warm-tag-discovery pod |
| `src/kinoforge/sources/huggingface/__init__.py` or ComfyUI `TARGET_TO_SUBDIR` | text_encoder target routing |

---

## Task 1: Lockdown test scaffold + prompt_node_ids type regression (offline, RED)

**Goal:** Land four lockdown tests + one type-regression test as RED. They go GREEN at the end of Task 4 when the real graph + YAML edits land. This task is fully offline — no pod, no cost.

**Files:**
- Create: `tests/examples/test_runpod_comfyui_wan_graph.py`

**Acceptance Criteria:**
- [ ] `tests/examples/test_runpod_comfyui_wan_graph.py` exists with 4 tests:
  `test_graph_shape_api_format`, `test_graph_class_types_within_expected_set`,
  `test_asset_node_ids_reference_existing_nodes`,
  `test_prompt_node_ids_is_dict_and_references_existing_nodes`
- [ ] `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v` reports 4 FAILED.
- [ ] `pixi run pre-commit run --files tests/examples/test_runpod_comfyui_wan_graph.py` clean.

**Verify:** `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v 2>&1 | tail -10` → 4 failed.

**Steps:**

- [ ] **Step 1: Create the lockdown test file**

```python
# tests/examples/test_runpod_comfyui_wan_graph.py
"""Lockdown tests for examples/configs/runpod-comfyui-wan.graph.json.

These tests assert the committed ComfyUI API-format graph is structurally
valid AND that the example YAML's asset_node_ids / prompt_node_ids dicts
reference node IDs that actually exist in the graph. Run offline — no pod.

The test_prompt_node_ids_is_dict_and_references_existing_nodes case also
locks down the pre-existing list-vs-dict type bug in the YAML
(prompt_node_ids was ``["8"]`` — a list — but Layer J's
``engines.comfyui`` calls ``.items()`` on the value and would crash at
runtime).
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.config import load_config

YAML_PATH = Path("examples/configs/runpod-comfyui-wan.yaml")
EXPECTED_NODE_COUNT = 26
EXPECTED_CLASS_TYPES = {
    "CLIPLoader",
    "CLIPTextEncode",
    "CLIPVisionLoader",
    "ImageResizeKJv2",
    "LoadImage",
    "LoadWanVideoT5TextEncoder",
    "Note",
    "VHS_VideoCombine",
    "WanVideoBlockSwap",
    "WanVideoClipVisionEncode",
    "WanVideoDecode",
    "WanVideoImageToVideoEncode",
    "WanVideoLoraSelect",
    "WanVideoModelLoader",
    "WanVideoSampler",
    "WanVideoSetBlockSwap",
    "WanVideoTextEmbedBridge",
    "WanVideoTextEncode",
    "WanVideoTorchCompileSettings",
    "WanVideoVAELoader",
    "WanVideoVRAMManagement",
}


def test_graph_shape_api_format() -> None:
    """Graph is a dict-of-dict; every value has class_type + inputs; node count == 26."""
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    assert isinstance(graph, dict), f"graph must be dict, got {type(graph)}"
    assert len(graph) == EXPECTED_NODE_COUNT, (
        f"expected {EXPECTED_NODE_COUNT} nodes, got {len(graph)}"
    )
    for node_id, node in graph.items():
        assert isinstance(node_id, str), f"node id {node_id!r} must be str"
        assert "class_type" in node, f"node {node_id!r} missing class_type"
        assert "inputs" in node, f"node {node_id!r} missing inputs"
        assert isinstance(node["inputs"], dict), (
            f"node {node_id!r} inputs must be dict, got {type(node['inputs'])}"
        )


def test_graph_class_types_within_expected_set() -> None:
    """Every class_type in the graph is in the expected kijai+core set."""
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    actual = {node["class_type"] for node in graph.values()}
    unexpected = actual - EXPECTED_CLASS_TYPES
    assert not unexpected, f"unexpected class_types in graph: {sorted(unexpected)}"


def test_asset_node_ids_reference_existing_nodes() -> None:
    """Every asset_node_ids[<role>] is a key in the graph dict."""
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    asset_node_ids = cfg.spec["asset_node_ids"]
    for role, node_id in asset_node_ids.items():
        assert node_id in graph, (
            f"asset role {role!r} -> node {node_id!r} not in graph "
            f"(graph keys: {sorted(graph.keys())})"
        )


def test_prompt_node_ids_is_dict_and_references_existing_nodes() -> None:
    """prompt_node_ids must be dict (Layer J), not list; every value in graph."""
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    prompt_node_ids = cfg.spec["prompt_node_ids"]
    assert isinstance(prompt_node_ids, dict), (
        f"prompt_node_ids must be dict (Layer J expects .items()), "
        f"got {type(prompt_node_ids).__name__}: {prompt_node_ids!r}"
    )
    for role, node_id in prompt_node_ids.items():
        assert node_id in graph, (
            f"prompt role {role!r} -> node {node_id!r} not in graph "
            f"(graph keys: {sorted(graph.keys())})"
        )
```

- [ ] **Step 2: Run tests to confirm RED**

Run: `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v`
Expected: 4 FAILED. Placeholder graph has 1 node (LoadImage) — `test_graph_shape_api_format` fails on node-count. `test_asset_node_ids_reference_existing_nodes` fails on `"12"` not in graph. `test_prompt_node_ids_is_dict_and_references_existing_nodes` fails on `list` type. `test_graph_class_types_within_expected_set` PASSES coincidentally (LoadImage is in the expected set) — that's fine.

- [ ] **Step 3: Lint + pre-commit gate**

Run: `pixi run pre-commit run --files tests/examples/test_runpod_comfyui_wan_graph.py`
Expected: all hooks pass (ruff, ruff-format, mypy, whitespace, eof).

- [ ] **Step 4: Commit**

```bash
git add tests/examples/test_runpod_comfyui_wan_graph.py
git commit -m "$(cat <<'EOF'
test(examples): lockdown scaffold for runpod-comfyui-wan graph (RED)

Four lockdown tests for examples/configs/runpod-comfyui-wan.graph.json
+ examples/configs/runpod-comfyui-wan.yaml cross-references. They
fail against the current 166-byte placeholder graph and the
list-typed prompt_node_ids YAML field; they go GREEN at the end of
Task 4 when the real graph and YAML wiring land.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Capture `/object_info` from warm pod (LIVE — user gate)

**Goal:** Boot a warm RunPod pod via the existing live smoke, capture ComfyUI's `/object_info` response (schema for every loaded node class), commit as a test fixture.

> **USER-ORDERED GATE — NON-SKIPPABLE.** Sub-spec §8.3 user checkpoint #1 (before pod boot). This task spends real money — ~$0.10–$0.20 cold boot for the pod + weights download. The agent MUST pause and confirm with the operator that creds are set and budget is OK before executing Step 1.

**Files:**
- Create: `tests/engines/fixtures/comfyui/object_info_wan21.json`

**Acceptance Criteria:**
- [ ] Pod boots; ComfyUI :8188 reachable via RunPod proxy.
- [ ] `tests/engines/fixtures/comfyui/object_info_wan21.json` exists, parses as JSON dict.
- [ ] Fixture contains entries for at least these class names: `WanVideoSampler`, `WanVideoModelLoader`, `WanVideoVAELoader`, `LoadWanVideoT5TextEncoder`, `WanVideoTextEncode`, `WanVideoImageToVideoEncode`, `WanVideoDecode`, `ImageResizeKJv2`, `VHS_VideoCombine`, `LoadImage`.
- [ ] Fixture size ≥ 100 KB (real `/object_info` for kijai + KJNodes + core is ~1–2 MB).
- [ ] Pod stays running (do NOT destroy) — Tasks 3, 4, 5 reuse this pod.

**Verify:**
```
test -f tests/engines/fixtures/comfyui/object_info_wan21.json \
  && jq -e 'keys | any(. == "WanVideoSampler")' tests/engines/fixtures/comfyui/object_info_wan21.json
```
Expected: `true`.

**Steps:**

- [ ] **Step 1: Confirm preconditions with user (gate)**

The agent must explicitly confirm before proceeding:
- `RUNPOD_API_KEY` + `RUNPOD_TERMINATE_KEY` + `HF_TOKEN` present in env / `.env`.
- `KINOFORGE_LIVE_TESTS=1`, `KINOFORGE_SAVE_FIXTURES=1`, `KINOFORGE_LIVE_KEEP_POD=1` set.
- Operator agrees to spend up to ~$2.00 across Tasks 2–5.
- Budget cap remaining ≥ $1.99 per PROGRESS.md item #2 closure snapshot.

If any precondition fails → STOP, surface to user.

- [ ] **Step 2: Boot pod via live smoke (expected to fail at the generate step)**

```bash
pixi run pytest tests/live/test_comfyui_wan_live.py -v -s 2>&1 | tee /tmp/smoke-task2.log
```

The test will fail at the generate step because the placeholder graph has 1 node — that is intentional. `KINOFORGE_LIVE_KEEP_POD=1` ensures the pod is NOT destroyed on failure. Smoke does provision ComfyUI + kijai custom nodes + downloads Wan weights (~25 GB). Expect ~5–10 minute cold boot.

- [ ] **Step 3: Locate POD_ID + proxy URL**

```bash
POD_ID=$(pixi run python -m kinoforge list \
  --config examples/configs/runpod-comfyui-wan.yaml \
  | rg -o 'id=([a-z0-9]+)' -r '$1' | head -1)
echo "POD_ID=$POD_ID"
PROXY_URL="https://${POD_ID}-8188.proxy.runpod.net"
echo "PROXY_URL=$PROXY_URL"
```

Expected: `POD_ID` is a 16-char alphanumeric string; `PROXY_URL` is a real RunPod proxy host.

- [ ] **Step 4: Capture `/object_info`**

```bash
mkdir -p tests/engines/fixtures/comfyui
curl -sS --max-time 30 "${PROXY_URL}/object_info" \
  | jq . > tests/engines/fixtures/comfyui/object_info_wan21.json
ls -l tests/engines/fixtures/comfyui/object_info_wan21.json
jq 'keys | length' tests/engines/fixtures/comfyui/object_info_wan21.json
```

Expected:
- File size ≥ 100 KB.
- `keys | length` returns a positive integer (typically 200–500).

- [ ] **Step 5: Sanity-check fixture content**

```bash
jq -e 'has("WanVideoSampler") and has("WanVideoModelLoader") and has("LoadImage")' \
  tests/engines/fixtures/comfyui/object_info_wan21.json
```

Expected: `true`. If `false` → kijai nodes aren't loaded yet. Wait 30s, restart ComfyUI on the pod, or destroy + retry. If still failing after 3 attempts → abort sub-plan; fall back to approach 3 (kijai source) on a future attempt (see sub-spec §8.1).

- [ ] **Step 6: Lint + pre-commit gate**

```bash
pixi run pre-commit run --files tests/engines/fixtures/comfyui/object_info_wan21.json
```

Note: `check-added-large-files (limit 500 KB)` hook is configured at the standard limit. The fixture is expected to be 1–2 MB — pre-commit WILL flag it. **Resolution:** add the fixture path to the hook's exclude list in `.pre-commit-config.yaml`, or commit with the explicit allow flag, OR shrink the fixture by removing schema entries unused by our graph. The recommended path is to add an exclusion line because the full fixture is useful as a reference. Apply this minimal edit to `.pre-commit-config.yaml`:

```yaml
  - id: check-added-large-files
    args: ['--maxkb=2048']
    exclude: ^tests/engines/fixtures/comfyui/object_info_wan21\.json$
```

Re-run pre-commit; expect clean.

- [ ] **Step 7: Commit**

```bash
git add tests/engines/fixtures/comfyui/object_info_wan21.json .pre-commit-config.yaml
git commit -m "$(cat <<'EOF'
test(fixtures): commit /object_info dump from warm pod for Wan 2.1 i2v graph

Captured ComfyUI /object_info from a warm RunPod pod with kijai
ComfyUI-WanVideoWrapper + ComfyUI-KJNodes loaded. Used as the
schema reference for hand-authoring the API-format workflow graph
in Task 3 and as the lockdown anchor for the /object_info fixture
test in Task 6.

Pre-commit large-files limit raised to 2 MB; this single fixture
explicitly excluded since the full schema is the point.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/engines/fixtures/comfyui/object_info_wan21.json"], "verifyCommand": "test -f tests/engines/fixtures/comfyui/object_info_wan21.json && jq -e 'has(\"WanVideoSampler\")' tests/engines/fixtures/comfyui/object_info_wan21.json", "acceptanceCriteria": ["pod up", "fixture committed", "fixture has WanVideoSampler entry", "fixture ≥ 100 KB", "pod stays running for Tasks 3-5"], "userGate": true, "tags": ["user-gate"], "gateScope": "step-1"}
```

---

## Task 3: Hand-author API-format graph + parse-validate on warm pod (LIVE — user gate)

**Goal:** Translate the kijai desktop workflow (`/tmp/wan21_i2v_workflow.json`) into ComfyUI API format using the captured `/object_info` schema. Validate by POSTing to `/prompt` on the warm pod until 200. Commit only the final passing graph.

> **USER-ORDERED GATE — NON-SKIPPABLE.** Sub-spec §8.3 user checkpoint #2 (after first `/prompt` 200). The agent MUST pause after Step 5 and confirm with the operator that the graph parses cleanly before proceeding to Task 4 (which depends on this graph's node IDs being final).

**Files:**
- Create: `examples/configs/runpod-comfyui-wan.graph.json` (replaces 166-byte placeholder)

**Acceptance Criteria:**
- [ ] `examples/configs/runpod-comfyui-wan.graph.json` is a JSON dict.
- [ ] Has exactly 26 entries.
- [ ] Every key is a semantic string ID per the locked mapping (sub-spec §7.4).
- [ ] Every value has `class_type` (str) and `inputs` (dict) keys.
- [ ] Every `class_type` appears in `tests/engines/fixtures/comfyui/object_info_wan21.json`.
- [ ] POSTing `{"prompt": <graph>, "client_id": "kinoforge-dev"}` to the pod's `/prompt` returns HTTP 200 with a non-empty `prompt_id`.
- [ ] Any queued prompt is cancelled via `DELETE /queue/{prompt_id}` so the pod does not actually render — keeps cost low.

**Verify:**
```bash
jq '. | length' examples/configs/runpod-comfyui-wan.graph.json  # → 26
jq -r '. | to_entries | .[] | "\(.key) \(.value.class_type)"' \
  examples/configs/runpod-comfyui-wan.graph.json | sort
```
Plus a recorded `/prompt` 200 response in the commit message body.

**Steps:**

- [ ] **Step 1: Locked semantic ID mapping**

Per sub-spec §7.4. Read the kijai desktop JSON (`/tmp/wan21_i2v_workflow.json`) and write a kijai-numeric-ID → semantic-ID mapping table. The 13 nodes named in the spec are locked; the remaining 13 nodes (BlockSwap, VRAMManagement, TorchCompileSettings, LoraSelect, TextEmbedBridge, plus duplicates and `Note` annotations) get short semantic names assigned during this step. Record the full mapping in the commit message body so future readers can diff against the kijai source.

- [ ] **Step 2: Author every node entry**

For each of the 26 desktop nodes:

```
desktop node.type        → API entry's "class_type"
semantic ID              → API entry's key
desktop node.widgets_values[i]
  + /object_info[class_type].input.required.<name> ordering
                         → API entry's "inputs" widget fields
desktop node.inputs[i] resolved via desktop.links
  → [<src_node_semantic_id>, <src_port_index>]
                         → API entry's "inputs" link fields
```

Worked example for `LoadImage`:

```json
"load_image": {
  "class_type": "LoadImage",
  "inputs": {
    "image": "input.png"
  }
}
```

Worked example for `WanVideoSampler` (showing both widget values and link inputs):

```json
"wan_sampler": {
  "class_type": "WanVideoSampler",
  "inputs": {
    "model": ["wan_model", 0],
    "image_embeds": ["i2v_encode", 0],
    "text_embeds": ["text_encode_pos", 0],
    "negative_text_embeds": ["text_encode_neg", 0],
    "steps": 20,
    "cfg": 6.0,
    "shift": 5.0,
    "seed": 42,
    "scheduler": "unipc",
    "riflex_freq_index": 0
  }
}
```

(Exact field names + counts come from the captured `/object_info` entry for each class — DO NOT guess names; read them from the fixture.)

Write the result to `examples/configs/runpod-comfyui-wan.graph.json` with `json.dump(..., indent=2, sort_keys=True)` for diff stability.

- [ ] **Step 3: Local shape sanity check**

```bash
jq -e 'length == 26' examples/configs/runpod-comfyui-wan.graph.json
jq -e '[.[] | has("class_type") and has("inputs")] | all' \
  examples/configs/runpod-comfyui-wan.graph.json
```

Expected: both return `true`.

- [ ] **Step 4: Parse-validate via `/prompt`**

```bash
PROXY_URL="https://${POD_ID}-8188.proxy.runpod.net"
RESPONSE=$(curl -sS --max-time 30 -X POST "${PROXY_URL}/prompt" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --slurpfile g examples/configs/runpod-comfyui-wan.graph.json \
        '{prompt: $g[0], client_id: "kinoforge-dev"}')")
echo "$RESPONSE" | jq .
```

- 200 case: response is `{"prompt_id": "<uuid>", "number": <int>, "node_errors": {}}`. Capture `prompt_id` and immediately cancel the queue:

```bash
PROMPT_ID=$(echo "$RESPONSE" | jq -r .prompt_id)
curl -sS -X POST "${PROXY_URL}/queue" \
  -H "Content-Type: application/json" \
  -d "{\"delete\": [\"${PROMPT_ID}\"]}"
```

- 4xx case: response is `{"error": {"message": "...", "details": "...", "extra_info": {...}}, "node_errors": {"<id>": ["..."]}}`. Read `node_errors`; patch the listed nodes' `inputs`; re-run Step 4. Each iteration is seconds + zero render cost.

- 5xx case: server crash. Restart ComfyUI on the pod via the live-smoke `provision` helper, or destroy + reboot. Penalty ~$0.10. If recurring → escalate to user.

Iterate until 200. Record the final `/prompt` response JSON for the commit message.

- [ ] **Step 5: User gate — confirm parse-clean**

Pause and confirm with operator before proceeding: the graph POSTed cleanly, `/queue` cancel succeeded, pod is still warm. OK to proceed to Task 4 (YAML wiring) and Task 5 (full render).

- [ ] **Step 6: Lint + pre-commit gate**

```bash
pixi run pre-commit run --files examples/configs/runpod-comfyui-wan.graph.json
```

Expected: passes (whitespace/EOF/check-merge-conflict only; mypy/ruff don't touch JSON).

- [ ] **Step 7: Commit**

```bash
git add examples/configs/runpod-comfyui-wan.graph.json
git commit -m "$(cat <<'EOF'
feat(examples): real ComfyUI API-format graph for Wan 2.1 i2v 14B 480P fp8

Replaces the 166-byte placeholder runpod-comfyui-wan.graph.json with
a 26-node hand-authored ComfyUI API-format workflow. Translated from
kijai's ComfyUI-WanVideoWrapper example wanvideo_2_1_14B_I2V_example_03.json
using the captured /object_info schema in
tests/engines/fixtures/comfyui/object_info_wan21.json.

Semantic string node IDs throughout (load_image, wan_sampler, etc).
Full kijai-numeric-ID → semantic-ID mapping below.

Parse-validated against the live warm pod via POST /prompt → 200
(prompt_id captured + queue-cancelled, no render performed at this
stage). No engine-side code touched.

Mapping (desktop ID → semantic ID):
  [populated during Step 1 above]

/prompt response at validation time:
  [populated from Step 4 above]

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["examples/configs/runpod-comfyui-wan.graph.json"], "verifyCommand": "jq -e 'length == 26 and ([.[] | has(\"class_type\") and has(\"inputs\")] | all)' examples/configs/runpod-comfyui-wan.graph.json", "acceptanceCriteria": ["26 entries", "semantic string IDs", "every entry has class_type + inputs", "every class_type in /object_info fixture", "POST /prompt → 200 on warm pod", "queue cancelled, no render performed"], "userGate": true, "tags": ["user-gate"], "gateScope": "step-5"}
```

---

## Task 4: YAML wiring → real node IDs + Layer J dict type (offline against warm pod)

**Goal:** Update `examples/configs/runpod-comfyui-wan.yaml` so `asset_node_ids.init_image` → `load_image`, `prompt_node_ids.positive` → `text_encode_pos` (and crucially: `prompt_node_ids` is a `dict`, not a `list`). After this lands, Task 1's four lockdown tests transition RED → GREEN.

**Files:**
- Modify: `examples/configs/runpod-comfyui-wan.yaml` (~10-line diff in the `spec:` block)

**Acceptance Criteria:**
- [ ] `cfg.spec["asset_node_ids"]` is `{"init_image": "load_image"}`.
- [ ] `cfg.spec["prompt_node_ids"]` is `{"positive": "text_encode_pos"}` — `dict`, not `list`.
- [ ] `cfg.spec["node_overrides"]` is `{}` (no per-run overrides at this stage; future runs can override).
- [ ] `cfg.spec["graph_file"]` resolution still works (Task 1 of main Layer P plan, already shipped).
- [ ] `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v` → 4 PASSED (RED → GREEN).
- [ ] Full offline suite `pixi run pytest -m 'not live'` stays green.

**Verify:** `pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v` → 4 passed.

**Steps:**

- [ ] **Step 1: Edit the YAML**

In `examples/configs/runpod-comfyui-wan.yaml`, replace the existing `spec:` block:

```yaml
spec:
  graph_file: runpod-comfyui-wan.graph.json
  asset_node_ids:
    init_image: "12"           # rewritten when exported workflow lands
  prompt_node_ids:
    - "8"
  node_overrides: {}
```

with:

```yaml
spec:
  graph_file: runpod-comfyui-wan.graph.json
  asset_node_ids:
    init_image: load_image
  prompt_node_ids:
    # Layer J prompt routing: dict[role -> node_id]. Wan i2v has two
    # text-encode nodes — the negative-prompt node carries a baked
    # default in the graph itself, so only the positive node receives
    # the user prompt via Layer J.
    positive: text_encode_pos
  node_overrides: {}
```

- [ ] **Step 2: Run lockdown tests**

```bash
pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v
```

Expected: 4 passed. If any fail:
- `test_graph_shape_api_format` failing → graph from Task 3 is wrong; revisit.
- `test_asset_node_ids_reference_existing_nodes` failing → `load_image` is not the semantic ID we used; check Task 3 mapping table.
- `test_prompt_node_ids_is_dict_and_references_existing_nodes` failing → YAML syntax (list, not dict); recheck the edit.

- [ ] **Step 3: Run full offline suite**

```bash
pixi run pytest -m 'not live' -q
```

Expected: at least 858 + 4 = 862 passed + 1 skipped (pre-Task-3 baseline was 858; Task 1 added 4 RED → now GREEN). No regressions in any other test.

- [ ] **Step 4: Typecheck + lint gate**

```bash
pixi run mypy . && pixi run ruff check . && \
  pixi run pre-commit run --all-files
```

Expected: all clean.

- [ ] **Step 5: Commit**

```bash
git add examples/configs/runpod-comfyui-wan.yaml
git commit -m "$(cat <<'EOF'
feat(examples): wire runpod-comfyui-wan.yaml to real node IDs (Layer J dict)

After Task 3 landed the real 26-node API-format graph, this updates
the example YAML to point at the actual semantic node IDs:

  asset_node_ids.init_image: load_image
  prompt_node_ids.positive: text_encode_pos

prompt_node_ids flips from list (["8"]) to dict ({positive: ...}) —
Layer J's engines/comfyui code calls .items() on the value, so the
prior list would AttributeError at first live run. That's the
pre-existing bug surfaced by Task 1's lockdown test, now fixed.

The negative prompt is baked into the graph's text_encode_neg node
default (a stable "low quality, blurry, distorted" string) rather
than routed through Layer J, which only carries one prompt.

Task 1's four lockdown tests transition RED → GREEN with this
commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["examples/configs/runpod-comfyui-wan.yaml"], "verifyCommand": "pixi run pytest tests/examples/test_runpod_comfyui_wan_graph.py -v", "acceptanceCriteria": ["asset_node_ids.init_image=load_image", "prompt_node_ids is dict with positive key", "node_overrides empty dict", "Task 1 lockdown tests RED → GREEN"]}
```

---

## Task 5: Live full execution → green MP4 + zero-or-more bug-catch commits (LIVE — user gate)

**Goal:** Run `kinoforge generate` against the warm pod with the new graph + YAML, drive ComfyUI to actually render the Wan 2.1 i2v workflow end-to-end, capture a non-empty MP4 with valid `ftyp` magic, capture ComfyUI HTTP fixtures via the existing `KINOFORGE_SAVE_FIXTURES=1` seam, and patch any production-code bugs that surface (each as its own commit with offline regression test).

> **USER-ORDERED GATE — NON-SKIPPABLE.** Sub-spec §8.3 user checkpoint #3 (after green MP4). This task spends the bulk of the live budget (~$0.30–$1.50 for a 5–10 min render + zero-or-more retry iterations). The agent MUST pause and confirm with the operator after a green MP4 lands before proceeding to Tasks 6–7. Bug-catch budget per sub-spec §8.2 is 5 distinct production bug classes; abort sub-plan if exceeded.

**Files:**
- May modify (zero or more, depends on bug-catch surface):
  - `src/kinoforge/engines/comfyui/__init__.py` (`/upload/image` multipart shape, `/history` outputs extraction)
  - `src/kinoforge/engines/comfyui/nodes.py` (custom-node requirements.txt install path)
  - `src/kinoforge/core/provision_state.py` (marker registration under warm-tag-discovery pod)
  - `src/kinoforge/sources/huggingface/__init__.py` or ComfyUI `TARGET_TO_SUBDIR` (text_encoder target routing)
- May create regression tests under `tests/engines/test_comfyui.py` (one per code patch)

**Acceptance Criteria:**
- [ ] `pixi run pytest tests/live/test_comfyui_wan_live.py -v -s` produces a non-empty MP4.
- [ ] MP4 first 12 bytes contain one of `ftypisom`, `ftypiso5`, `ftypiso6`, `ftypmp42`.
- [ ] MP4 file size ≥ 100 KB (sanity floor for a real Wan render).
- [ ] If `KINOFORGE_SAVE_FIXTURES=1` captured new ComfyUI HTTP fixtures under `tests/engines/fixtures/comfyui/`, they are committed alongside (or in the same commits as) the code patches that consume them.
- [ ] Each production-code patch ships its own commit with `fix(<area>): <one-line bug summary>` AND an offline regression test against captured fixture shape (Layer N pattern).
- [ ] Sub-plan bug-catch budget (5 distinct classes) NOT exceeded. If exceeded → abort sub-plan, commit landed work, partial-close PROGRESS row.
- [ ] Pod destroyed at end of task (clear `KINOFORGE_LIVE_KEEP_POD` and re-run smoke teardown, OR destroy via CLI / RunPod console).
- [ ] Total cost burn from Tasks 2–5 ≤ $1.50.

**Verify:** Sub-spec AC5 + AC6 from §10.

**Steps:**

- [ ] **Step 1: Re-run live smoke against warm pod**

```bash
pixi run pytest tests/live/test_comfyui_wan_live.py -v -s 2>&1 | tee /tmp/smoke-task5.log
```

Smoke now uses Task 7 item #2 warm-reuse: orchestrator finds the pod via `find_instance_by_tag`, calls `engine.provision(instance, cfg_dict)` (idempotent marker check), builds backend, calls `engine.submit(graph)`. ComfyUI renders. `engine.result()` polls `/history` until outputs populate. Final MP4 lands under `output/<run_id>/<filename>.mp4` (Layer O sink).

Expected outcomes:
- **Green path:** smoke passes; MP4 written; magic bytes valid. Proceed to Step 4.
- **Bug-catch path:** smoke fails at a specific node. Read `node_errors` or `messages[].type == "execution_error"` in `/history`. Identify which engine seam is at fault. Proceed to Step 2.

- [ ] **Step 2: Bug-catch loop (zero or more iterations)**

For each distinct production bug class (max 5 per sub-spec §8.2), repeat this sub-loop:

a. **Diagnose.** Read the failure response carefully. Map it to one of the four known-likely bug classes from sub-spec §5.2, or identify a new class.

b. **Patch.** Make the minimal code change in the relevant file. Examples (illustrative, exact code depends on the actual error):

   `/upload/image` multipart boundary missing trailing CRLF:

   ```python
   # src/kinoforge/engines/comfyui/__init__.py
   # before:
   body = b"--%s\r\nContent-Disposition: ..." % boundary
   # after:
   body = b"--%s\r\nContent-Disposition: ...\r\n--%s--\r\n" % (boundary, boundary)
   ```

   `/history` outputs key mismatch (e.g., real response uses `"videos"` not `"gifs"`):

   ```python
   # src/kinoforge/engines/comfyui/__init__.py
   # _extract_output_filename(...) — add real key to the search list
   for key in ("videos", "gifs", "images"):
       if key in outputs:
           return outputs[key][0]["filename"]
   ```

c. **Capture fixture.** If the failure response is novel, save it under `tests/engines/fixtures/comfyui/<descriptive_name>.json` for the regression test.

d. **Write offline regression test.** Layer N pattern — test loads the fixture and asserts the patched code handles it correctly.

   Template:

   ```python
   def test_extract_output_handles_videos_key(tmp_path: Path) -> None:
       """Regression: /history outputs dict uses "videos" key (real RunPod shape)."""
       fixture_path = Path("tests/engines/fixtures/comfyui/history_videos_outputs.json")
       outputs = json.loads(fixture_path.read_text())["<prompt_id>"]["outputs"]
       backend = _make_backend()
       filename = backend._extract_output_filename(outputs)
       assert filename.endswith(".mp4")
   ```

e. **Verify regression test fails without the patch, passes with it.**

   ```bash
   # Stash the patch temporarily
   git stash -- src/kinoforge/engines/comfyui/__init__.py
   pixi run pytest tests/engines/test_comfyui.py::test_extract_output_handles_videos_key -v
   # Expected: FAIL
   git stash pop
   pixi run pytest tests/engines/test_comfyui.py::test_extract_output_handles_videos_key -v
   # Expected: PASS
   ```

f. **Typecheck + lint.**

   ```bash
   pixi run mypy . && pixi run ruff check . && \
     pixi run pre-commit run --all-files
   ```

g. **Commit (one bug = one commit).**

   ```bash
   git add -p src/kinoforge/engines/comfyui/__init__.py \
              tests/engines/test_comfyui.py \
              tests/engines/fixtures/comfyui/history_videos_outputs.json
   git commit -m "$(cat <<'EOF'
   fix(engines/comfyui): extract output filename from "videos" key (real RunPod shape)

   First green MP4 (Layer P Task 7 item #3) revealed /history outputs
   dict uses "videos" key, not "gifs". Adds it to the search list and
   ships a regression test against the captured response shape.

   Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
   EOF
   )"
   ```

h. **Re-run live smoke.** Loop back to Step 1.

If bug-class count reaches 6 → abort. Commit what's landed. Skip to Step 4 (partial close).

- [ ] **Step 3: Green-MP4 validation**

When the live smoke passes:

```bash
MP4=$(rg -o 'output/[^ ]*\.mp4' /tmp/smoke-task5.log | tail -1)
echo "MP4=$MP4"
ls -l "$MP4"
xxd "$MP4" | head -1
```

Confirm:
- File exists, size ≥ 100 KB.
- `head -c 12 $MP4 | xxd` shows one of the `ftyp*` magic bytes per sub-spec AC5.

- [ ] **Step 4: User gate — confirm green or partial-close**

Pause. Present operator with:
- MP4 path + size + magic.
- Cost burn so far (read from RunPod console or BudgetTracker logs).
- Bug-catch count (zero or more).
- Decision: proceed to Task 6 (close-out), OR partial-close (if budget hit / bug ceiling hit).

- [ ] **Step 5: Destroy pod**

```bash
unset KINOFORGE_LIVE_KEEP_POD
# Re-run smoke once to trigger teardown finally-clause, OR:
pixi run python -m kinoforge destroy "$POD_ID" \
  --config examples/configs/runpod-comfyui-wan.yaml
```

Verify on RunPod console that the pod is gone. Selfterm also covers this if test process dies, but explicit destroy is cleaner.

```json:metadata
{"files": ["src/kinoforge/engines/comfyui/__init__.py", "src/kinoforge/engines/comfyui/nodes.py", "src/kinoforge/core/provision_state.py", "tests/engines/test_comfyui.py", "tests/engines/fixtures/comfyui/"], "verifyCommand": "pixi run pytest tests/live/test_comfyui_wan_live.py -v -s", "acceptanceCriteria": ["non-empty MP4 produced", "ftyp magic bytes valid", "MP4 ≥ 100 KB", "every code patch has offline regression test", "bug-catch budget (≤5 distinct classes) not exceeded", "pod destroyed", "total cost ≤ $1.50"], "userGate": true, "tags": ["user-gate"], "gateScope": "step-4", "requireEvidenceTokens": [["smoke-fail", "before-patch", "node_errors"], ["smoke-green", "after-patch", "ftypisom", "ftypiso5", "ftypiso6", "ftypmp42"]]}
```

---

## Task 6: `/object_info` fixture lockdown test (offline)

**Goal:** Add one offline test that loads the `/object_info` fixture and asserts every `class_type` used in the committed graph also exists in the fixture. Locks down the fixture's role as the schema-reference-of-record for future graph edits.

**Files:**
- Modify: `tests/engines/test_comfyui.py` (~+25 LOC for one test)

**Acceptance Criteria:**
- [ ] One new test `test_object_info_fixture_covers_graph_class_types` in `tests/engines/test_comfyui.py`.
- [ ] Test loads `tests/engines/fixtures/comfyui/object_info_wan21.json`.
- [ ] Test loads `examples/configs/runpod-comfyui-wan.graph.json` (via `load_config` on the YAML, for consistency with Task 1 tests).
- [ ] Asserts: for every `node["class_type"]` in the graph, the same `class_type` is a key in the `/object_info` fixture.
- [ ] `pixi run pytest tests/engines/test_comfyui.py::test_object_info_fixture_covers_graph_class_types -v` passes.

**Verify:** `pixi run pytest tests/engines/test_comfyui.py::test_object_info_fixture_covers_graph_class_types -v` → 1 passed.

**Steps:**

- [ ] **Step 1: Add the test**

Append to `tests/engines/test_comfyui.py`:

```python
def test_object_info_fixture_covers_graph_class_types() -> None:
    """Every class_type in the committed graph must exist in the /object_info fixture.

    Locks down the /object_info dump as the schema reference of record
    for the committed Wan 2.1 i2v workflow graph. If a future graph
    edit introduces a class_type not in the fixture, this test fails
    until the fixture is refreshed against a warm pod.
    """
    fixture_path = Path("tests/engines/fixtures/comfyui/object_info_wan21.json")
    graph_path = Path("examples/configs/runpod-comfyui-wan.graph.json")

    object_info = json.loads(fixture_path.read_text())
    graph = json.loads(graph_path.read_text())

    graph_class_types = {node["class_type"] for node in graph.values()}
    missing = graph_class_types - set(object_info.keys())

    assert not missing, (
        f"graph references class_types not in /object_info fixture: "
        f"{sorted(missing)} — refresh the fixture against a warm pod "
        f"with kijai custom nodes loaded"
    )
```

Add any needed imports (`from pathlib import Path`, `import json`) if not already imported in the file.

- [ ] **Step 2: Run the test**

```bash
pixi run pytest tests/engines/test_comfyui.py::test_object_info_fixture_covers_graph_class_types -v
```

Expected: PASSED.

- [ ] **Step 3: Run full offline suite**

```bash
pixi run pytest -m 'not live' -q
```

Expected: count grew by 1 vs the baseline at end of Task 4 (i.e., ≥ 863 passed + 1 skipped). No regressions.

- [ ] **Step 4: Typecheck + lint gate**

```bash
pixi run mypy . && pixi run ruff check . && \
  pixi run pre-commit run --all-files
```

Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/engines/test_comfyui.py
git commit -m "$(cat <<'EOF'
test(engines/comfyui): lockdown /object_info fixture covers graph class_types

One offline test that asserts every class_type in
examples/configs/runpod-comfyui-wan.graph.json also exists as a key
in tests/engines/fixtures/comfyui/object_info_wan21.json. The
fixture's role is to be the schema reference of record for future
graph edits; this test fails fast if a future graph edit references
a class the fixture doesn't cover, prompting a refresh.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/engines/test_comfyui.py"], "verifyCommand": "pixi run pytest tests/engines/test_comfyui.py::test_object_info_fixture_covers_graph_class_types -v", "acceptanceCriteria": ["test exists", "test passes", "no regressions in full offline suite"]}
```

---

## Task 7: PROGRESS.md closure row (offline)

**Goal:** Add the item #3 closure block to `PROGRESS.md` mirroring item #1 / item #2 shape, plus mark item #3 + item #4 CLOSED in the "Pending Task 7 work" list.

**Files:**
- Modify: `PROGRESS.md` (item #3 closure block + "Pending Task 7 work" item #3 + item #4 markers)

**Acceptance Criteria:**
- [ ] `PROGRESS.md` has a new "Layer P Task 7 item #3 (workflow API JSON + first green MP4) — ✅ CLOSED 2026-06-01 at HEAD `<sha>`" block matching the shape of the existing item #1 + item #2 closure blocks.
- [ ] Block includes: sub-spec path + commit SHA, sub-plan path + commit SHA, atomic commit list from Tasks 1–6, MP4 evidence (path / bytes / ftyp magic / run ID / capability key / git SHA at capture), final cost burn, bug-catch list (zero or more entries), test count delta, key design decisions.
- [ ] In the "Pending Task 7 work" list, item #3 is marked `~~item #3 (workflow API JSON)~~ — **CLOSED**` with the closing commits + MP4 evidence; item #4 is marked `~~item #4 (live unknowns surfaced)~~ — **CLOSED** as part of item #3 sub-plan`.
- [ ] Branch state table at PROGRESS.md (the SHA-to-task mapping) gets new rows for every commit from Tasks 1–6.
- [ ] Test counts line updated: `846 post-Task-7-item-1 → 858 post-Task-7-item-2 → <N> post-Task-7-item-3`.
- [ ] Cost burn line updated to running total.

**Verify:** `rg -n 'item #3.*CLOSED' PROGRESS.md` → at least 2 hits (closure block header + "Pending Task 7 work" list).

**Steps:**

- [ ] **Step 1: Gather inputs**

Collect:
- All commit SHAs from Tasks 1–6 via `git log --oneline build/layer-p ^main` (filter to the new commits).
- MP4 absolute path + byte size + first 12 bytes from Task 5's evidence.
- Final cost burn (read from RunPod console or BudgetTracker logs).
- Bug-catch list (zero or more, each `fix(...)` commit subject).
- Test count delta (`pixi run pytest -m 'not live' -q --co | tail -1` before/after).

- [ ] **Step 2: Edit PROGRESS.md "Single next action" + "Pending Task 7 work" sections**

In `PROGRESS.md`, mirror the existing item #1 + item #2 closure block shape. Insert a new "Layer P Task 7 item #3" closure block right after the item #2 closure block (around line 187). Sample structure (fill placeholders with real values):

```markdown
**Layer P Task 7 item #3 (workflow API JSON + first green MP4) — ✅ CLOSED 2026-06-01 at HEAD `<sha>`.**
Sub-spec + sub-plan + N atomic commits + bug-catch list:
- Sub-spec: `docs/superpowers/specs/2026-06-01-layer-p-task7-item3-workflow-api-json-design.md` (`e2f25df`)
- Sub-plan: `docs/superpowers/plans/2026-06-01-layer-p-task7-item3-workflow-api-json.md` (+ `.tasks.json`) (`<sha>`)
- `<sha>` — `test(examples): lockdown scaffold for runpod-comfyui-wan graph (RED)`
- `<sha>` — `test(fixtures): commit /object_info dump from warm pod for Wan 2.1 i2v graph`
- `<sha>` — `feat(examples): real ComfyUI API-format graph for Wan 2.1 i2v 14B 480P fp8`
- `<sha>` — `feat(examples): wire runpod-comfyui-wan.yaml to real node IDs (Layer J dict)`
- bug-catch commits (zero or more): [list each]
- `<sha>` — `test(engines/comfyui): lockdown /object_info fixture covers graph class_types`
- `<sha>` — `docs(progress): Layer P Task 7 item #3 — closure snapshot`

**First green MP4:** `<absolute path>` — `<bytes>` bytes, MP4 (`<ftyp magic>`), produced by `comfyui` engine on RunPod pod `<POD_ID>` (NVIDIA `<GPU>` @ `$<rate>`/hr), captured at git SHA `<sha>`. Cost burn for sub-plan: `$<final>`.

**Bug-catch list (Task 5 live shake-out):**
[zero or more entries, one per fix commit]

Test count 858 → `<N>` (+`<delta>` net offline tests). typecheck/lint/pre-commit all-files clean. Spec+code reviewers APPROVED on every task.

**Key design decisions (item #3):**
- Schema-first single live session — `/object_info` captured before graph authored, frozen reference for the iteration loop.
- `/object_info` is dev-time-only — engine never calls it at runtime (compliant with parent spec §Out-of-scope line 400).
- Semantic string node IDs (load_image, wan_sampler, etc) — self-documenting YAML wiring.
- Hand-author + REST-validate via /prompt parse-only loop — fast iteration (seconds per round-trip), captures actual server contract.
- `prompt_node_ids` list → dict fix — Layer J's engines/comfyui calls `.items()`; pre-existing bug surfaced + fixed.
- Negative prompt baked into graph default — Layer J only routes one prompt, negative is stable.
```

Also update the "Pending Task 7 work" list (around line 226) — strike through item #3 + item #4:

```markdown
3. ~~**Workflow format conversion**~~ **CLOSED** by Task 7 item #3 sub-plan (commits `<sha>`–`<sha>`). 26-node hand-authored API-format graph at `examples/configs/runpod-comfyui-wan.graph.json` validated against warm pod /prompt + green MP4 produced. `/object_info` fixture committed as schema reference.
4. ~~**Remaining unknowns to surface via live iteration:**~~ **CLOSED** as part of item #3 sub-plan. Bug-catches: [list each here].
```

Also append rows to the "Branch state" SHA table for every new commit.

- [ ] **Step 3: Lint + pre-commit gate**

```bash
pixi run pre-commit run --files PROGRESS.md
```

Expected: clean (trailing whitespace + EOF the only relevant hooks).

- [ ] **Step 4: Commit**

```bash
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(progress): Layer P Task 7 item #3 — closure snapshot

First green MP4 from kinoforge end-to-end on real cloud compute.
Item #3 (workflow API JSON) + item #4 (live unknowns surfaced) both
closed by this sub-plan.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["PROGRESS.md"], "verifyCommand": "rg -n 'item #3.*CLOSED' PROGRESS.md", "acceptanceCriteria": ["closure block added", "item #3 marked CLOSED in Pending Task 7 work list", "item #4 marked CLOSED as absorbed", "Branch state SHA table updated", "test counts line updated", "cost burn updated"]}
```

---

## Self-review notes

- **Spec coverage:** every section of the sub-spec maps to at least one task. §6 (Phase A) → Task 1. §7 (Phase B, steps 1–8) → Tasks 2, 3, 4, 5. §9 (Phase C) → Tasks 6, 7. §8 (failure modes) → embedded in Task 5 steps. AC1–AC11 (§10) all have corresponding task ACs.
- **Type consistency:** semantic IDs (`load_image`, `text_encode_pos`, `wan_sampler`) used consistently across Tasks 3, 4, 6. YAML keys (`asset_node_ids`, `prompt_node_ids`, `node_overrides`) match the engine's expected dict-of-dict shape.
- **Placeholder scan:** no TBD/TODO. Two intentional `[populated during Step 1 above]` markers in the Task 3 commit message body — those get filled in by the executing engineer at commit time, not hand-waved away.
- **User-gate tagging:** Tasks 2, 3, 5 tagged `userGate: true` per sub-spec §8.3 explicit checkpoint structure. Task 5 carries `requireEvidenceTokens` for before/after smoke evidence (cost is real, gate must show both states).
- **Test count math:** Task 1 adds +4 RED → GREEN at Task 4. Task 6 adds +1. Task 5 adds 0–N bug-catch regression tests. Sub-spec AC11 floor of +5 satisfied (net ≥ 4 + 1 = 5 even at zero bug catches).
