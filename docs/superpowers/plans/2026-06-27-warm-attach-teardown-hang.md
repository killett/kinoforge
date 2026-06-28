# Warm-attach teardown hang — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `kinoforge --ephemeral generate` warm-attach run #2 subprocess exit cleanly after `generate completed`, so the existing live smoke `tests/live/test_runpod_ephemeral_warm_reuse_smoke.py` passes end-to-end (destroy + post-cleanup reach GREEN).

**Architecture:** Differential debug. Run #1 (cold-boot, `instance=None`) exits cleanly; run #2 (warm-attach, `instance != None`) hangs under `EphemeralSession`. The bug lives in a single branch reachable only when `instance is not None` during the orchestrator's `deploy_session` exit. We reproduce the hang offline with pytest+faulthandler, read the dumped frame to localize the bug, ship the smallest possible branch-gated fix, then confirm with one live RunPod run.

**Tech Stack:** Python 3.12, pixi, pytest (with faulthandler), ruff, mypy, RunPod GraphQL.

**User decisions (already made):**
- Goal of follow-up: fix the orchestrator hang at root cause (Q1=A).
- Investigation strategy: offline-first with one final live confirmation (Q2=A).
- Definition of done: offline regression test RED→GREEN, full regression suites green, **live smoke passes end-to-end including destroy + post-cleanup** (Q3=A).

---

## File structure

```
tests/integration/
    test_ephemeral_warm_attach_exits_cleanly.py   # NEW — offline repro + regression
src/kinoforge/core/
    orchestrator.py                               # MOD — fix in (instance!=None, single=False, ephemeral=on) exit branch
tests/live/
    test_runpod_ephemeral_warm_reuse_smoke.py     # UNCHANGED — verification target
    _warm_attach_teardown_fix_evidence_run1.log   # NEW — live evidence (force-added)
    _warm_attach_teardown_fix_evidence_run2.log   # NEW — live evidence (force-added)
    _warm_attach_teardown_fix_evidence.json       # NEW — AC summary
PROGRESS.md                                       # MOD — close workstream
```

---

## Task 1: Write the offline RED repro test

**Goal:** A pytest test that exercises `_cmd_generate` on the warm-attach + ephemeral path with mocked compute/engine and asserts a clean exit within 30 s. Today this test must hang (RED via pytest faulthandler).

**Files:**
- Create: `tests/integration/test_ephemeral_warm_attach_exits_cleanly.py`

**Acceptance Criteria:**
- [ ] Test exercises `_cmd_generate(args, ctx)` inside `with EphemeralSession(enabled=True):`.
- [ ] Test pre-seeds a real `EphemeralIndex` row + matching disk state so `_scan_warm_candidates` returns a non-`None` instance without network.
- [ ] Test stubs `kinoforge.core.registry.get_provider("runpod")` with a fake provider whose `get_instance`, `list_instances`, `destroy_instance` are pure-Python no-ops returning the seeded Instance.
- [ ] Test stubs the engine's `generate` to write a small stub artifact and `wait_for_ready` to return immediately.
- [ ] Test runs with `pytest --faulthandler-timeout=30` (or pytest config) so a hang dumps the stack trace.
- [ ] Pre-fix: test FAILS with `Timeout (0:00:30)` + traceback pointing at the hung frame.
- [ ] Test is committed RED with an `@pytest.mark.xfail(reason="repro for orchestrator warm-attach teardown hang; flips on Task 2 fix")` so the suite stays green.

**Verify:** `pixi run pytest tests/integration/test_ephemeral_warm_attach_exits_cleanly.py -v --faulthandler-timeout=30` → XFAIL with timeout-traceback in captured output.

**Steps:**

- [ ] **Step 1: Inspect the production call shape**

