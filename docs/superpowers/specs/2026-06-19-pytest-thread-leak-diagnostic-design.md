# Pytest thread-leak diagnostic (layered)

**Status:** brainstormed, partially implemented (see "Existing state")
**Author:** Dr. Twinklebrane (via Claude Code)
**Date:** 2026-06-19
**Scope:** diagnose only — fix is out of scope for this spec
**Supersedes:** `2026-06-17-pytest-post-session-hang-diagnostic-design.md`
(reused symptom analysis, extended to layered trigger + pure-function unit
tests)

## Existing state (discovered post-self-review, 2026-06-19)

`tests/conftest.py:329-390` already implements a `pytest_sessionfinish`
hook from the 2026-06-17 spec lineage. The hook:

- Enumerates `threading.enumerate()` at session end.
- Fast-paths via `non_daemon_extras = [t for t in threads if t is not main_thread and not t.daemon]`
  — this is **smarter than what was specified above** (which used
  `len(threads) == 1`). Daemon threads cannot block
  `threading._shutdown()`, so filtering them out reduces false-positive
  dumps. The plan adopts this filter; spec is amended.
- Inlines dump construction (no extractable helper).
- Writes stderr + `tests/_post_session_dump.txt` (already gitignored).
- Has NO `pytest_configure` hook and NO `faulthandler.dump_traceback_later`
  arming.

The gap-to-this-spec is therefore:

1. Extract the inline dump body into a pure `_build_dump(threads, exitstatus) -> str`
   helper in a new `tests/_thread_dump_helper.py` module, behaviour-preserving.
2. Add the three unit tests (per "Test plan") against the extracted helper.
3. Add `pytest_configure(config)` hook arming `faulthandler.dump_traceback_later(15, ...)`.
4. Add `faulthandler.cancel_dump_traceback_later()` call at the top of
   `pytest_sessionfinish`.
5. Add the end-to-end smoke (per "Test plan").

The fast-path-filter line in the "Components" section below should be
read as "preserve the existing `non_daemon_extras` filter" — do not
regress it to `len(threads) == 1` during refactor.

## Problem

Pytest exits cleanly with a summary line of the shape
`N failed, 2510 passed, 58 skipped, 6 xfailed in 76s` but the host process
tree (`bash -c 'pixi run -- pytest ...'` → `pixi` → `python3.13 pytest`)
does not return.

Observed on:

- **CI / GitHub Actions, ubuntu-latest** (run `27693732183`, commit
  `4190c8c`): pytest summary at 76 s, job cancelled at 6 h 0 m 16 s,
  runner cleanup reports orphan processes `pid 2389 (pixi)` and
  `pid 2402 (python)`.
- **CI / GitHub Actions, macos-latest** (same run): pytest summary at
  75 s, job exits cleanly with `exit code 1` at 2 m 37 s. **No hang
  on macOS.**
- **Local linux container** (claude session 2026-06-17, two `pixi run
  -- pytest` invocations): both pytest runs reported a clean summary
  then hung > 5 h until manual `kill -9`.
- **Recurring 2026-06-19**: ~50 zombie `pixi`/`pytest` pairs piled up
  during the cfg-validation Check Registry workstream from
  subagent-dispatched test runs. Reaped manually.

When SIGINT is delivered to the hung process, the dying interpreter
prints a `ValueError: I/O operation on closed file.` from
`kinoforge/cli/_main.py:247`, with the stack showing
`threading._shutdown()` in progress — strong signal that one or more
**non-daemon threads** are still running after the test session is
ostensibly complete.

Three production-code thread sites are the obvious suspects:

- `src/kinoforge/core/heartbeat_loop.py:189` (`daemon=True`)
- `src/kinoforge/core/sweeper.py:177` (`daemon=True`)
- `src/kinoforge/core/pool.py:211` — `ThreadPoolExecutor` workers,
  **non-daemon by default**. A `ConcurrentPool` constructed in a test
  but never `.close()`-d leaves its executor workers alive. The stdlib
  `concurrent.futures` `atexit` handler then joins them at interpreter
  exit, blocking the process indefinitely.

The Linux vs. macOS divergence is consistent with a thread waiting on
a platform-specific primitive (`epoll`, `inotify`, or a `select()`
against a handle that gets reaped on macOS).

## Non-goals

