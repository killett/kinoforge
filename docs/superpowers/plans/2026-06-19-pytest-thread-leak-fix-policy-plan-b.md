# Pytest thread-leak FIX policy — Plan B (pool fix + WARN→FAIL flip)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out Phases 3-5 of the thread-leak FIX policy by (1) fixing the sole leaker surfaced by Plan A's harvest — the `kinoforge-pool-0_0` ThreadPoolExecutor workers in `src/kinoforge/core/pool.py:211-214` that default to non-daemon — and (2) flipping `_L1_MODE` from `"warn"` to `"fail"` while deleting the WARN-only code path, so the policy is now enforced.

**Architecture:** Two minimal changes. Task 1: make `ConcurrentPool` ThreadPoolExecutor workers `daemon=True` at construction via an `initializer` callback that sets `threading.current_thread().daemon = True`. This is the smallest possible fix — production semantics for graceful shutdown are unchanged (atexit still calls `executor.shutdown()`); the only diff is that on ungraceful exit, workers no longer block process termination. Task 2 is a one-line policy flip + dead-code delete.

**Tech Stack:** Python 3.13, pytest, stdlib `concurrent.futures`, stdlib `threading`. No new deps.

**User decisions (already made — from Plan A brainstorm + harvest):**
- "Defensive hardening / policy + lint" — runtime policy + fixture chosen over speculative fix.
- "Runtime: pytest fails any test that leaks" — runtime enforcement.
- "Lenient: daemon=True OR registered with managed_thread" — sanctioned escape hatch.
- "Big-bang: identify + fix all existing leakers, then enable enforcement" — Plan A delivered the harvest; this plan delivers the cutover.
- Harvest (committed `1d83d1d`): exactly ONE unique leaker (`kinoforge-pool-0_0`, non-daemon, 1845 distinct test nodeids). Plan B's scope is "fix that one thread, then flip the mode."

**Harvest doc reference:** `docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md` (committed in `1d83d1d`).

---

## File structure

| Path | New / Modify | Responsibility |
|---|---|---|
| `src/kinoforge/core/pool.py` | **MODIFY** lines 211-214 | Add `initializer=_mark_thread_daemon` to the `ThreadPoolExecutor(...)` constructor call so worker threads are `daemon=True` at the moment they're started, before the test pool ever queues work. Define `_mark_thread_daemon` as a tiny module-private helper at the top of the same file. |
| `tests/core/test_pool_workers_daemon.py` | **NEW** (~50 LOC) | Two unit tests pinning the contract: (1) construct a `ConcurrentPool`, register a backend, submit a job, assert every alive thread whose name starts with `kinoforge-pool-` is `daemon=True`; (2) verify the `initializer` runs by spawning a no-op job and checking that `threading.current_thread().daemon` was `True` inside the worker. |
| `tests/conftest.py` | **MODIFY** | Flip `_L1_MODE: Literal["warn", "fail"] = "warn"` to `"fail"`. Delete the `_l1_append_warn(...)` private helper (dead code under FAIL mode). Delete the WARN branch in `pytest_runtest_makereport` (`if _L1_MODE == "warn": _l1_append_warn(...); return`). The `_l1_collect_leakers` + `_l1_build_longrepr` helpers + the FAIL branch stay. |
| `.gitignore` | **MODIFY** | Remove the `tests/_l1_leakers_inventory.txt` line (no longer written by anything). |
| `tests/_l1_leakers_inventory.txt` | **DELETE** | gitignored generated artifact; remove from working tree. |
| `tests/test_l1_thread_policy.py` | **MODIFY** | Delete the two tests that exercised WARN mode (`test_warn_mode_teardown_appends_leaker_to_inventory`, `test_teardown_silent_when_teardown_report_already_failed` — the latter only used WARN mode as its `_L1_MODE` setting; keep its FAIL-mode sibling). Keep the other six tests. Adjust the test file's leading comment to say "8 tests" → "6 tests". |

---

## Task 1: Make ConcurrentPool ThreadPoolExecutor workers daemon

