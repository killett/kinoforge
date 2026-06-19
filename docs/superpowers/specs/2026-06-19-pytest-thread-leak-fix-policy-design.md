# Pytest thread-leak FIX policy — Design

**Date:** 2026-06-19
**Status:** Draft, awaiting user review.
**Companion:** `docs/superpowers/specs/2026-06-19-pytest-thread-leak-diagnostic-design.md` (the diagnostic shipped 2026-06-19; this spec is its prevention counterpart).

## Goal

Prevent the class of pytest hangs that the layered thread-dump diagnostic
exists to *detect*. Where the diagnostic catches a leaked non-daemon thread
*after* it has wedged a CI run, this spec catches the leak at PR time (per
test, named test, named thread) AND provides a sanctioned escape hatch for
the rare legitimate non-daemon background thread.

## Non-goals

- A `ruff` custom rule (static analysis cannot see SDK-spawned threads and
  would force a parallel maintenance burden — runtime-only enforcement is
  deliberate).
- Cross-process / `pytest-xdist` thread leak detection (kinoforge does not
  use xdist today; `_KNOWN_PYTEST_THREADS` includes `"execnetMain"` as a
  forward-compat hedge but no xdist semantics are exercised).
- Re-litigating the *diagnostic* design (L0 faulthandler timer + L2
  sessionfinish dump are SHIPPED and unchanged by this spec).

## Architecture

Three-layer defense, in order of trigger:

| Layer | Trigger | Catches | Status |
|---|---|---|---|
| L0 | `faulthandler.dump_traceback_later(15)` armed at `pytest_configure`. C-side dump @ 15 s. | Hangs that never reach `pytest_sessionfinish` (collection deadlock, fixture lock, infinitely-blocking test). | **SHIPPED 2026-06-19** (commit `a9929a0`). |
| L1 | `pytest_runtest_makereport(hookwrapper=True, trylast=True)`. Fires per test, **`teardown` phase only** (after fixture finalizers ran, so `managed_thread`-registered threads have already been joined). | Test exits but leaves non-daemon non-main threads alive → fail the test by attribution to its `nodeid`. | **NEW (this spec)**. |
| L2 | `pytest_sessionfinish`. Existing `_build_dump(...)` + dump file. | Fixture-/module-scope thread that outlived per-test enforcement. Belt-and-suspenders safety net. | **SHIPPED, unchanged.** |

A new project-scoped fixture `managed_thread` (defined in `tests/conftest.py`)
gives contributors a single sanctioned API for legitimate non-daemon test
threads. L1 exempts threads registered through this fixture.

## L1 policy contract

A test PASSES (under L1) iff every thread alive at the start of the L1
hook satisfies at least one of:

1. `t is threading.main_thread()`, OR
2. `t.daemon is True`, OR
3. `t.name in _KNOWN_PYTEST_THREADS` (`{"MainThread", "execnetMain"}`), OR
4. `t` was registered through `managed_thread.spawn(...)` or
   `managed_thread.register(...)` and the fixture's teardown joined it
   cleanly within the join-timeout (default 2.0 s).

Anything else is a **leaker**. Behavior on detected leakers depends on
`_L1_MODE`:

- `"warn"` — append one line per (test, leaker) to
  `tests/_l1_leakers_inventory.txt`. Leave the test outcome unchanged.
  Used during the Phase 2 harvest, then deleted at Phase 5.
- `"fail"` — flip the test report's `outcome` from `passed` to `failed`
  and set `longrepr` to a one-pager naming the test, leaker count, each
  leaker's `name`/`ident`/`daemon`/`alive`, and the fix recipe (use
  `managed_thread.spawn(...)` or `daemon=True`). Final policy.

L1 is silent for a given test iff EITHER its `call` report OR its
`teardown` report is already `failed` — either the test body asserted,
OR the `managed_thread` fixture teardown raised because a registered
thread did not join within its timeout. A genuine failure surfaces
first; the leak still appears in the L2 sessionfinish dump.

The hook is registered for ALL three phases (`setup`, `call`,
`teardown`) but is a no-op except on `teardown`. During `call`, the
hook stashes the call-phase report into `item.stash[_L1_CALL_REPORT_KEY]`
so the teardown pass can cross-reference it. This cross-phase
visibility is the only stateful behavior in the hook.

**Phase ordering (load-bearing):** L1 enforcement fires at
`when="teardown"`, not `when="call"`. pytest runs
`pytest_runtest_makereport(when="call")` BEFORE function-scoped fixture
finalizers, so a `call`-phase L1 would see registered threads still
alive and falsely flag them. By the time
`pytest_runtest_makereport(when="teardown")` fires, the
`managed_thread` fixture's `_teardown` has joined every registered
thread (or already failed loudly via `pytest.fail`). L1 only sees
unregistered leakers.

