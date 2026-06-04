# Layer P close-out — T8 / T9 / T10 design

**Date:** 2026-06-03
**Branch:** `main` (Layer P shipped directly to main across item #1/#2/#3 sub-plans; no `build/layer-p` branch ever existed)
**Predecessor specs / plans:**
- `docs/superpowers/plans/2026-06-01-layer-p-runpod-engine-integration.md` — original Layer P plan (Tasks 1–10)
- `docs/superpowers/plans/2026-06-02-layer-p-task7-item3-resume.md` — item #3 resume sub-plan (closed at HEAD `72bc6b7`)

**State going in:** HEAD `72bc6b7`. Suite shape `1034 passed, 3 skipped`. Item #3 wave SHIPPED. Layer P T8/T9/T10 unblocked.

---

## 1. Why this work

The original Layer P plan ended at T10 with a `--no-ff` merge of `build/layer-p` into `main`. That branch was never created — every Layer P sub-plan (item #1, item #2, item #3, ci-green-recovery, secret-scanning-cleanup) committed straight to `main`. T10's merge step is moot.

What still has to ship:

- **T8** — refactor offline ComfyUI tests onto captured fixtures (the 11 net-new tests landed since the original plan brought the file from 23 to 34 tests; current shape is hand-typed dicts that have repeatedly diverged from real-server reality — the "8-bug-catch trail" from item #3).
- **T9** — shape-lockdown tests against the captured fixtures (down-scoped from 3 ACs to 2 — see §3).
- **T10** — close-out docs (README sub-section + PROGRESS Phase 28 entry) + an annotated `layer-p-closed` git tag.

After this, Layer P is closed and the next layer (likely GitHub issue #9 aria2c fast-path, but user choice at that point) is unblocked.

---

## 2. Brainstorming decisions locked

| Decision | Choice | Reasoning |
|---|---|---|
| T9 view AC handling | Drop entirely | `/view` returns binary MP4 bytes, no JSON to capture. URL format already locked at `tests/engines/test_comfyui.py:542` + `:635`. The original AC was a misconception. |
| T8 scope (rewrite breadth) | Rewrite all 34 tests uniformly | Every test passes through `_load_comfy_fixture`. No "which pattern do I use here?" ambiguity for future contributors. |
| T10 merge ceremony | README + PROGRESS + annotated `layer-p-closed` tag (no merge) | `build/layer-p` never existed; tag marks the layer boundary in git history. |
| README depth | User-facing how-to only | Env vars, quickstart, KEEP_POD dev loop, cost shape. Internal bug-catch history stays in PROGRESS Phase 28 + per-commit messages. |
| Sequencing | Three separate commits, sequential, in main session | Per-task SHA isolation matches PROGRESS pattern. Bisectable. No subagent dispatch. |

---

## 3. T8 — fixture-replay helper + 34-test rewrite

### Architecture

Two files touched:

1. **Create:** `tests/engines/conftest.py` — new file holding the shared `_load_comfy_fixture` helper.
2. **Modify:** `tests/engines/test_comfyui.py` — all 34 tests refactored uniformly to load from captured fixtures.

Existing captures (committed during item #3 closure, currently at `tests/engines/fixtures/comfyui/`):

- `prompt_submit.json` — captured `POST /prompt` response from live RunPod run (HEAD `b05fcb3`).
- `history_done.json` — captured terminal `GET /history/{prompt_id}` response from the same run.
- (`last_smoke.json` is gitignored per commit `3bfaf6f`; not used as a test fixture.)

### Helper signature

```python
# tests/engines/conftest.py
"""Shared pytest helpers for the engines/ test suite.

Adds the Layer P fixture-replay helper for ComfyUI offline tests. Mirrors
the Layer N pattern in tests/providers/conftest_runpod.py.
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
        captured for forensic value, not asserted on).
    """
    with (_COMFY_FIXTURE_DIR / name).open() as f:
        return dict(json.load(f)["response"])
```

### Fixture mapping

| Test asserts on… | Fixture file |
|---|---|
| `POST /prompt` response shape (`prompt_id`) | `prompt_submit.json` |
| `GET /history/{id}` terminal response (`status.completed`, `outputs.<node>.gifs[…]`) | `history_done.json` |

Tests that exercise both call sites in sequence load both fixtures.

### Rewrite pattern (applied uniformly to all 34 tests)

```python
# BEFORE (hand-typed)
http_post = lambda url, body: {"prompt_id": "abc-123"}

# AFTER (fixture-loaded)
from tests.engines.conftest import _load_comfy_fixture
http_post = lambda url, body: _load_comfy_fixture("prompt_submit.json")
```

Tests asserting on specific magic values (e.g. `prompt_id == "abc-123"`) get relaxed to relationship asserts (`prompt_id == response["prompt_id"]`) — captures the engine's round-trip contract without pinning a value the real fixture won't carry.

### Verify

```bash
pixi run pytest tests/engines/test_comfyui.py -v
```

Expected: all 34 still pass. Any failure means either (a) a relaxed assertion needs updating to match the captured value, or (b) a production bug was masked by the prior hand-typed fake — both legitimate T8 deliverables; the latter gets a separate fix commit before the T8 commit.

### Commit message

```
refactor(tests/engines): ComfyUI tests load from captured fixtures (Layer P T8)

34 tests in tests/engines/test_comfyui.py now load HTTP response shapes
from tests/engines/fixtures/comfyui/*.json via the new _load_comfy_fixture
helper in tests/engines/conftest.py.

Layer N's RunPod refactor (Phase 24) precedent: hand-typed dicts in tests
silently diverged from real API shape; live capture + replay locks the
contract. Item #3's live smoke captured prompt_submit.json + history_done.json
during the b05fcb3 GREEN MP4 run.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## 4. T9 — shape-lockdown tests

### Architecture

Two new tests appended to `tests/engines/test_comfyui.py`. No new files. Both use `_load_comfy_fixture` from T8 (so T9 depends on T8 landing first).

**View AC dropped** per brainstorming Q1. Production URL builder is already locked at:

- `tests/engines/test_comfyui.py:542` — `assert artifact.url == "http://localhost:8188/view?filename=clip.mp4&type=output"`
- `tests/engines/test_comfyui.py:635` — `… == "http://localhost:8188/view?filename=clip%20frame%2601.mp4&type=output"` (percent-encoding)

The `/view` endpoint returns binary MP4 bytes — there's no JSON response to lockdown, and re-asserting on the string builder would just duplicate the two existing tests.

### Tests

```python
def test_comfyui_prompt_submit_shape() -> None:
    """Captured POST /prompt response has a string prompt_id key."""
    response = _load_comfy_fixture("prompt_submit.json")
    assert "prompt_id" in response
    assert isinstance(response["prompt_id"], str)
    assert response["prompt_id"]  # non-empty


def test_comfyui_real_shape_required_keys() -> None:
    """Captured GET /history/{id} terminal response: status.completed=True + non-empty outputs."""
    response = _load_comfy_fixture("history_done.json")
    assert len(response) == 1, "history_done.json should be keyed by a single prompt_id"
    prompt_id, body = next(iter(response.items()))
    assert isinstance(prompt_id, str) and prompt_id
    assert "status" in body, "missing 'status' field"
    assert body["status"].get("completed") is True, "status.completed != True"
    assert "outputs" in body, "missing 'outputs' field"
    assert isinstance(body["outputs"], dict)
    assert body["outputs"], "outputs dict empty"
```

### Purpose

Future ComfyUI server-side schema changes (key renames, type swaps, restructure) fail loudly on these two tests rather than silently breaking many of the 34 in §3 at once. Single source of "what's the contract" for the engine's HTTP boundary.

### Verify

```bash
pixi run pytest tests/engines/test_comfyui.py -v -k "shape or required_keys"
```

Expected: 2/2 pass.

### Commit message

```
test(engines/comfyui): shape-lockdown for prompt_submit + history (Layer P T9)

Two new tests pin the ComfyUI HTTP response contract against the T8
captured fixtures. Future ComfyUI schema upgrades that drop prompt_id or
change history's status.completed shape will fail loudly here.

View AC from original Layer P plan dropped: /view returns binary MP4
bytes (no JSON response to capture); production URL builder already
locked at tests/engines/test_comfyui.py:542 + :635.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## 5. T10 — README + PROGRESS Phase 28 + `layer-p-closed` tag

### README sub-section

Insert under the existing "Real providers — RunPod" heading in `README.md`. New `### Engine integration (ComfyUI + Wan i2v)` sub-section. Scope per brainstorming Q4 — user-facing how-to only.

Content:

- **Env vars:** `RUNPOD_API_KEY` (required), `HF_TOKEN` (for model downloads on the pod), optional `KEEP_POD=1` for the dev loop.
- **Quickstart command:**
  ```bash
  pixi run kinoforge generate --config examples/configs/wan.yaml \
    --prompt "a cat turns into a woman" \
    --init-image tests/providers/fixtures/runpod/sample_init_frame.png
  ```
- **KEEP_POD dev loop:** when set, the live test skips destroy on success so the pod stays warm for iteration. Manual cleanup via `pixi run kinoforge destroy <pod_id>` or wait for `idle_timeout_s` (configured in `examples/configs/wan.yaml`).
- **Cost shape:** typical pod RTX 3090 ~$0.27/hr. Cold-boot first run including model download: ~12–20 min wall-clock (~$0.05–0.09). Warm reuse: ~5 min (~$0.025).
- **Pointer:** see `examples/configs/wan.yaml` for the configured params and `examples/configs/wan_kijai_i2v.json` for the graph.

No internal-history content, no design retrospective, no bug-catch trail.

### PROGRESS Phase 28 entry

Phase 28 is the next free number (Phase 26 = Secret-Scanning Cleanup; Phase 27 = CI green recovery side-task).

Contents:

- One-line phase summary.
- Per-task SHA list (T8 / T9 / T10 commits).
- Total Layer P live spend across all sub-plans (~$0.74 = item #3 wave + the smaller item #1 / item #2 spends documented earlier in PROGRESS).
- Test-count delta: `1034 → 1036`.
- Live-smoke bug-catch list (one bullet each): prompt routing (`positive_prompt` not `text`), init-fixture (gradient placeholder), sampler steps (4→20 cfg 1→6), batch_cli sink leak, orphan-pod L1 (in-process registry) + L2 (orchestrator tuple return), morph strategy locked at `start_latent_strength=0.6`.
- Key design decisions: kijai workflow as upstream truth (fetched + SHA-pinned); fixture-replay as offline contract pattern; in-process pod registry + tuple-return orchestrator API as defence in depth against tag-discovery gaps.
- Mark Layer P closed in the "Real-cloud verification gaps" / "Architectural follow-ups" sections as appropriate.
- "Single next action" block reset — point at next candidate work (GitHub issue #9 aria2c fast-path is the standing recommendation, but final pick is user's at the time).

**Skip the legacy "close Layer-O carry-forward #1" step** — already closed by Layer N (Phase 24, merge commit `454e514`). The original Layer P plan instruction was stale.

### Git tag

```bash
git tag -a layer-p-closed -m "Layer P closed: RunPod engine integration (ComfyUI + Wan i2v)"
```

Annotated (not lightweight) so `git tag -n` surfaces the message. Tag points at the T10 commit (the closure commit).

### Verify

```bash
pixi run pre-commit run --all-files
pixi run test                                       # expect: 1036 passed, 3 skipped
pixi run typecheck
pixi run lint
rg 'Engine integration \(ComfyUI \+ Wan i2v\)' README.md
rg '^### Phase 28' PROGRESS.md
git tag -l 'layer-p-closed' -n1                     # surfaces tag message
git log --oneline -1 layer-p-closed                 # tag points at T10 commit
```

### Commit message

```
docs: Layer P closed — README + PROGRESS Phase 28 (Layer P T10)

README gains "Engine integration (ComfyUI + Wan i2v)" sub-section
under "Real providers — RunPod" with env vars, quickstart, KEEP_POD
dev loop, and cost shape.

PROGRESS Phase 28 entry documents the Layer P arc end-to-end:
per-task SHAs across T1–T10, item #1/#2/#3 live-smoke bug catches,
total live spend (~$0.74), test-count delta (1034 → 1036),
design decisions (kijai upstream truth, fixture-replay contract,
in-process pod registry + tuple-return orchestrator API).

Annotated tag `layer-p-closed` lands at this commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## 6. Out of scope

- No live-cloud spend. T8/T9/T10 are pure offline work.
- No production code changes in `src/kinoforge/`. Tests + docs only.
- No new GitHub issues opened.
- No CI workflow changes.
- No new examples committed (existing `examples/configs/wan.yaml` is the README's reference).

---

## 7. Risks + mitigations

| Risk | Mitigation |
|---|---|
| T8 rewrite surfaces a previously-masked production bug. | Acceptable + expected. Fix lands as a separate commit before the T8 commit, with a normal bug-fix commit message. T8 commit description notes the precondition. |
| Captured fixtures missing fields the production code reads in some test paths. | T8 step 5 (`rg` cleanup verify) plus full-suite pytest catches mismatches. **Never mutate the captured fixture to satisfy a test** — that taints the lockdown. Instead: adjust the test (relax over-tight assertion) or fix the production code (if it was reading a field the real server never sends). Re-capture is a last resort and triggers a follow-up live spend, out of scope here. |
| Phase 28 number turns out to be taken between brainstorm and execution. | Plan-writing step verifies free phase number via `rg '^### Phase' PROGRESS.md \| tail -3` immediately before writing the entry. |
| `layer-p-closed` tag name collides with an existing tag. | `git tag -l 'layer-p-closed'` check before creating; abort + escalate if non-empty. |

---

## 8. Acceptance criteria (rollup)

- [ ] `tests/engines/conftest.py` exists with `_load_comfy_fixture(name) -> dict[str, Any]`.
- [ ] All 34 tests in `tests/engines/test_comfyui.py` use `_load_comfy_fixture` — no hand-typed `{"prompt_id": …}` or `{"<id>": {"outputs": …}}` literals remain in test bodies.
- [ ] Two new shape-lockdown tests added (`test_comfyui_prompt_submit_shape`, `test_comfyui_real_shape_required_keys`). View AC absent (intentional).
- [ ] README has an `### Engine integration (ComfyUI + Wan i2v)` sub-section under "Real providers — RunPod" covering env vars, quickstart, KEEP_POD loop, cost shape.
- [ ] PROGRESS has a Phase 28 entry with per-task SHAs, live spend total, test count delta, bug-catch list, design decisions.
- [ ] PROGRESS "Single next action" reset post-Layer-P.
- [ ] Annotated `layer-p-closed` git tag exists at the T10 commit.
- [ ] Full offline gate: `pixi run test && pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files` green.
- [ ] Test count: 1036 passed, 3 skipped.
- [ ] Working tree clean; HEAD on `main`.

---

## 9. Sequencing

Three commits, in order, on `main`:

1. T8 commit — conftest helper + 34-test rewrite.
2. T9 commit — 2 shape-lockdown tests (depends on T8's helper).
3. T10 commit — README + PROGRESS + Phase 28; then `git tag -a layer-p-closed`.

Each commit independently passes the offline gate (`pixi run test && pre-commit`). Bisectable.