Read these surfaces to mirror them exactly in the test:
- `src/kinoforge/cli/_commands.py::_cmd_generate` — argument shape, ctx contract, _scan_warm_candidates call site, orchestrator.generate call site.
- `src/kinoforge/cli/_main.py::main` + `_build_parser` — argparse Namespace fields the cmd reads (mode, prompt, config, instance_id, force_attach, no_reuse, env_file, skip_preflight, dry_run_swap, run_id, output_dir, no_output_dir, …).
- `src/kinoforge/core/ephemeral.py::EphemeralSession` — constructor + current() lookup contract.
- `src/kinoforge/core/registry.py` — get_provider / register_provider for runtime swap.

- [ ] **Step 2: Write failing test scaffold**

Create `tests/integration/test_ephemeral_warm_attach_exits_cleanly.py`:

```python
"""Offline repro for the warm-attach teardown hang under --ephemeral.

Predecessor live smoke (2026-06-27) showed run #2 subprocess hangs
after emitting "generate completed" on the warm-attach path. Run #1
(cold-boot, instance=None) does NOT hang; run #2 (warm-attach,
instance != None) does. This test isolates the warm-attach branch
with mocked compute/engine so the hang reproduces in pytest.

Pre-fix: hangs ≥30s, faulthandler dumps stack, XFAIL.
Post-fix: exits <5s, XFAIL flips to XPASS (then the @xfail decorator
is removed in Task 2's final commit).
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kinoforge.cli._commands import _cmd_generate
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.interfaces import Instance
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _FakeProvider:
    """Pure-Python pod stand-in for the warm-attach path."""

    def __init__(self, instance: Instance) -> None:
        self._instance = instance

    def list_instances(self) -> list[Instance]:
        return [self._instance]

    def get_instance(self, instance_id: str) -> Instance:
        if instance_id != self._instance.id:
            raise KeyError(instance_id)
        return self._instance

    def destroy_instance(self, instance_id: str) -> None:
        return None

    def endpoints(self, instance: Instance) -> dict[str, str]:
        return dict(instance.endpoints)


def _seeded_instance(pod_id: str = "fake-pod") -> Instance:
    return Instance(
        id=pod_id,
        provider="runpod",
        status="ready",
        created_at=0.0,
        tags={"mode": "pod", "ports": "8188"},
        cost_rate_usd_per_hr=0.16,
        endpoints={"8188": f"https://{pod_id}-8188.proxy.runpod.example.invalid"},
    )


@pytest.fixture
def fake_state_dir(tmp_path: Path) -> Path:
    state = tmp_path / ".kinoforge"
    state.mkdir(parents=True)
    return state


@pytest.fixture
def seeded_ctx(fake_state_dir: Path) -> tuple[Any, Instance]:
    """Build a SessionContext + EphemeralIndex row that _scan_warm_candidates
    will discover under --ephemeral."""
    from kinoforge.cli._main import SessionContext  # adapt import to actual location

    instance = _seeded_instance()
    store = LocalArtifactStore(fake_state_dir)
    EphemeralIndex(store=store).add(
        EphemeralIndexRow(
            id=instance.id,
            warm_attach_key="wak-X",
            kinoforge_key="cap123456789",  # any 12-char prefix
            endpoints=dict(instance.endpoints),
            provider="runpod",
            created_at_local="2026-06-27T18:00:00",
        )
    )
    # Build SessionContext from a stub cfg path tmp_path / "cfg.yaml" if
    # the real ctor needs a file on disk. Adapt to actual SessionContext
    # surface during Step 1 inspection.
    ctx = SessionContext.from_args(
        state_dir=fake_state_dir,
        cfg_path=fake_state_dir / "cfg.yaml",  # write a minimal cfg here
    )
    return ctx, instance


@pytest.mark.xfail(
    reason=(
        "Reproduces the warm-attach teardown hang; flips XPASS after Task 2 "
        "fix in orchestrator.deploy_session exit. Remove this decorator in "
        "Task 2's final commit."
    ),
    run=True,
    strict=False,
)
def test_warm_attach_exits_cleanly_under_ephemeral(
    seeded_ctx: tuple[Any, Instance],
    fake_state_dir: Path,
) -> None:
    """Bug: --ephemeral warm-attach subprocess hangs after generate completed."""
    ctx, instance = seeded_ctx
    provider = _FakeProvider(instance)

    args = argparse.Namespace(
        config=str(fake_state_dir / "cfg.yaml"),
        prompt="test prompt",
        mode="t2v",
        run_id=None,
        output_dir=None,
        no_output_dir=False,
        instance_id=None,
        force_attach=False,
        no_reuse=False,
        skip_preflight=True,
        dry_run_swap=False,
        env_file=None,
        loras=None,
        ephemeral=True,
    )

    with (
        patch(
            "kinoforge.core.registry.get_provider",
            return_value=lambda: provider,
        ),
        patch(
            "kinoforge.engines.comfyui.ComfyUIEngine.generate",
            return_value={"artifact_uri": str(fake_state_dir / "stub.mp4")},
        ),
        patch(
            "kinoforge.engines.comfyui.ComfyUIEngine.wait_for_ready",
            return_value=None,
        ),
        EphemeralSession(enabled=True),
    ):
        rc = _cmd_generate(args, ctx)
        assert rc == 0, f"expected clean exit, got rc={rc}"
```