## `managed_thread` fixture

Function-scoped pytest fixture. Yields a registrar with two surfaces:

```python
class _ManagedThreadRegistrar:
    def spawn(
        self,
        target: Callable[..., Any],
        *,
        name: str,  # REQUIRED — anonymous test threads are unfixable when they leak.
        daemon: bool = False,
        args: tuple[Any, ...] = (),
        kwargs: Mapping[str, Any] | None = None,
    ) -> threading.Thread: ...

    def register(self, thread: threading.Thread) -> threading.Thread: ...

    def _teardown(self, join_timeout: float = 2.0) -> list[threading.Thread]: ...
```

- `.spawn(target, name=..., ...)` — constructs, starts, registers in one
  call. `daemon=False` default (opting into the managed path means
  opting into non-daemon by default). `name` is keyword-only and
  required so leak diagnostics always have a useful identifier.
- `.register(thread)` — accepts a pre-started `threading.Thread`,
  returns it unchanged for fluent chaining
  (`t = managed_thread.register(threading.Thread(...))`). Covers the
  case where an SDK or library hands you a thread you did not
  construct.
- `_teardown(join_timeout=2.0)` — joins every registered thread in
  registration order; returns the list of threads still alive past
  the timeout. The fixture wrapper calls this in the `finally` of
  its `yield`; if any thread did not join, `pytest.fail(...)` fires
  with a terse message naming the stuck thread(s) (`pytrace=False`).

The fixture is in `tests/conftest.py` (project-scoped), so any test in
the suite can `def test_foo(managed_thread): ...`.

## Rollout (big-bang)

The L1 hook ships gated on a single module-level constant:

```python
_L1_MODE: Literal["warn", "fail"] = "warn"
```