- **No fix** for the leak. The leak's root cause is whichever test (or
  production teardown path) is leaving a non-daemon thread alive; once
  this diagnostic identifies it, a separate spec patches the teardown.
- **No suite-wide timeout add** (e.g. `pytest-timeout`). That hides the
  leak rather than diagnosing it.
- **No production-code change.** Diagnostic lives in `tests/` only.
- **No process-zombie reaper.** The recurring `pixi`/`pytest` zombie
  pairs from subagent-dispatched test runs share the same surface
  symptom but a different root cause (Claude tool-call subprocess
  reaping) and need a separate tool. Filed as follow-up; not in this
  spec.
- **No fixture-level per-test thread audit.** Considered and rejected
  during the 2026-06-17 brainstorm because turning 2 510
  currently-passing tests into a noisy failure cascade until the leak
  is patched would make the test suite unusable in the interim.
- **No session-fail on leak.** Always-on, report-only — the diagnostic
  produces signal but does not change exit codes.

## Approach: layered (sessionfinish hook + faulthandler timer)

Two non-overlapping triggers, defense in depth:

1. **`pytest_sessionfinish` hook (Python).** Runs after the test
   summary is printed and before pytest's `main()` returns. Has access
   to `sys.stderr` and `threading.enumerate()`. Produces clean,
   labelled output on the common path.
2. **`faulthandler.dump_traceback_later(15, repeat=False)` (C-side
   timer).** Armed at `pytest_configure`. Cancelled at the top of
   `pytest_sessionfinish`. Fires only if the hook never reached —
   i.e. pytest is stuck in collection / fixture setup / a non-yielding
   test. C-side dump bypasses Python locks and stderr file objects,
   so it survives even an interpreter that has begun teardown.

This pairing catches both post-summary thread leaks AND mid-collection
hangs, neither of which the `sessionfinish`-only approach in
`2026-06-17-pytest-post-session-hang-diagnostic-design.md` would surface.

## Design

### Where the diagnostic lives

`tests/conftest.py`. Creating if absent; otherwise appending to the
session-scope section. Pure pytest infrastructure. Zero imports from
`src/kinoforge`. Preserves the core-import-ban invariant and the
`tests/` ↔ `src/` boundary.

### Module structure

```
tests/conftest.py
├─ pytest_configure(config)
│   ├─ faulthandler.enable()
│   └─ faulthandler.dump_traceback_later(15, repeat=False, file=sys.stderr)
├─ pytest_sessionfinish(session, exitstatus)
│   ├─ faulthandler.cancel_dump_traceback_later()
│   ├─ threads = threading.enumerate()
│   ├─ fast-path (len(threads) == 1):
│   │    stderr.write("=== POST-SESSION THREAD DUMP === clean (1 thread)\n")
│   └─ leak-path (len(threads) > 1):
│        payload = _build_dump(threads, exitstatus)
│        stderr.write(payload)
│        Path("tests/_post_session_dump.txt").write_text(payload)
└─ _build_dump(threads, exitstatus) -> str   # pure, unit-testable
```

### Components

**`pytest_configure(config)`** (~10 LOC)

- `faulthandler.enable()` — idempotent; safe if already on.
- `faulthandler.dump_traceback_later(timeout=15, repeat=False,
  file=sys.stderr, exit=False)`.
- 15 s chosen because a healthy `pytest_sessionfinish` returns in
  <1 s post-summary. 15 s gives slow CI runners headroom while still
  firing on any meaningful hang.

**`pytest_sessionfinish(session, exitstatus)`** (~25 LOC)

- `faulthandler.cancel_dump_traceback_later()` — idempotent per
  stdlib; returns silently if no timer armed.
- `threads = threading.enumerate()`.
- Fast path (single thread):
  `sys.stderr.write("=== POST-SESSION THREAD DUMP === clean (1 thread)\n")`,
  return.
- Leak path: `payload = _build_dump(threads, exitstatus)`;
  `sys.stderr.write(payload)`; `Path("tests/_post_session_dump.txt").write_text(payload)`
  (best-effort, swallow `OSError`).

**`_build_dump(threads, exitstatus) -> str`** (~25 LOC, pure)

- Header:
  `f"=== POST-SESSION THREAD DUMP === pid={os.getpid()} exitstatus={exitstatus} n_threads={len(threads)}\n"`