Adjust patch targets to match the actual SessionContext / engine surface (see Step 1). If `SessionContext.from_args` requires a real yaml on disk, write a minimal 5-line cfg (provider=runpod, engine=comfyui, base model placeholder) in the fixture. The smallest cfg that passes load_config without network is the one to use; copy the shape from `tests/cli/test_dry_run_swap.py::_FakeCfg`.

- [ ] **Step 3: Run pre-fix; confirm hang + XFAIL**

Run: `pixi run pytest tests/integration/test_ephemeral_warm_attach_exits_cleanly.py -v --faulthandler-timeout=30 -s`
Expected: XFAIL with `Timeout (0:00:30)!` and a captured Python stack trace identifying the hung frame inside `orchestrator.deploy_session` or `_cmd_generate` post-`generate completed`. Record the frame's file:line in the commit message — that's the input to Task 2.

If NO hang reproduces, switch to fallback: replace the mocked heartbeat with a real `HeartbeatLoop` thread backed by a fake util endpoint that blocks indefinitely. See spec R1.

- [ ] **Step 4: Lint + type-check**

Run: `pixi run ruff check tests/integration/test_ephemeral_warm_attach_exits_cleanly.py && pixi run mypy tests/integration/test_ephemeral_warm_attach_exits_cleanly.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_ephemeral_warm_attach_exits_cleanly.py
git commit -m "$(cat <<'EOF'
test(integration): offline RED repro for --ephemeral warm-attach teardown hang

Reproduces the bug observed in the 2026-06-27 live smoke: run #2
subprocess hangs after "generate completed" on the warm-attach path
(instance != None) under EphemeralSession. Cold-boot path is unaffected.

Test is XFAIL until Task 2's fix in orchestrator.deploy_session exit.
Faulthandler dump captures the hung frame: <FILE:LINE> (record from
Step 3 output).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/integration/test_ephemeral_warm_attach_exits_cleanly.py"], "verifyCommand": "pixi run pytest tests/integration/test_ephemeral_warm_attach_exits_cleanly.py -v --faulthandler-timeout=30 -s", "acceptanceCriteria": ["exercises _cmd_generate inside EphemeralSession", "seeds real EphemeralIndex row + stubbed provider/engine", "pre-fix XFAILs with faulthandler timeout traceback", "lint + mypy clean"], "modelTier": "standard"}
```

---

## Task 2: Identify hang frame + ship smallest branch-gated fix