| Phase | Task | Deliverable |
|---|---|---|
| 1 | Land L1 hook in WARN mode + `managed_thread` fixture + unit + e2e tests. | `tests/conftest.py` + `tests/test_managed_thread_fixture.py` + `tests/test_l1_thread_policy.py` + `tests/test_l1_thread_policy_e2e.py`. All green. WARN mode does not fail any existing test. |
| 2 | Run full suite locally (`pixi run pytest -q`) with WARN mode active. Commit `docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md` listing every (nodeid, name, daemon, count) tuple from `tests/_l1_leakers_inventory.txt`, sorted+unique. | Frozen inventory document. |
| 3 | One sub-task per unique leaker `name` in the harvest. Each sub-task: identify originating code, apply minimum fix (`daemon=True` if the thread is genuinely background, `managed_thread.spawn` if it's a test-owned thread, `managed_thread.register` if it comes from a library), rerun the originating test, verify the entry has dropped out of the WARN inventory. | N atomic commits, one per leaker family. N is unknown until Phase 2 completes — the impl plan will derive N from the harvest, not pre-bake it. |
| 4 | Final harvest re-run. Confirm `tests/_l1_leakers_inventory.txt` is empty. | Confirmation commit citing the empty inventory. |
| 5 | Flip `_L1_MODE: Literal["warn", "fail"] = "fail"`. Delete the WARN-only file-append code path. Remove `tests/_l1_leakers_inventory.txt` from `.gitignore`. Delete the file. | One-line policy flip + dead-code delete. Mechanical, no judgment calls. |

Two protections against rollout dragging on:
- WARN-mode side-effect is idempotent + bounded: appends to the inventory
  even when the test fails, so a flaky test in Phase 1 does not lose
  harvest data.
- Phase 5 is purely mechanical — once Phase 4 confirms empty inventory,
  Phase 5 introduces zero new judgment, ensuring big-bang cutover stays
  clean.

## Data flow

```
                            ┌─────────────────────────────────────────┐
test body runs ─────────────│  fixture finalizers (LIFO)              │
                            │   - managed_thread teardown:            │
                            │     join registered threads w/ timeout  │
                            │     pytest.fail if any stuck            │
                            └─────────────────────────────────────────┘
                                              │
                                              ▼
                            ┌─────────────────────────────────────────┐
                            │  pytest_runtest_makereport (teardown)   │  ← L1
                            │   if teardown-report outcome == passed: │
                            │     enumerate threads;                  │
                            │     leakers = non-daemon ∧ non-main     │
                            │                ∧ name ∉ KNOWN_PYTEST    │
                            │     if leakers:                         │
                            │       WARN: append to inventory file    │
                            │       FAIL: flip outcome, set longrepr  │
                            └─────────────────────────────────────────┘
                                              │
                                              ▼
                                       (next test)
                                              │
                                              ▼
                            ┌─────────────────────────────────────────┐
                            │  pytest_sessionfinish                   │  ← L2
                            │   cancel L0 timer                       │
                            │   _build_dump(...) + write file         │
                            └─────────────────────────────────────────┘
```

L0 timer (armed at `pytest_configure`, cancelled at `pytest_sessionfinish`)
runs orthogonally and is omitted from this diagram for clarity — it only
fires when the session never reaches `sessionfinish`, in which case L1
and L2 also do not run.

## Error handling

| Failure mode | Behaviour |
|---|---|
| `managed_thread.spawn` raises before starting the thread | Exception propagates; fixture teardown sees an empty registry; no teardown failure. The test fails for the original reason. |
| `managed_thread`-registered thread does not join within 2.0 s | `pytest.fail` with `pytrace=False` in fixture teardown. Distinct error from L1 (which only catches unregistered leakers). |
| L1 hook itself raises (e.g. `_KNOWN_PYTEST_THREADS` lookup error) | Hook is a `hookwrapper`; raising inside the wrapper crashes pytest. Mitigate with a `try/except` around the enumeration that, on any unexpected exception, emits a single `sys.stderr.write(...)` line and lets the test outcome pass through untouched. Catches programmer error in the hook without falsely failing user tests. |
| WARN-mode file write fails (read-only fs) | `OSError` swallowed; one-time `sys.stderr.write("L1 inventory write failed: ...")`. Harvest still readable from stderr if needed. |
| Test forgets to use `managed_thread` and the SDK spawns a non-daemon thread | Caught by L1 (the whole point). Fix recipe in the L1 longrepr points the contributor at the fixture. |

## Test plan

### Unit tests — `tests/test_managed_thread_fixture.py` (~120 LOC, 4 tests)

1. `test_spawn_constructs_starts_and_joins_at_teardown` — a real test
   using the fixture spawns a thread; assert it ran, fixture teardown
   joins it cleanly, no `pytest.fail`. Pinned: `_threads` list length
   == 1 after spawn; `t.is_alive()` False after teardown.
2. `test_register_accepts_external_thread` — caller constructs
   `threading.Thread(...)`, starts it, calls `.register(t)`; teardown
   joins it; no `pytest.fail`. Pinned: `_threads` list length == 1
   after register.
3. `test_register_returns_thread_for_fluent_chaining` — assert
   `managed_thread.register(t) is t`. Pinned: identity contract.
4. `test_teardown_fails_when_thread_does_not_join_within_timeout` —
   register a thread waiting on an unset Event; call `_teardown(0.05)`
   directly (not the fixture wrapper); assert `still_alive` list
   contains the thread. Cleanup explicitly sets the event + joins so
   the test does not itself leak.

### Unit tests — `tests/test_l1_thread_policy.py` (~200 LOC, 8 tests)

1. `test_warn_mode_appends_to_inventory_file` — patch `_L1_MODE =
   "warn"`; build a fake `Item` + `CallInfo(when="teardown",
   outcome=passed)`; spawn a non-daemon leaker thread (joined in
   `finally`); invoke the hook wrapper directly; assert inventory
   file has one line with the test nodeid + leaker name +
   `daemon=False`.
2. `test_fail_mode_flips_outcome_to_failed_with_leaker_in_longrepr` —
   patch `_L1_MODE = "fail"`; same fake call; assert
   `rep.outcome == "failed"` and `rep.longrepr` contains the leaker
   name + the policy-fix hint string.
3. `test_hook_silent_on_already_failed_test` — outcome=failed; spawn a
   leaker; invoke hook; assert no inventory append AND outcome
   unchanged. Catches "let's also pile L1 on top of asserts" drift.
4. `test_hook_skips_setup_phase_entirely` — invoke with `when="setup"`;
   assert hook is a no-op (no stash write, no inventory append, no
   outcome flip) even with a live leaker present.
5. `test_call_phase_stashes_report_for_cross_phase_lookup` — invoke
   with `when="call", outcome=failed`; assert the item's stash now
   carries the call-phase report at `_L1_CALL_REPORT_KEY` and the
   call-phase outcome was not modified.
6. `test_teardown_silent_when_call_already_failed` — pre-populate the
   item stash with a `failed` call report; invoke with
   `when="teardown", outcome=passed`; spawn a leaker; assert no
   inventory append AND teardown outcome unchanged. Catches the
   cross-phase silence contract.
7. `test_hook_exempts_daemon_threads` — spawn `daemon=True` thread;
   outcome=passed; invoke; assert no append, no outcome flip.
8. `test_hook_exempts_known_pytest_thread_names` — register a fake
   thread named `"execnetMain"`; assert it does not trigger the hook.
   Catches `_KNOWN_PYTEST_THREADS` set drift.

Every unit test that spawns a thread joins it in a `finally` block so
the unit tests themselves do not trigger L1 on the OUTER pytest run.

### E2e — `tests/test_l1_thread_policy_e2e.py` (~80 LOC, 1 test)

Spawn pytest as a subprocess (re-using the `PYTHONPATH` and
`Popen+TimeoutExpired` pattern from
`tests/test_post_session_dump_e2e.py`, factored into a tiny
`_run_subprocess_pytest(tmp_path, test_body)` helper to avoid
copy-paste). Temp test module has two tests:

- `test_unmanaged_leaker_fails` — spawns a non-daemon thread, leaves
  it alive.
- `test_managed_leaker_passes` — uses `managed_thread.spawn(target,
  name="managed_e2e_thread")` for the same thread; teardown joins it.

Assert: subprocess exit code is non-zero; combined stdout+stderr
contains `ERROR` next to `test_unmanaged_leaker_fails` (pytest renders
a failed-teardown-phase report as ERROR, not FAILED — the test body
itself passed, the cleanup failed because of the leak) and
`L1 thread-leak policy`, AND `PASSED` next to
`test_managed_leaker_passes`.

Test invariants pinned by the suite:
- L1 only fires on `call`, not setup/teardown.
- L1 stays quiet when the test already failed.
- WARN-mode file format (tab-separated columns) is pinned for the
  Phase 2 sort+uniq grep.
- `managed_thread` round-trip works through actual pytest
  fixture-finalization order.

## File structure

| Path | New / Modify | Responsibility |
|---|---|---|
| `tests/conftest.py` | **MODIFY** | Add `_KNOWN_PYTEST_THREADS`, `_L1_MODE`, `_ManagedThreadRegistrar`, `managed_thread` fixture, `pytest_runtest_makereport` hookwrapper. Existing `pytest_configure` (L0) and `pytest_sessionfinish` (L2) blocks unchanged. |
| `tests/test_managed_thread_fixture.py` | **NEW** (~120 LOC) | 4 fixture unit tests. |
| `tests/test_l1_thread_policy.py` | **NEW** (~150 LOC) | 6 L1 hook unit tests (direct invocation, no subprocess). |
| `tests/test_l1_thread_policy_e2e.py` | **NEW** (~80 LOC) | 1 subprocess e2e covering managed + unmanaged paths. |
| `.gitignore` | **MODIFY** | Add `tests/_l1_leakers_inventory.txt` for Phase 1-4; removed at Phase 5. |
| `docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md` | **NEW (Phase 2)** | Frozen leaker inventory, produced by Phase 2 task. Not authored upfront. |

No production-code (`src/kinoforge/...`) changes anticipated — the
brainstorm survey showed all three `threading.Thread(...)` call sites
in `src/` already pass `daemon=True`. If Phase 2 harvest reveals a
production thread, Phase 3 will include a `src/` edit for that
specific case.

## Success criteria

1. After Phase 5, every test in the suite passes L1 enforcement: zero
   non-daemon, non-main, non-managed threads alive at the end of any
   test's `call` phase.
2. After Phase 5, a contributor who adds a new test that leaks a
   non-daemon thread sees the test fail with the policy's longrepr
   pointing them at `managed_thread.spawn(...)` — no CI hang, no
   timeout, no need to consult a human.
3. The L2 `pytest_sessionfinish` dump remains operational as a final
   safety net for fixture-scope and module-scope leaks that escape
   L1.
4. The L0 `faulthandler.dump_traceback_later(15)` timer remains
   operational for hangs that prevent the session from reaching
   either L1 or L2.

## Open questions (resolved during brainstorm; recorded for the record)

- **Foundation: defensive vs targeted fix?** Resolved: defensive
  (policy + fixture). Targeted fix becomes a small follow-up patch
  if a specific leaker surfaces later.
- **Enforcement layer: lint vs runtime vs both?** Resolved: runtime
  only. Lint blind to SDK-spawned threads.
- **Policy: strict vs lenient (escape hatch)?** Resolved: lenient
  via `managed_thread` fixture. Downgradable to strict later (delete
  the fixture) if it sees no use.
- **Fixture API: spawn-only vs register-only vs both?** Resolved:
  both. Two genuinely distinct construction patterns
  (test-owned vs SDK-handed).
- **Rollout: big-bang vs allowlist vs opt-in marker?** Resolved:
  big-bang with a WARN-mode harvest phase. No permanent debt list.
