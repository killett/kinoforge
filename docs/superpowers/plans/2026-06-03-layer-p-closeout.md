# Layer P close-out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close Layer P by refactoring offline ComfyUI tests onto captured fixtures, locking the HTTP contract with two shape tests, and writing the README + PROGRESS Phase 28 close-out + `layer-p-closed` git tag.

**Architecture:** Three sequential commits on `main`. T8 adds `tests/engines/conftest.py` with `_load_comfy_fixture` helper and refactors all 34 tests in `tests/engines/test_comfyui.py` uniformly. T9 appends two shape-lockdown tests using the same helper (view AC dropped). T10 updates README + PROGRESS + tags the closure commit.

**Tech Stack:** pytest, ruff, mypy, pixi. No new runtime dependencies. Pure offline work — zero live spend.

**Spec:** `docs/superpowers/specs/2026-06-03-layer-p-closeout-design.md`

---

## File structure

| File | Action | Owner task | Purpose |
|---|---|---|---|
| `tests/engines/conftest.py` | Create | T8 | Holds the `_load_comfy_fixture(name)` helper shared across `tests/engines/` |
| `tests/engines/test_comfyui.py` | Modify | T8 (rewrite 34 existing) + T9 (+2 new tests) | All 34 tests load HTTP shapes via the helper; T9 appends 2 shape-lockdown tests |
| `tests/engines/fixtures/comfyui/prompt_submit.json` | Read only | — | Captured `POST /prompt` response from item #3 live smoke |
| `tests/engines/fixtures/comfyui/history_done.json` | Read only | — | Captured terminal `GET /history/{id}` response from same smoke |
| `README.md` | Modify | T10 | Insert `Engine integration (ComfyUI + Wan i2v)` sub-section under existing RunPod heading |
| `PROGRESS.md` | Modify | T10 | Append Phase 28 entry; mark Layer P closed; reset Single Next Action |
| (git tag) `layer-p-closed` | Create | T10 | Annotated tag at the T10 commit |

No production code in `src/kinoforge/` is touched.

---

## Task ordering and dependencies

```
T8 (helper + 34-test rewrite)
  └─► T9 (2 shape-lockdown tests; depends on T8 helper)
        └─► T10 (docs + tag; depends on T8/T9 SHAs to cite)
```

Each task produces exactly one commit. Bisectable.

---

## Task 8: Add `_load_comfy_fixture` helper + refactor 34 tests onto captured fixtures

**Goal:** Replace every hand-typed ComfyUI HTTP-response literal in `tests/engines/test_comfyui.py` with a load from `tests/engines/fixtures/comfyui/{prompt_submit,history_done}.json` via a new `_load_comfy_fixture` helper in `tests/engines/conftest.py`.

**Files:**
- Create: `tests/engines/conftest.py`
- Modify: `tests/engines/test_comfyui.py` (34 tests refactored)

**Acceptance Criteria:**
- [ ] `tests/engines/conftest.py` exists, defines `_load_comfy_fixture(name: str) -> dict[str, Any]`, and is exported via the conftest module (importable as `from tests.engines.conftest import _load_comfy_fixture`).
- [ ] Every test in `tests/engines/test_comfyui.py` that previously constructed a hand-typed `{"prompt_id": "..."}`-style POST response now loads from `_load_comfy_fixture("prompt_submit.json")`.
- [ ] Every test that previously constructed a hand-typed `{"<id>": {"status": ..., "outputs": ...}}`-style history response now loads from `_load_comfy_fixture("history_done.json")` (or a modified copy of it for the few tests that need running-state or per-node-output variations — see Step 4).
- [ ] `rg '\{"prompt_id":\s*"[^"]+"\}' tests/engines/test_comfyui.py` returns no matches in test bodies (lambdas / dict returns). Module-level `meta={"prompt_id": "X"}` constructions on the `Artifact` test fixture are allowed (they test the request-side, not the response-side).
- [ ] `pixi run pytest tests/engines/test_comfyui.py -v` → 34 passed.
- [ ] `pixi run pre-commit run --files tests/engines/conftest.py tests/engines/test_comfyui.py` → all hooks green.