**Goal:** Read the faulthandler dump from Task 1, identify the offending frame in `src/kinoforge/core/orchestrator.py` (or adjacent), ship the smallest possible diff that makes the offline test GREEN, branch-gated so cold-boot and non-ephemeral paths stay byte-identical.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (likely; may extend to `kinoforge.core.lifecycle.HeartbeatLoop` or `kinoforge.core.ephemeral`)
- Modify: `tests/integration/test_ephemeral_warm_attach_exits_cleanly.py` (remove `@pytest.mark.xfail` decorator in final commit)

**Acceptance Criteria:**
- [ ] Task 1's test passes (no `@xfail`) within 5 s under `pixi run pytest --faulthandler-timeout=30`.
- [ ] Fix is branch-gated to `(EphemeralSession.current() is not None) and (instance is not None) and (single is False)` — verified by grepping every conditional touched.
- [ ] Diff is minimal: prefer changing one condition / one `.join(timeout=…)` / one finally block over restructuring the orchestrator.
- [ ] `pixi run pytest tests/core tests/cli tests/integration -x` stays green (no regression in cold-boot or non-ephemeral paths).
- [ ] `pixi run ruff check src/kinoforge/core/orchestrator.py && pixi run mypy src/kinoforge/core/orchestrator.py` clean.

**Verify:** `pixi run pytest tests/integration/test_ephemeral_warm_attach_exits_cleanly.py tests/core tests/cli tests/integration -x` → all GREEN.

**Steps:**

- [ ] **Step 1: Read the dumped frame**

From Task 1's RED run capture the `File "/workspace/src/kinoforge/core/orchestrator.py", line NNN in FRAME` line. That's the hang point.

- [ ] **Step 2: Differential read**

Open `src/kinoforge/core/orchestrator.py`. Read `deploy_session` end-to-end. Grep every `instance is None` / `instance is not None` / `single` in the file. The hang frame from Step 1 narrows the candidate to one branch. Read its neighbors. Identify the call that blocks: `Thread.join()` without timeout, `lock.acquire()` without timeout, a `while True` waiting for a sentinel that the warm-attach path never sets, etc.

- [ ] **Step 3: Write the fix**

Apply the smallest diff. Guidance:
- If `Thread.join()` blocks → add `timeout=` + a follow-up `is_alive()` log line.
- If a `with` context manager's `__exit__` blocks → wrap the offending call in a try/finally that ALWAYS releases the held resource.
- If a sentinel flag is never set on warm-attach → set it in the warm-attach branch of `_cmd_generate` before returning, or in `deploy_session.__enter__`.

Branch-gate aggressively. The fix must:
- Touch only code reachable when `EphemeralSession.current() is not None` AND the warm-attach `instance != None` flag is set, OR
- Have a default value that preserves bit-identical behavior for the cold-boot + non-ephemeral paths.

- [ ] **Step 4: Run the offline test**

Run: `pixi run pytest tests/integration/test_ephemeral_warm_attach_exits_cleanly.py -v --faulthandler-timeout=30`
Expected: XPASS (still has @xfail decorator from Task 1). If GREEN, remove the decorator now and re-run — should be PASS.

- [ ] **Step 5: Run cold-boot + non-ephemeral regression**

Run: `pixi run pytest tests/core/test_ephemeral_only_output_dir_survives.py tests/core/test_lifecycle.py tests/core/test_lifecycle_sweeper.py tests/core/test_warm_reuse_matcher.py tests/core/test_warm_reuse_integration.py tests/core/warm_reuse/ tests/cli/test_dry_run_swap.py -v`
Expected: all green. If any cold-boot/non-ephemeral test fails, the fix is NOT branch-gated tightly enough — go back to Step 3.

- [ ] **Step 6: Full suite**

Run: `pixi run pytest tests/core tests/cli tests/integration -x`
Expected: green. Known unrelated failure: `tests/test_examples.py::test_readme_contains_heading` (README rewrite vs old heading test; pre-existing, not blocking).