- For each thread:
  - `f"  thread name={t.name!r} ident={t.ident} daemon={t.daemon} alive={t.is_alive()}{main_marker}\n"`
  - Python stack: `frame = sys._current_frames().get(t.ident)`. If
    `None`: emit `"    <no Python frame — likely in C extension>\n"`.
    Else `traceback.format_stack(frame)`, each line prefixed `"    "`.
- Linux-only fd inventory: `try: fds = sorted(os.listdir("/proc/self/fd/"))
  ; append f"  open fds: {len(fds)} → {fds}\n"`. `except OSError: pass`
  (macOS short-circuit).
- Return joined string.

**`.gitignore`** append `tests/_post_session_dump.txt`.

### Data flow

```
pytest invocation
  │
  ▼
pytest_configure ──► faulthandler.enable()
                  ──► dump_traceback_later(15, file=stderr)
                          │  (C-side timer)
                          │
[ collection + tests run normally ]
                          │
   ┌─────────── HEALTHY PATH ────────────┐    ┌─────── HANG PATH ────────┐
   │  test summary printed               │    │ collection / fixture     │
   │       │                             │    │ stalls > 15 s           │
   │       ▼                             │    │       │                  │
   │  pytest_sessionfinish               │    │       ▼                  │
   │   cancel_dump_traceback_later()     │    │  faulthandler dumps ALL  │
   │   threading.enumerate() == [Main]   │    │  thread stacks to stderr │
   │   write "clean (1 thread)"          │    │  (may also reach         │
   │   return                            │    │   sessionfinish later)   │
   └─────────────────────────────────────┘    └──────────────────────────┘
                          │
   ┌─────────── LEAK PATH ───────────────┐
   │  test summary printed               │
   │   pytest_sessionfinish              │
   │    cancel_dump_traceback_later()    │
   │    threading.enumerate() = [Main,…] │
   │    _build_dump(...) → payload       │
   │    stderr.write(payload)            │
   │    tests/_post_session_dump.txt ←   │
   │        payload (truncate)           │
   │   return                            │
   │  threading._shutdown() blocks       │
   │  on the leaked non-daemon thread    │
   │  (dump already on disk + stderr)    │
   └─────────────────────────────────────┘
```

### Error handling

| Failure | Handling |
|---|---|
| `os.listdir("/proc/self/fd/")` raises `OSError` (macOS) | `except OSError: pass` — skip fd inventory, rest of dump still emits. |
| `sys._current_frames().get(ident)` returns `None` | Emit `"    <no Python frame — likely in C extension>\n"`, continue loop. |
| `Path("tests/_post_session_dump.txt").write_text(...)` raises (CI tmpdir cwd, perms) | `except OSError: pass` after `stderr.write(payload)`. Disk dump is best-effort; stderr is authoritative. |
| `cancel_dump_traceback_later()` raises | Never per stdlib. No guard. |
| `dump_traceback_later` fires mid-`_thread_shutdown()` | C-side write — bypasses Python locks. Safe by stdlib design. |
| stderr already closed at sessionfinish | Should not happen. Do NOT wrap stderr write in try/except — would mask the bug we're hunting. |
| `pytest -p no:conftest` | Diagnostic disabled. Acceptable; explicit opt-out. |
| pytest-xdist parallel workers | Each worker runs its own hook; last writer wins on the dump file. xdist not used by kinoforge today. Known limitation. |

### Gating

Always on. Two reasons:

- A diagnostic the operator must remember to enable is one that does
  not fire when it matters. The hang already evaded one CI cycle.
- Fast-path output is a single line on healthy runs; cost is
  effectively zero.

### Output destinations

1. **stderr.** CI logs include stderr verbatim. The dump appears
   immediately above the runner-cancellation line.
2. **`tests/_post_session_dump.txt`.** Local-run convenience. Gitignored.
   Truncated per session.
3. **C-side faulthandler dump** also writes to stderr (same FD as `sys.stderr` at `pytest_configure` time).

## Success criteria

A re-run of the ubuntu CI job (or a fresh local `pixi run -- pytest`)
on the HEAD that includes this diagnostic must:

1. Print the `=== POST-SESSION THREAD DUMP ===` banner before the
   runner cancels (or within 15 s of any hang).
2. Name at least one non-`MainThread` thread with `daemon=False`.
3. Surface a Python stack pointing into kinoforge production code
   (likely `core/pool.py`, `core/heartbeat_loop.py`, or
   `core/sweeper.py`).
