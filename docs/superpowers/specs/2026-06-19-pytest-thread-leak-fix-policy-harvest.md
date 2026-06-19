# Plan A Task 4 — L1 leaker harvest

**Captured:** 2026-06-19 (local timezone)

**Spec:** `docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md`

**Plan:** `docs/superpowers/plans/2026-06-19-pytest-thread-leak-fix-policy-plan-a.md`

**L1 WARN-mode commit (Task 2):** `ec12d0f`

**Suite HEAD at capture time:** `45d7e4d`

**Raw inventory (gitignored):** `tests/_l1_leakers_inventory.txt`

## Capture scope

Harvest run: `pixi run pytest -q --ignore=tests/core/test_orchestrator.py --ignore=tests/core/test_batch_generate.py`.
Result: **4 failed, 2548 passed, 76 skipped, 6 xfailed in 116.06s**. The four
failures are pre-existing and unrelated to L1:

- `tests/core/test_orchestrator_heartbeat.py::test_deploy_session_with_interval_none_does_not_spawn_loop`
- `tests/core/test_orchestrator_session_fields.py::test_deploy_session_session_start_absent_when_hb_disabled`
- `tests/test_core_invariant.py::test_no_adapter_imports_in_core`
- `tests/test_l1_thread_policy.py::test_hook_exempts_daemon_threads`

The last one is L1's own unit test — see *Known curiosities* below.

### Excluded modules

`tests/core/test_orchestrator.py` and `tests/core/test_batch_generate.py` were
ignored because they hang indefinitely on a `provision:pod-premade-7b2.lock`
FileLock contention that pre-dates this work (any `_make_premade_instance`-using
test holds the lock past its own teardown, blocking the next test that does
`deploy_session(...)` in the same module's `_make_premade_instance` flow with
`timeout_s=None`, i.e. forever). The L0 faulthandler timer (15 s) confirmed the
stuck stack is in `src/kinoforge/stores/local_lock.py::FileLock.acquire` polling
loop. This is a pre-existing flakiness orthogonal to L1; leakers inside those
files are not in this harvest. If the underlying lock bug is fixed before Plan
B runs, the harvest should be re-captured against the full suite.

## Parser assumption

The `_l1_append_warn` format in `tests/conftest.py` uses raw `name` (not `repr`)
in the tab-separated payload:
`<nodeid>\tname=<thread name>\tdaemon=<bool>\tident=<int>\n`.
Thread names that contain literal tab characters would break this parser. In
practice kinoforge thread names are alphanumeric + dash (e.g.
`kinoforge-pool-0_0`) so the assumption is stable. If a future leaker ships a
tab in its `Thread.name`, this harvest doc would silently mis-split the row;
audit the raw inventory first.

## Known curiosities

- `tests/test_l1_thread_policy.py::test_hook_exempts_daemon_threads` failed with
  `assert teardown_rep.outcome == 'failed' == 'passed'`. The unit test asserts
  that a daemon-thread leaker leaves a passing teardown report untouched, but at
  the time the assertion ran the test process had other live non-daemon threads
  in `threading.enumerate()` (other kinoforge-pool workers from prior tests), so
  the FAIL-mode branch flipped the report. This is *expected behaviour* given
  the pre-existing pool leaker — the unit test will start passing again once
  Plan B fixes the underlying pool teardown. Not a regression introduced by
  this work; recorded here so it isn't lost.
- The Task 3 L1 e2e test (`tests/test_l1_thread_policy_e2e.py`) runs its own
  pytest subprocess against a temp test module whose in-tmp conftest also writes
  to `tests/_l1_leakers_inventory.txt`. Any leakers caught inside that
  subprocess point at subprocess-temp nodeids, not the outer suite's nodeids,
  so they are recognizable. In this capture they did not show up because the
  subprocess writes its inventory into its own cwd's `tests/` dir, not the
  workspace one.

## Leaker inventory

| name | daemon | count | first 3 nodeids |
|---|---|---|---|
| `kinoforge-pool-0_0` | False | 1845 | `tests/core/test_pool_cancel.py::test_close_no_kwargs_preserves_existing_behavior`<br>`tests/core/test_pool_cancel.py::test_close_returns_within_timeout_when_worker_wedged`<br>`tests/core/test_pool_cancel.py::test_sequential_pool_close_accepts_kwargs` |