**Goal:** Add an `initializer=_mark_thread_daemon` callback to the `ThreadPoolExecutor` construction in `src/kinoforge/core/pool.py:211-214` so worker threads are `daemon=True` from birth. Cover with 2 unit tests pinning the contract.

**Files:**
- Modify: `src/kinoforge/core/pool.py` — add `_mark_thread_daemon()` module-private helper at top, pass `initializer=_mark_thread_daemon` into the `ThreadPoolExecutor(...)` call at line ~211.
- Create: `tests/core/test_pool_workers_daemon.py`

**Acceptance Criteria:**
- [ ] `src/kinoforge/core/pool.py` defines `_mark_thread_daemon() -> None` that calls `threading.current_thread().daemon = True` (with a docstring explaining why).
- [ ] The `ThreadPoolExecutor(...)` call at line ~211 passes `initializer=_mark_thread_daemon` AND `max_workers` + `thread_name_prefix` unchanged.
- [ ] `tests/core/test_pool_workers_daemon.py` defines 2 tests; both pass under `pixi run pytest tests/core/test_pool_workers_daemon.py -v`.
- [ ] The full L1-instrumented thread-stack regression suite still passes (`pixi run pytest tests/test_post_session_dump.py tests/test_post_session_dump_e2e.py tests/test_conftest_post_session_dump.py tests/test_managed_thread_fixture.py tests/test_l1_thread_policy.py tests/test_l1_thread_policy_e2e.py -v` → 22 passed).
- [ ] `tests/core/test_pool_cancel.py` still passes (`pixi run pytest tests/core/test_pool_cancel.py -v` → all green). This file exercises pool semantics under shutdown — must not regress.
- [ ] `pixi run ruff check src/kinoforge/core/pool.py tests/core/test_pool_workers_daemon.py` zero new findings.
- [ ] `pixi run mypy src/kinoforge/core/pool.py tests/core/test_pool_workers_daemon.py` zero new findings.

**Verify:** `pixi run pytest tests/core/test_pool_workers_daemon.py -v` → 2 passed in <2 s.

**Steps:**

- [ ] **Step 1.1: Write the 2 failing tests first.**

Create `tests/core/test_pool_workers_daemon.py`:

```python
"""Unit tests for ConcurrentPool worker thread daemon-flag contract.

The L1 thread-leak policy (Plan A spec, Plan B Task 2 enforcement)
requires every pool worker thread to be `daemon=True`. Without this,
any test that constructs a ConcurrentPool and submits a job leaks a
non-daemon worker named `kinoforge-pool-N_M` past test teardown —
which Plan A's harvest captured across 1845 distinct test nodeids.

These tests pin the fix at the construction site.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.interfaces import (
    Artifact,
    GenerationBackend,
    GenerationJob,
    ModelProfile,
    Segment,
)
from kinoforge.core.pool import ConcurrentPool


@dataclass
class _NoopBackend(GenerationBackend):
    """Minimal GenerationBackend that returns immediately with a fake Artifact."""

    name: str = "noop"

    def submit(
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> str:
        del job, cancel_token
        return "fake-handle"

    def result(
        self,
        handle: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Artifact:
        del handle, cancel_token
        return Artifact(
            id="noop-artifact",
            url_or_path="memory://noop",
            content_type="application/octet-stream",
            segments=(Segment(start=0.0, end=1.0),),
            metadata={},
            cost_usd=0.0,
        )

    def profile(self) -> ModelProfile:
        return ModelProfile(
            engine_kind="noop",
            model="noop",
            mode="t2v",
            max_duration_s=1.0,
            supports_t2v=True,
            supports_i2v=False,
            supports_flf2v=False,
            supports_keyframe=False,
        )


def test_pool_workers_are_daemon_after_submit() -> None:
    """Every alive thread named `kinoforge-pool-*` must be daemon=True.

    Catches: dropping the `initializer=_mark_thread_daemon` arg from the
    ThreadPoolExecutor construction in pool.py, or accidentally setting
    daemon=False inside the initializer.
    """
    backend = _NoopBackend()
    pool = ConcurrentPool()
    pool.add_backend(backend, max_in_flight=2)

    job = GenerationJob(
        engine_kind="noop", model="noop", mode="t2v",
        prompt="ignored", duration_s=1.0, params={},
    )
    fut = pool.submit(job)
    artifact = fut.result(timeout=5.0)
    assert artifact.id == "noop-artifact"

    # Pool workers are still alive at this point — submit returned a Future
    # whose work happened on a worker. Inspect every thread named with
    # the pool's prefix.
    pool_workers = [
        t for t in threading.enumerate()
        if t.name.startswith("kinoforge-pool-") and t.is_alive()
    ]
    assert pool_workers, "no pool workers found after submit; pool may have shut down prematurely"
    non_daemon = [t for t in pool_workers if not t.daemon]
    assert not non_daemon, (
        f"non-daemon pool workers found: "
        f"{[(t.name, t.ident) for t in non_daemon]}"
    )

    pool.close(timeout=2.0)


def test_initializer_runs_inside_worker_setting_daemon() -> None:
    """The initializer sets daemon=True FROM INSIDE the worker thread.

    Catches: setting daemon on the wrong thread (e.g. the constructor's
    caller thread) or forgetting to mutate `threading.current_thread()`.
    """
    backend = _NoopBackend()
    pool = ConcurrentPool()
    pool.add_backend(backend, max_in_flight=1)

    observed_daemon: list[bool] = []
    observed_event = threading.Event()

    def _probe_target() -> Artifact:
        observed_daemon.append(threading.current_thread().daemon)
        observed_event.set()
        return backend.result("fake-handle", cancel_token=None)

    # Submit a probe through the pool's executor directly to capture
    # the daemon flag of the worker thread that actually runs it.
    fut = pool._slots[0].executor.submit(_probe_target)
    fut.result(timeout=5.0)
    assert observed_event.is_set(), "probe target was never invoked"
    assert observed_daemon == [True], (
        f"worker thread saw daemon={observed_daemon!r}; expected [True]"
    )

    pool.close(timeout=2.0)
```

- [ ] **Step 1.2: Run tests — expect failure (workers still daemon=False).**

```bash
pixi run pytest tests/core/test_pool_workers_daemon.py -v
```
Expected: both tests FAIL. Test 1 should fail with "non-daemon pool workers found"; test 2 should fail with "observed_daemon == [False]".