- [ ] **Step 7: Lint + mypy**

Run: `pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/integration/test_ephemeral_warm_attach_exits_cleanly.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/integration/test_ephemeral_warm_attach_exits_cleanly.py
git commit -m "$(cat <<'EOF'
fix(orchestrator): warm-attach teardown exits cleanly under --ephemeral

<one-line description of the offending frame from Step 1 dump>

<one-paragraph description of the fix, branch-gating, and why it
preserves byte-identical behavior for cold-boot + non-ephemeral paths>

Removes the @pytest.mark.xfail decorator on the Task 1 offline repro
test; it now passes <5s. Full tests/core tests/cli tests/integration
suite stays green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/core/orchestrator.py", "tests/integration/test_ephemeral_warm_attach_exits_cleanly.py"], "verifyCommand": "pixi run pytest tests/integration/test_ephemeral_warm_attach_exits_cleanly.py tests/core tests/cli tests/integration -x", "acceptanceCriteria": ["Task 1 test passes <5s with @xfail removed", "fix branch-gated to ephemeral+warm-attach", "no regression in cold-boot or non-ephemeral", "lint + mypy clean"], "modelTier": "standard"}
```

---

## Task 3: Full regression sweep + sanity check

**Goal:** Verify the fix did not regress any committed test, including the predecessor warm-reuse workstream's offline tests.

**Files:** (no source changes — verification only)

**Acceptance Criteria:**
- [ ] `pixi run pytest tests/core tests/cli tests/integration -q` green (modulo the pre-existing README-headings unrelated failure).
- [ ] `pixi run pytest tests/core/warm_reuse/ tests/integration/test_ephemeral_cross_session_warm_reuse.py tests/integration/test_non_ephemeral_consumes_index.py tests/core/test_non_ephemeral_does_not_write_index.py tests/test_ephemeral_index_write_gated.py -v` all green (predecessor workstream protected).
- [ ] `pixi run pre-commit run --all-files` exits 0 (modulo the known README-heading pre-existing).

**Verify:** Three commands above.

**Steps:**

- [ ] **Step 1: Run the offline regression sweep**

Run: `pixi run pytest tests/core tests/cli tests/integration -q`
Expected: green (allow `tests/test_examples.py::test_readme_contains_heading` as known pre-existing).

- [ ] **Step 2: Run the warm-reuse predecessor suite**

Run: `pixi run pytest tests/core/warm_reuse/ tests/integration/test_ephemeral_cross_session_warm_reuse.py tests/integration/test_non_ephemeral_consumes_index.py tests/core/test_non_ephemeral_does_not_write_index.py tests/test_ephemeral_index_write_gated.py -v`
Expected: all green.

- [ ] **Step 3: Pre-commit on the whole repo**

Run: `pixi run pre-commit run --all-files`
Expected: green (allow the pre-existing README issue).

- [ ] **Step 4: No commit** — verification-only task.

```json:metadata
{"files": [], "verifyCommand": "pixi run pytest tests/core tests/cli tests/integration -q && pixi run pre-commit run --all-files", "acceptanceCriteria": ["full offline suite green", "warm-reuse predecessor suite green", "pre-commit on all files green"], "modelTier": "mechanical"}
```

---

## Task 4: Live confirmation on RunPod (USER GATE)

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Re-run the existing `tests/live/test_runpod_ephemeral_warm_reuse_smoke.py` end-to-end on RunPod. Test must pass verbatim, including destroy + post-cleanup assertions reaching GREEN. Spend cap $0.10.

