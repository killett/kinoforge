# Pytest post-session hang diagnostic

**Status:** brainstormed, not implemented
**Author:** Dr. Twinklebrane (via Claude Code, Phase 53 follow-up)
**Date:** 2026-06-17
**Scope:** diagnose only — fix is out of scope for this spec

## Problem

Pytest exits cleanly with a summary line of the shape
`N failed, 2510 passed, 58 skipped, 6 xfailed in 76s` but the host process tree
(`bash -c 'pixi run -- pytest ...'` → `pixi` → `python3.13 pytest`) does not
return. Observed on:

- **CI / GitHub Actions, ubuntu-latest** (run `27693732183`, commit `4190c8c`):
  pytest summary at 76 s, job cancelled at 6 h 0 m 16 s, runner cleanup
  reports orphan processes `pid 2389 (pixi)` and `pid 2402 (python)`.
- **CI / GitHub Actions, macos-latest** (same run): pytest summary at 75 s,
  job exits cleanly with `exit code 1` at 2 m 37 s. **No hang on macOS.**
- **Local linux container** (claude session 2026-06-17, two `pixi run -- pytest`
  invocations): both pytest runs reported a clean summary then hung > 5 h until
  manual `kill -9`.

When SIGINT is delivered to the hung process (manual `kill -INT` locally;
GitHub Actions runner cleanup), the dying interpreter prints a logging error
from `kinoforge/cli/_main.py:247`:

```
ValueError: I/O operation on closed file.
Call stack:
  File "/workspace/.pixi/envs/default/lib/python3.13/threading.py", line 1543, in _shutdown
    _thread_shutdown()
  File "/workspace/src/kinoforge/cli/_main.py", line 247, in _handler
    _log.warning(
Message: 'interrupt received; finishing in-flight work + draining pool. ...'
```

The trace shows `threading._shutdown()` running (so the interpreter has begun
teardown) and the graceful SIGINT handler from `_install_sigint_handler()`
firing during shutdown. The `_log.warning` call fails because `sys.stderr` has
already been closed by the shutdown sequence. Two separate symptoms:

1. **`_thread_shutdown()` is blocked** — that is what stalls Python's exit
   past the pytest summary line. Strong signal that one or more **non-daemon
   threads** are still running with the test session ostensibly complete.
2. **Logging-on-closed-stderr noise** — cosmetic, downstream of (1). Once (1)
   is fixed, the SIGINT handler does not get a chance to fire during
   shutdown.

The non-daemon-thread hypothesis is corroborated by three known thread sites
in kinoforge production code:

- `src/kinoforge/core/heartbeat_loop.py:1`
- `src/kinoforge/core/pool.py:1`
- `src/kinoforge/core/sweeper.py:1`

Any test that constructs one of these objects without an explicit teardown,
or whose teardown path raises, would leave a live thread behind. The Linux
vs. macOS divergence is the kind of pattern produced by a thread waiting on
a platform-specific primitive (e.g. an epoll loop, inotify watch, or a
`select()` against a file handle that gets reaped on macOS).

## Non-goals

- **No fix** for the leak in this spec. The leak's root cause is whichever
  test (or production teardown path) is leaving a non-daemon thread alive;
  once the diagnostic identifies it, a separate spec fixes it.
- **No suite-wide timeout add** (e.g. `pytest-timeout`). That hides the leak
  rather than diagnosing it.
- **No production-code change.** Diagnostic lives in `tests/`.
- **No fixture-level per-test thread audit.** Considered and rejected during
  brainstorming because turning 2 510 currently-passing tests into a
  noisy failure cascade until the leak is patched would make the test suite
  unusable in the interim.

## Design

### Where the diagnostic lives

`tests/conftest.py` (creating if absent; otherwise appending to the
session-scope section). Uses pytest's `pytest_sessionfinish(session, exitstatus)`
hook, which runs **after** the test summary is printed and **before** pytest's
`main()` returns. The hook executes in the main thread, has access to
`sys.stderr` (still open at that point), and is guaranteed to run on every
session — passing, failing, or interrupted.

Living in `tests/conftest.py` (not `src/`) preserves the
core-import-ban invariant and the `tests/` ↔ `src/` boundary. The hook is
test infrastructure, not production diagnostics.

### What the hook captures