4. The dump file `tests/_post_session_dump.txt` mirrors the stderr
   content on a local run.

Once those four hold, a follow-up spec
(`<date>-pytest-thread-leak-fix-design.md`) identifies the leaking
test by stack-trace correlation and patches the teardown.

## Test plan

### Pure-function tests (`tests/test_post_session_dump.py`, 3 tests)

Each test names a concrete bug it catches (per `test-design` skill):

1. `test_build_dump_includes_thread_metadata` — construct a real
   `threading.Thread(name='leaker', daemon=False)` whose target is
   `lambda: stop_event.wait()`, `.start()` it, pass `[t]` to
   `_build_dump([t], 0)`, then `stop_event.set()` + `t.join(1.0)` in
   the test teardown (no leaked thread). Assert output contains the
   substrings `name='leaker'`, `daemon=False`, `n_threads=1`, AND
   contains the literal callable filename (i.e. a Python stack frame
   was found and formatted). **Catches:** accidental swap of
   `daemon=True`↔`daemon=False` in formatter, accidental drop of
   `name=` or `n_threads=` fields, accidental skip of the stack-format
   block on the happy path.
2. `test_build_dump_no_frame_falls_back_to_extension_marker` — pass
   a `types.SimpleNamespace(name='ghost', ident=999_999_999,
   daemon=False, is_alive=lambda: True)` (with `ident` chosen above
   any plausible OS thread id so `sys._current_frames()` cannot
   contain it); assert the literal
   `"<no Python frame — likely in C extension>"` appears in the
   output. **Catches:** accidental `KeyError`/`AttributeError` on
   missing frame, accidental change of fallback string.
3. `test_build_dump_includes_fd_count_on_linux` — skip on
   `sys.platform == "darwin"`; assert output matches regex
   `r"open fds: \d+ →"`. **Catches:** accidental swallow of the
   fd inventory block, accidental wrong key (`fd` vs `fds`).

### End-to-end smoke (`tests/test_post_session_dump_e2e.py`, 1 test)

Spawn `pytest` as a subprocess on a temp test-module that constructs
`threading.Thread(target=lambda: time.sleep(60), daemon=False).start()`
and exits. Subprocess given a hard `timeout=30`. Assert stderr
contains `=== POST-SESSION THREAD DUMP ===` AND contains
`daemon=False`. **Catches:** hook registration regression,
`conftest.py` syntax error, faulthandler-timer cancellation bug,
fast-path / leak-path branch swap.

### Hook itself

Not separately unit-tested. Pytest's hook dispatch is the integration
boundary; the e2e covers it.

### Platform validation

- Linux container: `pixi run pytest tests/test_post_session_dump*.py`
  — confirm 4/4 pass.
- CI ubuntu-latest: same.
- CI macos-latest: confirm test #3 skips cleanly; #1 + #2 + e2e pass.

## Risk

- **Output noise on every run.** Mitigation: fast-path one-line
  confirmation when only `MainThread` is alive.
- **Confusing stderr output mixing with pytest's own summary.**
  Mitigation: banner prefix is recognisable; dump comes after summary.
- **A test that intentionally leaves a daemon thread alive.**
  Acceptable — `daemon=True` lines show in the output; operator
  decides they are expected. The block-on-`_thread_shutdown()`
  symptom only manifests with `daemon=False`.
- **C-side faulthandler dump on a slow CI runner (false trigger).**
  15 s threshold chosen against observed `<1 s` healthy
  `sessionfinish` post-summary timing. If CI proves it noisy, widen
  to 30 s in a follow-up — but do NOT widen pre-emptively without
  evidence.

## Open questions

- Does `pytest_unconfigure` (runs later than `sessionfinish`) need a
  second cancel call? Probably not — `cancel_dump_traceback_later()`
  is already idempotent. Revisit if `sessionfinish` itself ever
  becomes the hang site.
- Should the dump be appended (history) instead of truncating
  (single-session)? Truncating chosen for grep simplicity; append
  is a one-liner change later if needed.

## Out of scope, deferred to follow-up

- Fixing the thread leak (separate spec post-identification).
- Adding `pytest-timeout` as CI safety net.
- Auditing every `Thread(...)` call site in `src/kinoforge` for
  cleanup parity.
- Reaping `pixi`/`pytest` process zombies from
  subagent-dispatched test runs (different root cause, different
  surface).