**Files:**
- Use unchanged: `tests/live/test_runpod_ephemeral_warm_reuse_smoke.py`
- Create: `tests/live/_warm_attach_teardown_fix_evidence_run1.log` (force-added)
- Create: `tests/live/_warm_attach_teardown_fix_evidence_run2.log` (force-added)
- Create: `tests/live/_warm_attach_teardown_fix_evidence.json`

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 before the live run (clean tree + env + pods=0).
- [ ] `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_runpod_ephemeral_warm_reuse_smoke.py -v -s --faulthandler-timeout=600` exits 0 with 1 passed, 0 failed.
- [ ] Run #1 cold-boot provision marker in run1.log: `running provisioner.provision for instance <pod_id_run1>`.
- [ ] Run #2 warm-attach marker in run2.log: `warm-reuse: attached to <pod_id_run1>` within 30 s of run #1's `generate completed`.
- [ ] Both subprocesses exit with `generated: uri=...` printed (the FIX evidence — pre-fix run #2 did not reach this line).
- [ ] Post-run `cat .kinoforge/_lifecycle/ephemeral-index.json` returns `{"rows": []}`.
- [ ] Post-run RunPod GraphQL `{ myself { pods { id desiredStatus } } }` returns `pods: []`.
- [ ] Total live spend ≤ $0.10 (per-attempt cents-per-hour × uptimeSeconds arithmetic recorded in the evidence JSON).
- [ ] Prompt for run #1 sourced verbatim from `/workspace/examples/configs/prompts/field-realistic.txt`.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_runpod_ephemeral_warm_reuse_smoke.py -v -s --faulthandler-timeout=600`

**Steps:**

- [ ] **Step 1: Pre-spend authorization check**

Per `~/.claude/CLAUDE.md` autonomous-mode memory (`feedback_autonomous_no_gates`) live smokes are pre-authorized up to $20 session budget. No additional user confirmation required at this step.

- [ ] **Step 2: Sweeper safety net + preflight**

```bash
pixi run kinoforge sweeper start &
sleep 2
pixi run preflight
```
Expected: `preflight: PASS — safe to spend`.

- [ ] **Step 3: Run the live smoke**

Run: `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_runpod_ephemeral_warm_reuse_smoke.py -v -s --faulthandler-timeout=600`
Expected exit: 0. Single test passes end-to-end including destroy + post-cleanup.

While running, poll RunPod every 60-90 s per `CLAUDE.md::Live smoke monitoring`:

```bash
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus costPerHr runtime { uptimeInSeconds gpus { gpuUtilPercent } container { cpuPercent } } } } }'})
print(r)
"
```

If GPU=0% for ≥3 consecutive probes after the generate phase should have started → kill, capture logs, fail loud per `CLAUDE.md`.

- [ ] **Step 4: Capture evidence**

```bash
cp /tmp/pytest-of-claudeuser/pytest-*/test_two_ephemeral_runs_share_*/run1.log \
   tests/live/_warm_attach_teardown_fix_evidence_run1.log
cp /tmp/pytest-of-claudeuser/pytest-*/test_two_ephemeral_runs_share_*/run2.log \
   tests/live/_warm_attach_teardown_fix_evidence_run2.log
```

Write `tests/live/_warm_attach_teardown_fix_evidence.json` mirroring the predecessor's evidence JSON schema (see `tests/live/_ephemeral_warm_reuse_smoke_evidence.json`) with: pod_id, GPU type, cost rate, approx spend, AC results.

- [ ] **Step 5: Sweeper teardown**

```bash
pkill -f "kinoforge sweeper" || true
pixi run kinoforge list  # confirm No running instances + No instances recorded
```

- [ ] **Step 6: Commit**

```bash
git add tests/live/_warm_attach_teardown_fix_evidence.json
git add -f tests/live/_warm_attach_teardown_fix_evidence_run1.log
git add -f tests/live/_warm_attach_teardown_fix_evidence_run2.log
git commit -m "$(cat <<'EOF'
test(live): warm-attach teardown fix GREEN end-to-end on RunPod

tests/live/test_runpod_ephemeral_warm_reuse_smoke.py passes verbatim
post-fix (Task 2). Run #1 cold-boot + run #2 warm-attach + destroy +
post-cleanup all reach GREEN. Evidence captured.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_warm_attach_teardown_fix_evidence.json", "tests/live/_warm_attach_teardown_fix_evidence_run1.log", "tests/live/_warm_attach_teardown_fix_evidence_run2.log"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_runpod_ephemeral_warm_reuse_smoke.py -v -s --faulthandler-timeout=600", "acceptanceCriteria": ["preflight exit 0", "live test exits 0 with 1 passed", "run #1 cold-boot provision marker", "run #2 warm-attach within 30s", "both subprocesses print generated: uri=...", "post-run index empty", "post-run RunPod pods empty", "spend <= $0.10", "prompt from field-realistic.txt"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["cold-boot", "run-1", "provision"], ["warm-attach", "run-2", "attached"], ["destroy", "post-cleanup", "empty"]]}
```

---

## Task 5: Close workstream

**Goal:** Update PROGRESS.md + .tasks.json so the resume protocol cleanly sees this workstream CLOSED.

**Files:**
- Modify: `PROGRESS.md` (replace the predecessor's Active block with this workstream's close + reinstate predecessor as a closed entry under it).
- Modify: `docs/superpowers/plans/2026-06-27-warm-attach-teardown-hang.md.tasks.json` (all tasks → completed).

**Acceptance Criteria:**
- [ ] PROGRESS.md "Active workstream" block names every commit + the evidence file path.
- [ ] `.tasks.json` has all 5 tasks `status: completed` and `lastUpdated` set to local time.
- [ ] No other workstream's text is moved or rewritten.

**Verify:** `git diff HEAD~1 PROGRESS.md docs/superpowers/plans/2026-06-27-warm-attach-teardown-hang.md.tasks.json` shows only the close edits.

**Steps:**

- [ ] **Step 1: Update tasks.json**

Use python:

```bash
pixi run python -c "
import json
from datetime import datetime
p = 'docs/superpowers/plans/2026-06-27-warm-attach-teardown-hang.md.tasks.json'
d = json.load(open(p))
for t in d['tasks']:
    t['status'] = 'completed'
d['lastUpdated'] = datetime.now().astimezone().isoformat(timespec='seconds')
json.dump(d, open(p,'w'), indent=2, ensure_ascii=False)
print('all tasks marked completed')
"
```

- [ ] **Step 2: Update PROGRESS.md**

Replace the current `## Active workstream` block (which references the 2026-06-27 ephemeral warm-reuse discovery workstream) with the new active block for this workstream, naming the spec path, the plan path, every commit hash from Tasks 1-4, and the evidence file. Push the predecessor's block down under a `---` divider as a CLOSED entry.

- [ ] **Step 3: Commit**

```bash
git add PROGRESS.md docs/superpowers/plans/2026-06-27-warm-attach-teardown-hang.md.tasks.json
git commit -m "$(cat <<'EOF'
docs(progress): warm-attach teardown hang workstream CLOSED (5 tasks all green)

Records the .tasks.json all-completed flip and updates the active-
workstream block to name every commit + the live evidence file.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["PROGRESS.md", "docs/superpowers/plans/2026-06-27-warm-attach-teardown-hang.md.tasks.json"], "verifyCommand": "git diff HEAD~1 PROGRESS.md docs/superpowers/plans/2026-06-27-warm-attach-teardown-hang.md.tasks.json", "acceptanceCriteria": ["PROGRESS.md active block names all commits + evidence", ".tasks.json all completed with current local-TZ timestamp", "no other workstream text disturbed"], "modelTier": "mechanical"}
```

---

## Dependencies

- Task 2 blocked by Task 1 (need RED faulthandler dump to localize fix).
- Task 3 blocked by Task 2 (need fix landed before regression sweep).
- Task 4 blocked by Tasks 2 and 3 (need fix + offline regression green before spending).
- Task 5 blocked by Task 4 (close workstream only after live GREEN evidence committed).