**Verify:** `pixi run pytest tests/engines/test_comfyui.py -v && rg '\{"prompt_id":\s*"[^"]+"\}' tests/engines/test_comfyui.py`

Expected: 34 passed; second command empty output.

**Steps:**

- [ ] **Step 1: Create `tests/engines/conftest.py`**

Write the file exactly:

```python
"""Shared pytest helpers for the engines/ test suite.

Layer P fixture-replay helper for ComfyUI offline tests. Mirrors the
Layer N pattern in tests/providers/conftest_runpod.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_COMFY_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "comfyui"


def _load_comfy_fixture(name: str) -> dict[str, Any]:
    """Load a captured ComfyUI HTTP response by fixture filename.

    Args:
        name: Fixture filename relative to ``tests/engines/fixtures/comfyui/``
            (e.g. ``"prompt_submit.json"``).

    Returns:
        The ``response`` block of the fixture (the ``_meta`` block is
        captured for forensic value, not surfaced).
    """
    with (_COMFY_FIXTURE_DIR / name).open() as f:
        return dict(json.load(f)["response"])
```

- [ ] **Step 2: Inspect the captured response shapes before editing tests**

Run (one-time, for reference; don't commit any output):

```bash
python3 -c "import json; d=json.load(open('tests/engines/fixtures/comfyui/prompt_submit.json')); print('prompt_submit.response =', d['response'])"
python3 -c "import json; d=json.load(open('tests/engines/fixtures/comfyui/history_done.json')); r=d['response']; pid=next(iter(r)); print('history pid:', pid); print('body keys:', list(r[pid].keys())); print('outputs node ids:', list(r[pid]['outputs'].keys()))"
```

Expected output (use these literal values when relaxing assertions in Step 4):

```
prompt_submit.response = {'prompt_id': '929aecfb-22c9-4cbf-85e5-4dc92042f2d7', 'number': 0, 'node_errors': {}}
history pid: 929aecfb-22c9-4cbf-85e5-4dc92042f2d7
body keys: ['prompt', 'outputs', 'status', 'meta']
outputs node ids: ['30']
```

Key facts for the rewrite:
- `prompt_submit.response["prompt_id"]` is the literal UUID `929aecfb-22c9-4cbf-85e5-4dc92042f2d7`.
- `history_done.response` has exactly one top-level key (that same UUID).
- The history `outputs` dict has one node (`"30"`) — tests asserting on different node IDs need an override (see Step 4 helper).

- [ ] **Step 3: Locate every hand-typed response in `test_comfyui.py`**

```bash
rg -n '\{"prompt_id"|"outputs":|"status":\s*"(running|completed)"' tests/engines/test_comfyui.py
```

Expected match clusters (commit `72bc6b7` baseline):
- lines ~300, 324, 698, 743, 779, 842, 878, 918, 953, 1345, 1383, 1420, 1459 — POST `/prompt` lambdas returning `{"prompt_id": "..."}`
- lines ~368-394, 525, 615 — history-response dicts with `status`/`outputs`
- lines ~532, 568, 621 — `http_post=lambda url, body: {"prompt_id": "..."}` inside test fixtures
- line ~543 — `assert artifact.meta == {"prompt_id": "PROMPT_ID"}`

(Exact line numbers may drift if T8 is re-run; rely on `rg` to find them at execution time.)

- [ ] **Step 4: Rewrite pattern — uniform application across all 34 tests**

For each occurrence:

**Pattern A — POST `/prompt` lambdas:**

```python
# BEFORE
http_post = lambda url, body: {"prompt_id": "p-123"}

# AFTER
from tests.engines.conftest import _load_comfy_fixture
http_post = lambda url, body: _load_comfy_fixture("prompt_submit.json")
```

If the test asserts on the magic string `"p-123"` or `"PROMPT_ID"`, change to:

```python
# BEFORE
assert artifact.meta["prompt_id"] == "p-123"

# AFTER
expected_id = _load_comfy_fixture("prompt_submit.json")["prompt_id"]
assert artifact.meta["prompt_id"] == expected_id
```

If the assertion is a strict equality on the whole meta dict (`assert artifact.meta == {"prompt_id": "PROMPT_ID"}`), relax to the relationship form (`meta["prompt_id"] == expected_id`) — the engine only contracts to round-trip `prompt_id`, not the entire dict shape.

**Pattern B — Single-call history responses:**

```python
# BEFORE
http_get = lambda url: {
    "p-123": {"status": "completed", "outputs": {"node_9": {"files": [{"filename": "clip.mp4"}]}}}
}

# AFTER
http_get = lambda url: _load_comfy_fixture("history_done.json")
```

If the test asserts on a specific node ID (e.g. `"node_9"`) or a specific filename, fetch them from the fixture at call site:

```python
response = _load_comfy_fixture("history_done.json")
prompt_id = next(iter(response))
node_id = next(iter(response[prompt_id]["outputs"]))  # "30" in this fixture
expected_filename = response[prompt_id]["outputs"][node_id]["gifs"][0]["filename"]
```

The captured `outputs["30"]` block in `history_done.json` will have its own structure — inspect it once and adapt assertions to whatever node-output schema is captured (likely `gifs` for Wan video output, not `files`). Step 5 catches any drift.

**Pattern C — Multi-response polling tests (running → completed):**

Some tests simulate the polling loop with two response shapes — first `{"p-123": {"status": "running"}}`, then `{"p-123": {"status": "completed", "outputs": ...}}`. For these, build a small in-test variant:

```python
DONE_FIXTURE = _load_comfy_fixture("history_done.json")
PROMPT_ID = next(iter(DONE_FIXTURE))

RUNNING = {PROMPT_ID: {"status": {"completed": False}, "outputs": {}}}
DONE = DONE_FIXTURE  # already terminal

responses = iter([RUNNING, DONE])
http_get = lambda url: next(responses)
```

Do NOT mutate the loaded fixture in place — copy or rebuild. Mutating the dict returned by `_load_comfy_fixture` taints subsequent calls in the same test process if pytest happens to keep the JSON cached (the helper re-reads on every call by design, but defensive immutability avoids the foot-gun).

**Pattern D — Status-string comparisons:**

Note that the captured fixture nests `completed` under a dict (`status: {status_str: "success", completed: True, messages: [...]}`), not as a flat string. Tests that compare `status == "completed"` or `status == "running"` should now read `body["status"]["completed"]` (bool). If a test specifically needed the flat string shape (which never matched production), this is the bug T8 surfaces — fix the test, not the fixture.

- [ ] **Step 5: Run the suite — find drift, fix, re-run**

```bash
pixi run pytest tests/engines/test_comfyui.py -v
```

Three legitimate failure classes and their fixes:

| Failure | Cause | Fix |
|---|---|---|
| `KeyError: 'node_9'` (or similar node id) | Test hard-coded a node id that doesn't exist in the captured fixture | Use the fixture's actual node id (`"30"` for `history_done.json`) at call site via `next(iter(...))` |
| `AssertionError: expected "completed", got {...}` | Test compared flat status string but fixture nests it | Update test to read `body["status"]["completed"]` (bool) |
| `AssertionError: meta["prompt_id"] == "p-123"` | Test pinned a magic string | Relax to `expected_id = _load_comfy_fixture("prompt_submit.json")["prompt_id"]` |

Anything else (e.g. a production code path reading a field not present in the fixture) is a real bug. If found, **commit the bug fix as a separate commit before the T8 commit** with a message like `fix(engines/comfyui): <description>` — keep the T8 commit clean refactor-only.

- [ ] **Step 6: Verify no hand-typed response shape remains**

```bash
rg '\{"prompt_id":\s*"[^"]+"\}' tests/engines/test_comfyui.py
rg '"outputs":\s*\{' tests/engines/test_comfyui.py
```

Expected:
- First command: empty (no inline POST responses).
- Second command: may match assertions like `assert ... in response["outputs"]` — those are reads, not constructions. Inspect each match; flag any that build a dict literal as response.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/engines/conftest.py tests/engines/test_comfyui.py
git add tests/engines/conftest.py tests/engines/test_comfyui.py
git commit -m "$(cat <<'EOF'
refactor(tests/engines): ComfyUI tests load from captured fixtures (Layer P T8)

34 tests in tests/engines/test_comfyui.py now load HTTP response shapes
from tests/engines/fixtures/comfyui/*.json via the new _load_comfy_fixture
helper in tests/engines/conftest.py.

Layer N's RunPod refactor (Phase 24) precedent: hand-typed dicts in tests
silently diverged from real API shape; live capture + replay locks the
contract. Item #3's live smoke captured prompt_submit.json + history_done.json
during the b05fcb3 GREEN MP4 run.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: ComfyUI shape-lockdown tests

**Goal:** Append two new tests to `tests/engines/test_comfyui.py` that pin the captured ComfyUI HTTP response contract.

**Files:**
- Modify: `tests/engines/test_comfyui.py` (+2 tests)

**Acceptance Criteria:**
- [ ] `test_comfyui_prompt_submit_shape` exists, asserts captured `prompt_submit.json` response has a non-empty string `prompt_id`.
- [ ] `test_comfyui_real_shape_required_keys` exists, asserts captured `history_done.json` response has exactly one top-level key (the prompt_id), that the body contains `status` (with `completed: True`) + non-empty `outputs` dict.
- [ ] No `view.json`-related test added (intentional — covered by existing tests at lines ~542 + ~635).
- [ ] `pixi run pytest tests/engines/test_comfyui.py -v -k "shape or required_keys"` → 2 passed.
- [ ] `pixi run pytest tests/engines/test_comfyui.py -v` → 36 passed (34 from T8 + 2 new).

**Verify:** `pixi run pytest tests/engines/test_comfyui.py -v -k "shape or required_keys"` → 2/2 pass.

**Steps:**

- [ ] **Step 1: Append both tests at the end of `tests/engines/test_comfyui.py`**

Place after the last existing test. The helper is already imported by T8's edits.

```python
def test_comfyui_prompt_submit_shape() -> None:
    """Captured POST /prompt response has a non-empty string prompt_id key.

    Shape-lockdown: future ComfyUI server-side renames or type changes
    around prompt_id fail loudly here, not silently in production.
    """
    response = _load_comfy_fixture("prompt_submit.json")
    assert "prompt_id" in response
    assert isinstance(response["prompt_id"], str)
    assert response["prompt_id"]  # non-empty


def test_comfyui_real_shape_required_keys() -> None:
    """Captured terminal GET /history/{id} response: status.completed=True + non-empty outputs.

    Shape-lockdown: future ComfyUI server-side restructure of the
    history response surfaces as a clear AssertionError on the
    affected field, not as a downstream KeyError in production code.
    """
    response = _load_comfy_fixture("history_done.json")
    assert len(response) == 1, (
        "history_done.json should be keyed by exactly one prompt_id"
    )
    prompt_id, body = next(iter(response.items()))
    assert isinstance(prompt_id, str) and prompt_id
    assert "status" in body, "missing 'status' field"
    assert body["status"].get("completed") is True, "status.completed != True"
    assert "outputs" in body, "missing 'outputs' field"
    assert isinstance(body["outputs"], dict)
    assert body["outputs"], "outputs dict empty"
```

- [ ] **Step 2: Run the two tests**

```bash
pixi run pytest tests/engines/test_comfyui.py -v -k "shape or required_keys"
```

Expected:

```
tests/engines/test_comfyui.py::test_comfyui_prompt_submit_shape PASSED
tests/engines/test_comfyui.py::test_comfyui_real_shape_required_keys PASSED
2 passed
```

- [ ] **Step 3: Run the full file to confirm 36 total**

```bash
pixi run pytest tests/engines/test_comfyui.py -v 2>&1 | tail -5
```

Expected: `36 passed`.

- [ ] **Step 4: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/engines/test_comfyui.py
git add tests/engines/test_comfyui.py
git commit -m "$(cat <<'EOF'
test(engines/comfyui): shape-lockdown for prompt_submit + history (Layer P T9)

Two new tests pin the ComfyUI HTTP response contract against the T8
captured fixtures. Future ComfyUI schema upgrades that drop prompt_id
or change history's status.completed shape fail loudly here.

View AC from the original Layer P plan dropped: /view returns binary
MP4 bytes (no JSON response to capture); production URL builder is
already locked at tests/engines/test_comfyui.py lines 542 + 635.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: README + PROGRESS Phase 28 + `layer-p-closed` tag

**Goal:** Document Layer P close-out (README user-facing sub-section + PROGRESS Phase 28 entry), reset Single Next Action, and create the annotated `layer-p-closed` git tag at the closure commit.

**Files:**
- Modify: `README.md`
- Modify: `PROGRESS.md`
- (Tag): `layer-p-closed` annotated tag at the T10 commit

**Acceptance Criteria:**
- [ ] README has an `#### Engine integration (ComfyUI + Wan i2v)` sub-section (heading level chosen to match the surrounding section depth — see Step 1) under the existing `### Real providers — RunPod` heading.
- [ ] README sub-section covers: required env vars (`RUNPOD_API_KEY`, `HF_TOKEN`), optional `KEEP_POD=1`, quickstart command using `examples/configs/wan.yaml`, KEEP_POD dev loop note, cost shape (~$0.27/hr; cold-boot 12–20 min; warm 5 min), pointer to example YAML + graph JSON.
- [ ] No internal-history content (no bug-catch trail, no design retrospective).
- [ ] PROGRESS has a `### Phase 28 — Layer P close-out (T8/T9/T10)` entry containing: per-task SHAs (T8/T9/T10), Layer P total live spend (~$0.74), test count delta (`1034 → 1036`), bug-catch list (one bullet each: prompt routing, init-fixture, sampler steps, batch_cli sink leak, orphan-pod L1+L2, morph 0.6), key design decisions, link to spec + plan paths.
- [ ] PROGRESS "Single next action" block reset — pointer to next candidate work (GitHub issue #9 aria2c fast-path is the standing recommendation; final pick is user's at execution time).
- [ ] **Do not touch** the "Layer-O carry-forward #1" line — already closed by Layer N (Phase 24, commit `454e514`); leave existing text intact.
- [ ] `git tag -l 'layer-p-closed'` empty BEFORE creating the tag; non-empty AFTER (verified by grep below).
- [ ] Annotated tag `layer-p-closed` exists at the T10 commit with message `Layer P closed: RunPod engine integration (ComfyUI + Wan i2v)`.
- [ ] Full offline gate: `pixi run pre-commit run --all-files && pixi run test && pixi run typecheck && pixi run lint` all green.
- [ ] `pixi run test` → `1036 passed, 3 skipped`.

**Verify:**

```bash
pixi run pre-commit run --all-files
pixi run test
pixi run typecheck
pixi run lint
rg 'Engine integration \(ComfyUI \+ Wan i2v\)' README.md
rg '^### Phase 28 — Layer P close-out' PROGRESS.md
git tag -l 'layer-p-closed' -n1
git log --oneline -1 layer-p-closed
```

Expected: gate green; rg matches return 1 line each; tag command shows the message; tag points at the T10 commit SHA.

**Steps:**

- [ ] **Step 1: Determine heading depth for the README sub-section**

```bash
rg -n '^#+ Real providers' README.md
```

Expected at commit `72bc6b7`:

```
272:## Real providers — fal.ai
295:### Real providers — RunPod
```

(That `## ... ### ...` pair is a pre-existing inconsistency in the README — `### RunPod` is logically a peer of `## fal.ai`, not a child. Do not fix this in T10; out of scope. Pick the new sub-section's heading depth as one level deeper than `### Real providers — RunPod`, i.e. `####`.)

- [ ] **Step 2: Find the exact insertion point in README**

```bash
rg -n '^##+ ' README.md | rg -A1 '### Real providers — RunPod'
```

Identify the next heading after `### Real providers — RunPod`. Insert the new sub-section above that next heading and below the last line of content under `### Real providers — RunPod`.

- [ ] **Step 3: Write the README sub-section**

Insert this block (after the current RunPod section's last content line, before the next heading). Substitute the actual `idle_timeout_s` value from `examples/configs/wan.yaml` in the dev-loop bullet (currently 25 min per commit `9e3075a`):

```markdown
#### Engine integration (ComfyUI + Wan i2v)

End-to-end RunPod → ComfyUI → Wan 2.1 i2v generation. Drives a real RunPod pod that boots ComfyUI with the kijai WanVideoWrapper graph and produces an MP4.

**Required env vars:**
- `RUNPOD_API_KEY` — RunPod REST API key (least-privilege; see "Credential safety in tests")
- `HF_TOKEN` — Hugging Face token (for on-pod model downloads)

**Optional env vars:**
- `KEEP_POD=1` — skip pod destroy on success so you can iterate without paying for cold boots (manual reap below)

**Quickstart:**

```bash
pixi run kinoforge generate \
  --config examples/configs/wan.yaml \
  --prompt "a cat turns into a woman" \
  --init-image tests/providers/fixtures/runpod/sample_init_frame.png
```

**Dev loop with KEEP_POD:**

```bash
KEEP_POD=1 pixi run kinoforge generate --config examples/configs/wan.yaml --prompt "..." --init-image ...
# iterate: tweak prompt / graph / params, re-run with the same KEEP_POD=1
# pod stays warm and auto-reaps after idle_timeout_s (configured at 25m in examples/configs/wan.yaml)
# manual reap:
pixi run kinoforge destroy <pod_id>
```

**Cost shape:**
- Pod: NVIDIA RTX 3090 @ ~$0.27/hr (varies by region/availability)
- Cold boot (first run; downloads model weights): ~12–20 min wall-clock, ~$0.05–0.09
- Warm reuse: ~5 min, ~$0.025
- Always run `pixi run preflight` before live spend (checks zero active pods, clean tree, creds present)

**Configuration files:**
- `examples/configs/wan.yaml` — Wan 2.1 i2v engine config (lifecycle, params, model entries)
- `examples/configs/wan_kijai_i2v.json` — kijai WanVideoWrapper API-format graph
```

- [ ] **Step 4: Find next free Phase number for PROGRESS**

```bash
rg -n '^### Phase [0-9]+' PROGRESS.md | tail -3
```

Expected at commit `72bc6b7`:

```
968:### Phase 25 — Layer O (user-facing output directory)
1007:### Phase 26 — Secret-Scanning Cleanup (post-Layer-Q housekeeping)
```

Also scan for Phase 27 (used by CI green recovery side-task per PROGRESS line ~480):

```bash
rg -n '^### Phase 27' PROGRESS.md
```

If matches → use Phase 28. If no matches → re-check structure; the spec assumes Phase 28 — if reality differs, use whichever next free integer is correct and update the commit message accordingly.

- [ ] **Step 5: Write the PROGRESS Phase 28 entry**

Append this block at the bottom of PROGRESS.md, after the last existing Phase entry, **before** the `## Single next action` block (which gets rewritten in Step 6).

Substitute actual SHAs for `<T8_SHA>`, `<T9_SHA>`, and `<T10_SHA>` (the T10 SHA is this commit; the other two are visible from `git log --oneline -3` at execution time).

```markdown
### Phase 28 — Layer P close-out (T8 / T9 / T10)

Layer P (RunPod engine integration: ComfyUI + Wan i2v) closes here. Phases 24–28 + the item #1, #2, #3 sub-plans + the ci-green-recovery + secret-scanning-cleanup all together comprise the Layer P arc shipped directly to `main` (no `build/layer-p` branch ever existed). Reference spec: `docs/superpowers/specs/2026-06-03-layer-p-closeout-design.md`; plan: `docs/superpowers/plans/2026-06-03-layer-p-closeout.md`.

**Per-task SHAs:**
- T8 (conftest helper + 34-test rewrite): `<T8_SHA>`
- T9 (2 shape-lockdown tests): `<T9_SHA>`
- T10 (this commit — README + PROGRESS + tag): `<T10_SHA>`

**Test count:** `1034 → 1036` passing (+2: T9 lockdowns).

**Total Layer P live spend across all sub-plans:** ~$0.74 (item #3 wave: T6 + diagnostic + capture + quality re-render + cat-fixture re-render + morph re-render; plus earlier smaller item #1/#2 spends).

**Bug-catch trail from the live wave (one bullet each):**
- Prompt routing: kijai `WanVideoTextEncode` uses `positive_prompt`, not `text` (`d455f93`).
- Sampler defaults: non-distilled Wan 2.1 needs `steps=20 cfg=6 shift=7 scheduler=unipc` (`d455f93`).
- Init-fixture: gradient PNG placeholder showed through as diagonal seam at t=0; replaced with real cat photo (`056abe4`).
- `batch_cli` sink leak: `output.dir` defaulted to repo root in tests (`c2d28e2`).
- Orphan-pod L1: in-process `_created_instances` registry in `RunPodProvider` (`93beb14`).
- Orphan-pod L2: `orchestrator.generate()` returns `tuple[Artifact, Instance | None]` so callers can teardown by id (`7a10fd4`).
- Subject-morph: `start_latent_strength=0.6` locked in node 63 for visible morph (`b7b4ff2`).

**Key design decisions surfaced during the wave:**
- kijai WanVideoWrapper graph treated as upstream truth — fetched at pinned SHA, validated by a SHA cross-reference test, not hand-edited.
- Fixture-replay as offline-contract pattern (T8/T9): captured real-server HTTP shapes drive offline tests; future server-side drift fails loudly.
- In-process pod registry + tuple-return orchestrator API as defence-in-depth against tag-discovery gaps in cold-start cloud-state APIs.

**Annotated tag:** `layer-p-closed` at this commit.

**Real-cloud verification gap closed:** ~~ComfyUI engine end-to-end against real RunPod compute~~ — Layer P ships the live shake-out + offline fixture lockdown.

**Carry-forwards (unchanged):**
- `SkyPilotProvider._get_sky()` lazy path still unexercised against real `sky` SDK.
- `S3ArtifactStore` + `GCSArtifactStore` never hit real cloud.
- (Other follow-ups per the "Known limitations & follow-ups" section above.)
```

- [ ] **Step 6: Reset the Single Next Action block in PROGRESS**

Locate the `## Single next action` block (currently around line 150 of PROGRESS.md per the commit `72bc6b7` view). Replace its contents with a fresh post-Layer-P block. Suggested content:

```markdown
## Single next action

### RESUME — START HERE

**Where we are:** 🏁 **Layer P CLOSED.** Tagged `layer-p-closed` at HEAD `<T10_SHA>`. Phase 28 entry above. Test suite at `1036 passed, 3 skipped`. Working tree clean.

**Read in this order:**
1. The Phase 28 entry directly above (per-task SHAs + bug-catch trail + design decisions).
2. `docs/superpowers/specs/2026-06-03-layer-p-closeout-design.md` for the brainstorming-locked decisions.
3. `git log --oneline -10` for the most-recent commits.

**First unchecked task in fresh session:** pick next layer. Standing recommendation: GitHub issue #9 — aria2c fast-path on the downloader. Small, contained, immediate payoff on every future Wan live run (current stdlib threadpool is the bottleneck on multi-GB model fetches). Alternatives: GitHub issue #8 (HF bare-repo listing), issue #4 (keyframe stage), real-cloud verification of SkyPilot or S3/GCS stores, or the orchestrator-status-reads-from-ledger architectural follow-up.

**Budget remaining: ~$11.26 of $15.** Layer P arc total live spend across all sub-plans: ~$0.74.
```

Do NOT delete the existing "Pointers" / "Phase" / "Task checklist" / "Key decisions & gotchas" / "Established patterns for layer development" / "Known limitations & follow-ups" / "GitHub issues status" sections that precede `## Single next action` — they stay intact.

- [ ] **Step 7: Pre-commit + commit (README + PROGRESS together)**

```bash
pixi run pre-commit run --files README.md PROGRESS.md
git add README.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs: Layer P closed — README + PROGRESS Phase 28 (Layer P T10)

README gains an "Engine integration (ComfyUI + Wan i2v)" sub-section
under "Real providers — RunPod" covering env vars, quickstart,
KEEP_POD dev loop, and cost shape.

PROGRESS Phase 28 entry documents the Layer P arc end-to-end:
per-task SHAs across T1–T10, item #1/#2/#3 live-smoke bug catches,
total live spend (~$0.74), test-count delta (1034 → 1036),
design decisions (kijai upstream truth, fixture-replay contract,
in-process pod registry + tuple-return orchestrator API).

Annotated tag `layer-p-closed` lands at this commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

After the commit lands, record the SHA — needed for the next step.

- [ ] **Step 8: Sanity-check that no `layer-p-closed` tag already exists**

```bash
git tag -l 'layer-p-closed'
```

Expected: empty output. If non-empty, abort and investigate (someone else tagged in parallel; resolve before continuing).

- [ ] **Step 9: Create the annotated tag**

```bash
git tag -a layer-p-closed -m "Layer P closed: RunPod engine integration (ComfyUI + Wan i2v)"
```

- [ ] **Step 10: Verify the tag points at HEAD (the T10 commit)**

```bash
git tag -l 'layer-p-closed' -n1
git log --oneline -1 layer-p-closed
git log --oneline -1
```

Expected: the tag message displays; the tag's commit SHA matches HEAD.

- [ ] **Step 11: Full offline gate**

```bash
pixi run pre-commit run --all-files
pixi run test
pixi run typecheck
pixi run lint
```

Expected: all green. `pixi run test` → `1036 passed, 3 skipped`.

- [ ] **Step 12: Final readback checks**

```bash
rg 'Engine integration \(ComfyUI \+ Wan i2v\)' README.md
rg '^### Phase 28 — Layer P close-out' PROGRESS.md
git tag -l 'layer-p-closed' -n1
git status
```

Expected: each `rg` returns one line; tag command shows the message; `git status` shows clean working tree.

---

## Out of scope

- No `git push` (origin push is user-driven, not part of this plan).
- No CI workflow changes.
- No production code changes in `src/kinoforge/`.
- No `examples/` changes.
- No live cloud spend (zero pods spawned).
- No fix to the pre-existing README heading-depth inconsistency (`## fal.ai` vs `### RunPod`).
- No closure of the Layer-O carry-forward #1 line (already closed by Layer N).

---

## Self-review

**Spec coverage:** every spec section maps to a task —
- Spec §3 (T8 helper + 34-test rewrite) → Task 8.
- Spec §4 (T9 shape-lockdown) → Task 9.
- Spec §5 (T10 README + PROGRESS + tag) → Task 10.
- Spec §6 (out of scope) → "Out of scope" section here.
- Spec §7 (risks + mitigations) → encoded into Step 5 of Task 8 (drift fix decision tree) + Step 8 of Task 10 (tag-collision check).
- Spec §8 (acceptance criteria rollup) → distributed across each task's Acceptance Criteria.
- Spec §9 (sequencing — three commits) → reflected in task ordering + per-task commit step.

**Placeholder scan:** no `TBD` / `TODO` / "add error handling" / "fill in details" patterns. Three explicit substitution points are marked with `<T8_SHA>`, `<T9_SHA>`, `<T10_SHA>` — these are intended placeholders for SHAs not yet existing; clearly labeled and only used inside PROGRESS body text.

**Type consistency:** `_load_comfy_fixture(name: str) -> dict[str, Any]` signature identical between Task 8 (Step 1 file content) and Task 9 (Step 1 test bodies). Fixture file names (`prompt_submit.json`, `history_done.json`) consistent across both tasks and the spec. Phase number (`Phase 28`) consistent between Task 10 acceptance criteria, Step 5 entry header, and Step 12 readback rg.

**Test count consistency:** T8 acceptance says "34 passed". T9 acceptance says "36 passed (34 from T8 + 2 new)". T10 acceptance says "1036 passed, 3 skipped". Math: prior suite shape per PROGRESS = `1034 passed, 3 skipped`; T9 adds 2 → `1036, 3`. Consistent.

No issues found.