If they unexpectedly PASS, the tests are wrong (they're not actually probing what they claim). Stop and debug.

- [ ] **Step 1.3: Add `_mark_thread_daemon` helper + initializer.**

Open `/workspace/src/kinoforge/core/pool.py`. Near the top of the file (after the existing imports, before the first class), add:

```python
def _mark_thread_daemon() -> None:
    """ThreadPoolExecutor initializer that flips the worker thread to daemon.

    Plan A's L1 thread-leak harvest (commit 1d83d1d) showed the
    ConcurrentPool's ThreadPoolExecutor workers leak non-daemon across
    1845 distinct test nodeids — concurrent.futures defaults workers to
    daemon=False on Python 3.13, which means any test that submits a job
    and does not also call ``pool.close()`` in teardown leaks the worker
    past process exit (blocking pytest's interpreter shutdown). Setting
    daemon=True here ensures the worker dies with the process on
    ungraceful exit; graceful shutdown is unchanged because
    ``executor.shutdown(wait=True)`` still joins.
    """
    import threading  # noqa: PLC0415  — kept local; this is a hot path

    threading.current_thread().daemon = True
```

Then in the `ConcurrentPool.add_backend` method, modify the `ThreadPoolExecutor(...)` call at lines 211-214 from:

```python
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_in_flight,
            thread_name_prefix=f"kinoforge-pool-{len(self._slots)}",
        )
```

to:

```python
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_in_flight,
            thread_name_prefix=f"kinoforge-pool-{len(self._slots)}",
            initializer=_mark_thread_daemon,
        )
```

That's the only change to `pool.py`. Do NOT modify the watchdog Thread at lines 424-429 (already daemon=True). Do NOT modify any other executor in the project (downloader.py / batch.py — those are out of scope for the harvest leaker).

- [ ] **Step 1.4: Run tests — expect both to PASS.**

```bash
pixi run pytest tests/core/test_pool_workers_daemon.py -v
```
Expected: 2 passed in <2 s.

- [ ] **Step 1.5: Regression run — full thread-stack suite + pool_cancel.**

```bash
pixi run pytest tests/test_post_session_dump.py tests/test_post_session_dump_e2e.py tests/test_conftest_post_session_dump.py tests/test_managed_thread_fixture.py tests/test_l1_thread_policy.py tests/test_l1_thread_policy_e2e.py tests/core/test_pool_workers_daemon.py tests/core/test_pool_cancel.py -v 2>&1 | tail -5
```
Expected: 22 thread-stack passes + 2 new + all `test_pool_cancel.py` passes (count varies; whatever existed before should still pass).

If `test_l1_thread_policy.py::test_hook_exempts_daemon_threads` now passes (it was a known-failure in the harvest), that's a positive signal: the pool leaker that was tripping it is gone.

- [ ] **Step 1.6: Lint / typecheck.**

```bash
pixi run ruff check src/kinoforge/core/pool.py tests/core/test_pool_workers_daemon.py
pixi run mypy src/kinoforge/core/pool.py tests/core/test_pool_workers_daemon.py
```
Expected: zero new findings.

- [ ] **Step 1.7: Commit.**

```bash
git add src/kinoforge/core/pool.py tests/core/test_pool_workers_daemon.py
git commit -m "fix(pool): mark ConcurrentPool ThreadPoolExecutor workers daemon (Plan B Task 1)

Plan A's L1 thread-leak harvest (commit 1d83d1d) showed
kinoforge-pool-0_0 — a non-daemon ThreadPoolExecutor worker
from ConcurrentPool.add_backend — leaking across 1845 distinct
test nodeids. concurrent.futures defaults workers to daemon=False
on Python 3.13, so any test that submits a job without also
calling pool.close() in teardown leaks the worker past the test
boundary, blocking pytest's interpreter shutdown.

Fix: pass initializer=_mark_thread_daemon to the
ThreadPoolExecutor constructor so workers flip
threading.current_thread().daemon = True at startup. Production
semantics for graceful shutdown are unchanged: executor.shutdown()
still joins; on ungraceful exit, workers now die with the process.

tests/core/test_pool_workers_daemon.py pins the contract:
 - every kinoforge-pool-* thread alive after submit is daemon=True
 - the initializer runs inside the worker, not the caller

Unblocks Plan B Task 2 (WARN->FAIL mode flip)."
```

---

## Task 2: Flip L1 to FAIL mode + delete WARN dead code

**Goal:** With the pool leaker fixed (Task 1), flip `_L1_MODE` from `"warn"` to `"fail"`, delete the WARN-only code path (the `_l1_append_warn` helper + its branch in the hookwrapper), remove the inventory line from `.gitignore`, delete the inventory file, and prune the two WARN-only tests from `tests/test_l1_thread_policy.py`. Final state: L1 is fully enforcing; any future test that leaks a non-daemon non-main non-managed thread fails its own teardown with the policy longrepr.

**Files:**
- Modify: `tests/conftest.py` — flip `_L1_MODE` value to `"fail"`; delete `_l1_append_warn`; delete the WARN branch in `pytest_runtest_makereport`.
- Modify: `.gitignore` — remove `tests/_l1_leakers_inventory.txt` line + its companion comment if any.
- Delete: `tests/_l1_leakers_inventory.txt` from working tree.
- Modify: `tests/test_l1_thread_policy.py` — delete the WARN-mode tests; rename the leading comment + helper imports if they reference WARN; keep 6 tests.

**Acceptance Criteria:**
- [ ] `tests/conftest.py` has `_L1_MODE: Literal["warn", "fail"] = "fail"` (default value flipped). `_KNOWN_PYTEST_THREADS` and `_L1_CALL_REPORT_KEY` unchanged.
- [ ] `_l1_append_warn` function and its module-level `Path` / `sys.stderr.write` glue are deleted from `tests/conftest.py`.
- [ ] `pytest_runtest_makereport` teardown branch only has the FAIL path (`teardown_rep.outcome = "failed"; teardown_rep.longrepr = _l1_build_longrepr(...)`); no `if _L1_MODE == "warn":` branch remains.
- [ ] `.gitignore` no longer contains `tests/_l1_leakers_inventory.txt`.
- [ ] `tests/_l1_leakers_inventory.txt` no longer exists on disk.
- [ ] `tests/test_l1_thread_policy.py` has 6 tests (was 8) — the two WARN-mode tests deleted. The file's preamble docstring count is updated.
- [ ] All 6 tests in `tests/test_l1_thread_policy.py` pass.
- [ ] The full thread-stack regression suite passes: `pixi run pytest tests/test_post_session_dump.py tests/test_post_session_dump_e2e.py tests/test_conftest_post_session_dump.py tests/test_managed_thread_fixture.py tests/test_l1_thread_policy.py tests/test_l1_thread_policy_e2e.py tests/core/test_pool_workers_daemon.py -v` → all green.
- [ ] A scoped run of the full suite (matching the harvest's scope — same `--ignore` for the FileLock-stuck modules) passes the L1 policy: zero teardown-phase ERROR results from `L1 thread-leak policy` in the output.
- [ ] `pixi run ruff check tests/conftest.py tests/test_l1_thread_policy.py` zero new findings.
- [ ] `pixi run mypy tests/conftest.py tests/test_l1_thread_policy.py` zero new findings.

**Verify:** `pixi run pytest tests/test_l1_thread_policy.py -v` → 6 passed in <2 s.

**Steps:**

- [ ] **Step 2.1: Edit `tests/conftest.py`.**

Open `/workspace/tests/conftest.py`. Apply the following three edits:

**Edit A** — flip the mode default. Find:
```python
_L1_MODE: Literal["warn", "fail"] = "warn"
```
Replace with:
```python
_L1_MODE: Literal["warn", "fail"] = "fail"
```

**Edit B** — delete the `_l1_append_warn` helper. Find the function definition (it should be a `def _l1_append_warn(nodeid: str, leakers: list[threading.Thread]) -> None:` block starting around line 454 of the current file) and delete the entire function including its docstring. Approximately 17 lines.

**Edit C** — delete the WARN branch from the hookwrapper. Inside `pytest_runtest_makereport`, find:
```python
    if _L1_MODE == "warn":
        _l1_append_warn(item.nodeid, leakers)
        return

    # _L1_MODE == "fail"
    teardown_rep.outcome = "failed"
    teardown_rep.longrepr = _l1_build_longrepr(item.nodeid, leakers)
```
Replace with:
```python
    teardown_rep.outcome = "failed"
    teardown_rep.longrepr = _l1_build_longrepr(item.nodeid, leakers)
```

Also remove the `from typing import Literal` import if `_L1_MODE` is the only user (verify with `rg 'Literal' tests/conftest.py` after the edit; keep the import if other code uses it).

- [ ] **Step 2.2: Remove the gitignore line + delete the inventory file.**

Open `/workspace/.gitignore`. Find the line:
```
tests/_l1_leakers_inventory.txt
```
Delete that line. If there's a comment immediately above it referring to L1 / leaker inventory (likely added by Task 2 of Plan A), delete that too — leave no orphan comment.

Then delete the inventory file:
```bash
rm -f tests/_l1_leakers_inventory.txt
```

- [ ] **Step 2.3: Prune WARN-mode tests from `tests/test_l1_thread_policy.py`.**

Open `/workspace/tests/test_l1_thread_policy.py`. Delete these two test functions in full:

1. `test_warn_mode_teardown_appends_leaker_to_inventory` — the test patches `_L1_MODE` to `"warn"`; obsolete now that WARN is gone.
2. `test_teardown_silent_when_teardown_report_already_failed` — read it; if it patches `_L1_MODE` to `"warn"`, delete it. If it patches to `"fail"`, KEEP it (it asserts FAIL-mode silence on already-failed teardown, which is still a valid policy contract).

After deletion, update the file's leading module docstring to read "6 tests" instead of "8 tests" if it currently says "8".

The remaining tests (do NOT delete these):
- `test_call_phase_stashes_report_and_leaves_outcome_unchanged`
- `test_setup_phase_is_complete_noop`
- `test_fail_mode_teardown_flips_outcome_and_writes_longrepr`
- `test_teardown_silent_when_call_already_failed`
- `test_hook_exempts_daemon_threads`
- `test_hook_exempts_known_pytest_thread_names`

Plus whichever silent-on-failed-teardown test you kept above.

- [ ] **Step 2.4: Run the L1 unit tests — expect green.**

```bash
pixi run pytest tests/test_l1_thread_policy.py -v
```
Expected: 6 passed in <2 s. (Or 7 if you kept the FAIL-mode silent-on-teardown-failed test.)

- [ ] **Step 2.5: Full thread-stack + pool regression run.**

```bash
pixi run pytest tests/test_post_session_dump.py tests/test_post_session_dump_e2e.py tests/test_conftest_post_session_dump.py tests/test_managed_thread_fixture.py tests/test_l1_thread_policy.py tests/test_l1_thread_policy_e2e.py tests/core/test_pool_workers_daemon.py tests/core/test_pool_cancel.py -v 2>&1 | tail -5
```
Expected: all green; line count is the previous 22 + 2 new pool tests + the pool_cancel count, minus the 2 WARN tests we deleted.

- [ ] **Step 2.6: Scoped harvest-equivalent re-run — confirm zero L1 failures.**

```bash
pixi run pytest -q --ignore=tests/core/test_orchestrator.py --ignore=tests/core/test_batch_generate.py 2>&1 | tail -10
```
Expected: the suite finishes; the summary line shows roughly `2550 passed, 76 skipped, 6 xfailed` (numbers will drift slightly because the WARN tests were deleted and the pool daemon test added). CRITICALLY: zero `ERROR` lines mentioning `L1 thread-leak policy`. The 3 pre-existing core/orchestrator regressions noted in the harvest doc are still expected to fail — they're unrelated to L1.

To audit: `pixi run pytest -q --ignore=tests/core/test_orchestrator.py --ignore=tests/core/test_batch_generate.py 2>&1 | rg 'L1 thread-leak policy' | head -5` — expected empty.

- [ ] **Step 2.7: Lint / typecheck.**

```bash
pixi run ruff check tests/conftest.py tests/test_l1_thread_policy.py
pixi run mypy tests/conftest.py tests/test_l1_thread_policy.py
```
Expected: zero new findings.

- [ ] **Step 2.8: Commit.**

```bash
git add tests/conftest.py tests/test_l1_thread_policy.py .gitignore
git rm -f tests/_l1_leakers_inventory.txt 2>/dev/null || true
git commit -m "feat(tests): flip L1 thread-leak policy to FAIL mode (Plan B Task 2)

With the kinoforge-pool-0_0 leaker fixed in Plan B Task 1, the
L1 policy is now safe to enforce. This commit:
 - flips _L1_MODE default from \"warn\" to \"fail\" in tests/conftest.py;
 - deletes the _l1_append_warn helper (dead under FAIL mode);
 - removes the WARN branch from pytest_runtest_makereport so the
   hookwrapper only knows how to fail a teardown report;
 - removes tests/_l1_leakers_inventory.txt from .gitignore + the
   file itself;
 - prunes the two WARN-mode tests from tests/test_l1_thread_policy.py
   (the file is now 6 tests covering call-phase stash, setup no-op,
   FAIL-mode outcome flip, cross-phase silence-on-already-failed,
   daemon exemption, known-pytest-thread exemption).

Final L1 contract: a test PASSES iff every thread alive at the start
of its teardown-phase makereport is daemon=True, the main thread, in
{MainThread, execnetMain}, or was registered via managed_thread. Any
other live thread flips the teardown report to failed (renders as
ERROR in pytest output) with a longrepr pointing the fixer at
managed_thread.spawn.

Closes Plan B. Closes the thread-leak FIX policy work (spec
2026-06-19-pytest-thread-leak-fix-policy-design.md).

Companion: docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md"
```

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md` rollout phases 3, 4, 5):
- Phase 3 ("Per-leaker fix tasks — one task per distinct `name` value in the harvest"): harvest had exactly 1 name → 1 fix task. Task 1 covers it. ✓
- Phase 4 ("Final harvest re-run; expect empty inventory"): degenerate under FAIL mode because the inventory file no longer exists. Step 2.6 verifies the equivalent: zero `L1 thread-leak policy` ERROR lines on a scoped suite run. ✓
- Phase 5 ("Flip `_L1_MODE` + delete WARN-only code path + remove gitignore line + delete file"): Task 2 covers all four steps verbatim. ✓
- Spec architecture invariants (L0 + L2 unchanged, L1 contract preserved with FAIL-only branch): Task 2 explicitly does NOT touch L0 / L2 / `_l1_collect_leakers` / `_l1_build_longrepr`. ✓

**Placeholder scan.** No "TBD", "TODO", "implement later", "similar to Task N". One ambiguity: Step 2.3 says "if the test patches `_L1_MODE` to `'warn'`, delete it. If it patches to `'fail'`, KEEP it." That's an honest if-then on a specific observable, not a vague placeholder — the executing agent reads the test and decides based on the patched value. Acceptable. ✓

**Type consistency.**
- `_mark_thread_daemon` defined in Task 1, referenced as `initializer=_mark_thread_daemon` in the same task. ✓
- `_L1_MODE` value change is the only contract drift in Task 2; the type annotation `Literal["warn", "fail"]` stays the same (the user might want WARN back temporarily during a future incident). ✓
- `_l1_build_longrepr(item.nodeid, leakers)` reference in the post-edit hookwrapper matches the existing helper signature shipped in Plan A Task 2. ✓
- The 6 remaining test names listed in Step 2.3 match the test names landed in `tests/test_l1_thread_policy.py` per Plan A Task 2 commit `ec12d0f`. ✓

**User-gate scan.** Brief has zero gate-language matches. The harvest doc handed off the leaker; user did not request "first prove on one then all" or "verify each fix" ordering. The user-memory `Run autonomously — no user-gates` standing instruction further suppresses tagging. No tasks tagged `userGate: true`. ✓

No issues found.

---

## Task persistence

After plan landing, the corresponding `.tasks.json` carries:

- Task 1: pool worker daemon fix. `blockedBy: []`.
- Task 2: L1 WARN→FAIL flip + dead-code delete. `blockedBy: [<Task 1 id>]`.

Both tagged `modelTier: "mechanical"` — the plan code is verbatim and the contract is fully pinned by tests.

---

## Out of scope (for both this plan AND the spec — these belong in separate workstreams)

- The 9 CI failures pre-dating Plan A's push (heartbeat/supplied-instance regression in `tests/core/test_orchestrator.py` + `test_orchestrator_heartbeat.py` + `test_orchestrator_session_fields.py` + the `test_core_invariant.py::test_no_adapter_imports_in_core` violation at `src/kinoforge/core/config.py:1134-1135`). They are unrelated to L1.
- The `FileLock` hang in `src/kinoforge/stores/local_lock.py` that forced the harvest to `--ignore` two test modules. Plan A's harvest doc flags this as a pre-existing flakiness; Plan B inherits the same `--ignore` for its verification re-run. Fixing it is a separate workstream.
- Re-running the harvest against the full suite (no `--ignore`) once the FileLock bug is fixed, to verify no second leaker was hidden inside those modules. The spec's rollout was big-bang on the visible inventory; if a hidden leaker surfaces later, treat it as a Plan B-style follow-up (one fix task + a verification re-run, no WARN reintroduction required because FAIL mode will surface it immediately).