```python
def pytest_sessionfinish(session, exitstatus):
    import io, os, sys, threading, traceback
    from pathlib import Path

    threads = threading.enumerate()
    if len(threads) <= 1:
        # Fast path: only MainThread alive → no leak. Single confirmation line.
        sys.stderr.write("=== POST-SESSION THREAD DUMP === clean (1 thread)\n")
        return

    main_ident = threading.main_thread().ident
    frames = sys._current_frames()

    # Build the dump in a StringIO so stderr + file get identical bytes.
    buf = io.StringIO()
    buf.write(
        f"=== POST-SESSION THREAD DUMP === pid={os.getpid()} "
        f"exitstatus={exitstatus} n_threads={len(threads)}\n"
    )
    for t in threads:
        marker = " (main)" if t.ident == main_ident else ""
        buf.write(
            f"  thread name={t.name!r} ident={t.ident} "
            f"daemon={t.daemon} alive={t.is_alive()}{marker}\n"
        )
        frame = frames.get(t.ident)
        if frame is None:
            buf.write("    <no Python frame — likely in C extension>\n")
            continue
        for line in traceback.format_stack(frame):
            buf.write("    " + line.rstrip() + "\n")

    # Linux-only file-descriptor inventory. /proc/self/fd/ does not exist on
    # macOS; an OSError there short-circuits the inventory cleanly.
    try:
        fds = sorted(os.listdir("/proc/self/fd/"))
        buf.write(f"  open fds: {len(fds)} → {fds}\n")
    except OSError:
        pass

    payload = buf.getvalue()
    sys.stderr.write(payload)
    # Mirror to a local file for grep-friendly review on hung local runs.
    # Gitignored. Truncated on every session.
    Path("tests/_post_session_dump.txt").write_text(payload)
```

Approximate LOC: 40-50 lines including imports + a short docstring.

### Where the output goes

1. **stderr.** CI logs include stderr verbatim; ubuntu's
   `gh api .../actions/jobs/<id>/logs` will surface the dump immediately
   above `##[error]The operation was canceled`.
2. **`tests/_post_session_dump.txt`.** Local-run convenience. Gitignored
   via a one-line append to `.gitignore`.

### Gating

Always on. Two reasons:

- A diagnostic that the operator must remember to enable is a diagnostic
  that does not fire when it matters. The hang already evaded one CI cycle.
- The fast-path output is a single line ("clean (1 thread)"); cost is
  effectively zero on healthy runs.

### Failure modes the dump must handle gracefully

- **`sys._current_frames()` returns `None` for a C-extension-resident
  thread.** Handled: explicit fallback line.
- **stderr already closed.** The hook runs *before* `threading._shutdown()`,
  so this should not occur. Defensive: a bare `try / except` around the
  write loop would mask the real problem; do not add it.
- **macOS missing `/proc`.** Handled: `OSError` short-circuit.

## Success criteria

A re-run of the ubuntu CI job (or a fresh local `pixi run -- pytest`) on the
HEAD that includes this diagnostic must:

1. Print the `=== POST-SESSION THREAD DUMP ===` banner before the runner
   cancels.
2. Name at least one non-`MainThread` thread with `daemon=False`.
3. Surface a Python stack pointing into kinoforge production code (likely
   `core/heartbeat_loop.py`, `core/pool.py`, or `core/sweeper.py`).
4. The dump file `tests/_post_session_dump.txt` must mirror the stderr
   content on a local run.

Once those four hold, a follow-up spec (`<date>-pytest-thread-leak-fix-design.md`)
can identify the leaking test by stack-trace correlation and patch the
teardown.

## Risk

- **Output noise on every run.** Mitigation: fast-path one-line confirmation
  when only `MainThread` is alive.
- **Confusing stderr output mixing with pytest's own summary.** Mitigation:
  banner prefix is recognisable and the dump comes *after* pytest's summary
  line, not before.
- **A test that intentionally leaves a daemon thread alive (legitimate)
  triggers the dump.** Acceptable — `daemon=True` lines will show in the
  output and the operator can decide they are expected. The block-on-
  `_thread_shutdown()` symptom only manifests with `daemon=False`.

## Test plan for the diagnostic itself

The conftest hook does not get a unit test (it IS test infrastructure).
Validation is end-to-end:

- **Local:** run `pixi run -- pytest`. Confirm dump fires. If currently
  hung, confirm dump names a thread.
- **CI:** push a branch with only the conftest change. Confirm the
  `27693732183`-shape dump appears in the ubuntu log.

## Open questions

- Does the dump need a hook for `pytest_unconfigure` (runs even later than
  `pytest_sessionfinish`)? Probably not — `sessionfinish` covers the hang
  window per the trace. Revisit if `sessionfinish` itself runs after the
  hang starts.
- Should the dump include `multiprocessing.active_children()` for stuck
  worker processes? Considered — only relevant if the hang turns out to be
  multiprocessing-pool-related. Add in a follow-up if (3) above points at
  `pool.py`.

## Out of scope, deferred to follow-up spec

- Fixing the thread leak.
- Adding `pytest-timeout` as a CI safety net.
- Auditing every `Thread(...)` call site in `src/kinoforge` for cleanup
  parity.
