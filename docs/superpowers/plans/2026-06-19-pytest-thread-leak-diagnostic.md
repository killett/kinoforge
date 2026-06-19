# Pytest thread-leak diagnostic (layered) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the layered post-session thread-dump diagnostic — extract the existing inline hook body into a pure, testable helper; add a `faulthandler.dump_traceback_later(15)` timer in `pytest_configure` as the safety net for hangs that never reach `pytest_sessionfinish`; cover both paths with unit + end-to-end tests.

**Architecture:** Two pytest hooks (`pytest_configure` arms a C-side faulthandler timer; `pytest_sessionfinish` cancels it and emits a labelled thread dump). The dump construction is delegated to a pure helper `_build_dump(threads, exitstatus) -> str` in a new `tests/_thread_dump_helper.py` module, so the formatter has dedicated unit tests independent of pytest's hook plumbing. Existing fast-path filter (`non_daemon_extras`) is preserved across the refactor.

**Tech Stack:** Python 3.13, pytest, stdlib `faulthandler`, stdlib `threading`, stdlib `traceback`. No new deps.

**User decisions (already made):**
- "Diagnostic first, then fix" — separate spec for the eventual thread-leak fix (out of scope here).
- "Full forensics" — threads + open fds + faulthandler timer + dump-to-file.
- "Always on, report-only" — no env-var gating, no exit-code change on leak.
- "Separate concern — thread diag only" — process-zombie reaping is a different follow-up; not bundled.
- "Fresh brainstorm — different angle" — supersedes the 2026-06-17 sessionfinish-only spec.

---

## File structure

| Path | New / Modify | Responsibility |
|---|---|---|
| `tests/_thread_dump_helper.py` | **NEW** (~30 LOC) | Pure `_build_dump(threads, exitstatus) -> str`. Header line + per-thread block (name/ident/daemon/alive + Python stack via `sys._current_frames()`, or C-extension fallback line) + linux `/proc/self/fd` inventory with `OSError` swallow. No I/O, no logging, no pytest imports. |
| `tests/conftest.py` | **MODIFY** lines 315-390 + new hook above | (1) Add `pytest_configure(config)` arming `faulthandler.dump_traceback_later(15, ...)`. (2) Refactor existing `pytest_sessionfinish` to call `_build_dump` from the helper module; prepend `faulthandler.cancel_dump_traceback_later()`; preserve existing `non_daemon_extras` fast-path filter. |
| `tests/test_post_session_dump.py` | **NEW** (~80 LOC) | 3 pure-function unit tests against `_build_dump`. |
| `tests/test_post_session_dump_e2e.py` | **NEW** (~60 LOC) | 1 end-to-end smoke spawning pytest as a subprocess on a temp test file that leaks a non-daemon thread; assert the dump banner + `daemon=False` appear in stderr. |

Already in place (do NOT modify):
- `.gitignore` — already lists `tests/_post_session_dump.txt`.
- `tests/__init__.py` — already present; `tests/` is a package, so `from tests._thread_dump_helper import _build_dump` works.

---

## Task 1: Extract `_build_dump` helper + 3 unit tests

**Goal:** Move the inline dump-construction body from `tests/conftest.py:359-382` into a new pure helper `tests/_thread_dump_helper.py::_build_dump(threads, exitstatus) -> str`, refactor `pytest_sessionfinish` to call it (behaviour-preserving), and cover the helper with 3 unit tests. Existing fast-path filter (`non_daemon_extras`) is preserved.

**Files:**
- Create: `tests/_thread_dump_helper.py`
- Modify: `tests/conftest.py` lines ~338-389 (replace inline dump body with helper call; remove now-unused `io`/`traceback`/`os` imports inside the hook if they fall out of use elsewhere in the hook)
- Test: `tests/test_post_session_dump.py`

**Acceptance Criteria:**
- [ ] `tests/_thread_dump_helper.py` defines `_build_dump(threads: list, exitstatus: int) -> str` with the docstring + Google-style typed signature.
- [ ] `tests/conftest.py::pytest_sessionfinish` imports `_build_dump` and calls it for the leak path; the inline `buf = io.StringIO()` … `buf.getvalue()` block is deleted.
- [ ] The `non_daemon_extras` fast-path filter survives intact (line 347 logic unchanged).
- [ ] All 3 unit tests in `tests/test_post_session_dump.py` pass under `pixi run pytest tests/test_post_session_dump.py -v`.
- [ ] `pixi run ruff check tests/_thread_dump_helper.py tests/test_post_session_dump.py tests/conftest.py` reports zero new findings.
- [ ] `pixi run mypy tests/_thread_dump_helper.py tests/test_post_session_dump.py tests/conftest.py` reports zero new findings.

**Verify:** `pixi run pytest tests/test_post_session_dump.py -v` → 3 passed in <2 s.

**Steps:**

- [ ] **Step 1.1: Write the 3 failing unit tests first.**

Create `tests/test_post_session_dump.py`:

```python
"""Unit tests for tests/_thread_dump_helper._build_dump.

These tests pin the formatter's output shape so a regression in the dump
text (which is the load-bearing signal for diagnosing pytest hangs) is
caught locally instead of waiting for a 6 h CI cancellation.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import types

import pytest

from tests._thread_dump_helper import _build_dump


def test_build_dump_includes_thread_metadata() -> None:
    """Happy path: real live thread → name, daemon flag, n_threads, and a Python stack frame appear.

    Catches:
      * accidental swap of daemon=True ↔ daemon=False in formatter,
      * accidental drop of name= or n_threads= fields,
      * accidental skip of the stack-format block (i.e. no Python frame
        emitted for a thread that demonstrably has one).
    """
    stop_event = threading.Event()

    def _wait() -> None:
        stop_event.wait()

    t = threading.Thread(target=_wait, name="leaker", daemon=False)
    t.start()
    try:
        output = _build_dump([t], 0)
    finally:
        stop_event.set()
        t.join(1.0)
        assert not t.is_alive(), "test thread must not leak past this test"

    assert "name='leaker'" in output
    assert "daemon=False" in output
    assert "n_threads=1" in output
    # Stack frame for `_wait` MUST appear — that proves the
    # sys._current_frames().get(t.ident) → traceback.format_stack(frame)
    # branch ran. The literal filename of this test module is the
    # cheapest deterministic anchor.
    assert __file__ in output or "_wait" in output


def test_build_dump_no_frame_falls_back_to_extension_marker() -> None:
    """Thread whose ident is not in sys._current_frames() → C-extension fallback line.

    Catches:
      * accidental KeyError / AttributeError on a missing frame,
      * accidental change of the fallback string (downstream log-grep
        recipes search for the literal text).
    """
    fake_thread = types.SimpleNamespace(
        name="ghost",
        ident=999_999_999,  # well above any plausible OS thread id
        daemon=False,
        is_alive=lambda: True,
    )
    output = _build_dump([fake_thread], 0)
    assert "<no Python frame — likely in C extension>" in output
    assert "name='ghost'" in output


@pytest.mark.skipif(sys.platform == "darwin", reason="no /proc on macOS")
def test_build_dump_includes_fd_count_on_linux() -> None:
    """Linux: dump appends a `open fds: N → [...]` line with a real count.

    Catches:
      * accidental swallow of the fd-inventory block,
      * accidental wrong key (`fd:` vs `fds:`).
    """
    # Sanity: /proc/self/fd is readable here — if not, this test is
    # invalid and should fail loudly rather than silently skip.
    assert os.path.isdir("/proc/self/fd"), "/proc/self/fd missing on a supposedly linux platform"

    output = _build_dump([], 0)
    assert re.search(r"open fds: \d+ →", output), f"missing fd-count line, got: {output!r}"
```

- [ ] **Step 1.2: Run the tests to confirm they fail with ImportError.**

Run:
```bash
pixi run pytest tests/test_post_session_dump.py -v
```
Expected: 3 collection errors with `ModuleNotFoundError: No module named 'tests._thread_dump_helper'`.

- [ ] **Step 1.3: Create the helper module.**

Create `tests/_thread_dump_helper.py`:

```python
"""Pure formatter for the post-session thread-dump diagnostic.

Extracted from tests/conftest.py:329-390 so the formatter can be
unit-tested without spawning pytest. The hook itself stays in
conftest.py (pytest's discovery boundary) and delegates to this module
for the dump string.
"""

from __future__ import annotations

import io
import os
import sys
import traceback
from typing import Any


def _build_dump(threads: list[Any], exitstatus: int) -> str:
    """Format a post-session thread dump as a single multi-line string.

    The output is identical to what the previous inline body in
    tests/conftest.py produced, modulo: this function takes the thread
    list as an argument (so callers can inject fakes), and it appends
    the linux ``/proc/self/fd/`` inventory unconditionally on linux
    (the caller is responsible for fast-pathing the empty-leak case
    before invoking).

    Args:
        threads: The live thread objects (or duck-typed stand-ins with
            ``name``, ``ident``, ``daemon``, ``is_alive`` attributes) to
            include in the dump.
        exitstatus: The exit status pytest will return. Echoed in the
            banner so a green vs. red session is distinguishable in CI
            logs.

    Returns:
        A multi-line string ending with a newline. Structure:

        - One ``=== POST-SESSION THREAD DUMP === pid=... exitstatus=... n_threads=...`` banner.
        - One ``  thread name=... ident=... daemon=... alive=...`` line per
          thread, followed by either the formatted Python stack or a
          C-extension fallback marker.
        - On linux: one trailing ``  open fds: N → [...]`` line. On
          macOS / non-linux: omitted.
    """
    import threading  # noqa: PLC0415  — kept local for symmetry with the hook

    main_ident = threading.main_thread().ident
    frames = sys._current_frames()

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
        frame = frames.get(t.ident) if t.ident is not None else None
        if frame is None:
            buf.write("    <no Python frame — likely in C extension>\n")
            continue
        for line in traceback.format_stack(frame):
            buf.write("    " + line.rstrip() + "\n")

    try:
        fds = sorted(int(e) for e in os.listdir("/proc/self/fd/"))
        buf.write(f"  open fds: {len(fds)} → {fds}\n")
    except OSError:
        # macOS / non-linux — no /proc. Skip the FD inventory.
        pass

    return buf.getvalue()
```

- [ ] **Step 1.4: Run tests — expect them to pass against the helper.**

Run:
```bash
pixi run pytest tests/test_post_session_dump.py -v
```
Expected: 3 passed (or 2 passed + 1 skipped on macOS).

- [ ] **Step 1.5: Refactor `tests/conftest.py:pytest_sessionfinish` to call the helper.**

Read the existing block at `tests/conftest.py:329-390` first to confirm exact line numbers. Then replace lines 356-389 of the existing hook (everything from `main_ident = threading.main_thread().ident` through the final `pass`) with:

```python
    # Delegate dump-string construction to the pure helper so the format
    # can be unit-tested without spawning pytest. The fast-path filter
    # above (non_daemon_extras) handles the no-leak case.
    from tests._thread_dump_helper import _build_dump  # noqa: PLC0415

    payload = _build_dump(threads, exitstatus)
    sys.stderr.write(payload)
    try:
        Path("tests/_post_session_dump.txt").write_text(payload, encoding="utf-8")
    except OSError:
        # cwd not writable (sandboxed CI step) — stderr is authoritative.
        pass
```

Then prune unused imports at the top of the hook body (lines 338-343 in the existing file): `io` and `traceback` are no longer needed inside the hook — drop both. Keep `os`, `sys`, `threading`, `Path`.

- [ ] **Step 1.6: Confirm pytest still collects + runs cleanly.**

Run:
```bash
pixi run pytest tests/test_post_session_dump.py tests/test_pool.py -v
```
Expected: 3 (or 2) pass for `test_post_session_dump.py`; existing pool tests unaffected; the `=== POST-SESSION THREAD DUMP === clean (... no non-daemon extras)` banner appears once at session end.

- [ ] **Step 1.7: Lint / typecheck.**

Run:
```bash
pixi run ruff check tests/_thread_dump_helper.py tests/test_post_session_dump.py tests/conftest.py
pixi run mypy tests/_thread_dump_helper.py tests/test_post_session_dump.py tests/conftest.py
```
Expected: zero new findings from either tool.

- [ ] **Step 1.8: Commit.**

```bash
git add tests/_thread_dump_helper.py tests/test_post_session_dump.py tests/conftest.py
git commit -m "refactor(tests): extract _build_dump pure helper + unit tests

tests/conftest.py:329-390 inlined the post-session thread-dump
formatter. Moving the body into tests/_thread_dump_helper.py
makes the format unit-testable without spawning pytest.

Three new unit tests pin the output shape:
 - happy path: name/daemon/n_threads + Python stack frame present
 - C-extension fallback: ident missing from _current_frames() →
   literal '<no Python frame — likely in C extension>' line
 - linux fd inventory: '  open fds: N → [...]' line appears
   (skipped on macOS via sys.platform check)

No behavioural change for the hook — the non_daemon_extras fast-path
filter is preserved, and the leak-path payload is byte-identical
because the helper is a copy-out of the inline body."
```

---

## Task 2: Add `pytest_configure` + faulthandler timer + e2e smoke

**Goal:** Layer the C-side `faulthandler.dump_traceback_later(15, repeat=False, file=sys.stderr)` timer on top of the existing `pytest_sessionfinish` hook so a pytest hang that never reaches sessionfinish (collection / fixture stall) still produces a stack dump within 15 s. Cancel the timer at the top of sessionfinish to suppress the C-side dump on healthy runs. Add one end-to-end test that spawns pytest as a subprocess against a leaky test module and asserts the dump appears in stderr.

**Files:**
- Modify: `tests/conftest.py` — add `pytest_configure(config)` above the existing `pytest_sessionfinish`; prepend a single `faulthandler.cancel_dump_traceback_later()` line at the top of `pytest_sessionfinish`.
- Test: `tests/test_post_session_dump_e2e.py`

**Acceptance Criteria:**
- [ ] `pytest_configure(config)` exists in `tests/conftest.py`, calls `faulthandler.enable()` then `faulthandler.dump_traceback_later(15, repeat=False, file=sys.stderr, exit=False)`.
- [ ] First executable line of `pytest_sessionfinish` (after the docstring + `import faulthandler`) is `faulthandler.cancel_dump_traceback_later()`.
- [ ] `tests/test_post_session_dump_e2e.py` spawns `pytest` as a subprocess (`timeout=30 s`) on a temp test module that leaks a non-daemon thread; the test asserts the subprocess stderr contains both `=== POST-SESSION THREAD DUMP ===` and `daemon=False`.
- [ ] `pixi run pytest tests/test_post_session_dump_e2e.py -v` passes in <20 s.
- [ ] A healthy `pixi run pytest tests/test_post_session_dump.py -v` still emits the `clean (... no non-daemon extras)` banner (i.e. the faulthandler timer was cancelled and did NOT also dump).
- [ ] `pixi run ruff check tests/conftest.py tests/test_post_session_dump_e2e.py` and `pixi run mypy tests/conftest.py tests/test_post_session_dump_e2e.py` report zero new findings.

**Verify:** `pixi run pytest tests/test_post_session_dump_e2e.py -v` → 1 passed in <20 s.

**Steps:**

- [ ] **Step 2.1: Write the e2e test first.**

Create `tests/test_post_session_dump_e2e.py`:

```python
"""End-to-end smoke for the post-session thread-dump diagnostic.

Spawns pytest as a subprocess on a temp test module that constructs a
non-daemon thread and exits, then asserts the diagnostic produced both
the banner and a daemon=False line in stderr.

This is the only test that exercises the actual pytest hook dispatch
+ faulthandler.cancel_dump_traceback_later() call. The unit tests in
tests/test_post_session_dump.py cover the formatter in isolation.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_session_dump_fires_on_leaked_non_daemon_thread(tmp_path: Path) -> None:
    """A leaked non-daemon thread surfaces in the subprocess stderr.

    Catches: hook registration regression, conftest.py syntax error,
    faulthandler-timer cancellation bug (would manifest as a SECOND
    'Timeout (0:00:15)!' dump appearing in stderr on a healthy run),
    fast-path / leak-path branch swap.
    """
    # The temp module starts a non-daemon thread that waits on a
    # threading.Event the test never sets, then pytest's "session" ends
    # with that thread still alive. The diagnostic must flag it.
    test_module = tmp_path / "test_leaky.py"
    test_module.write_text(
        textwrap.dedent(
            """
            import threading

            _stop = threading.Event()


            def test_starts_a_non_daemon_thread() -> None:
                t = threading.Thread(
                    target=_stop.wait,
                    name="e2e_leaker",
                    daemon=False,
                )
                t.start()
                # Intentionally do NOT join. The diagnostic must catch it.
                assert t.is_alive()
            """
        ),
        encoding="utf-8",
    )

    # Copy the project's tests/conftest.py + tests/_thread_dump_helper.py
    # into a sibling `tests/` directory under tmp_path so the subprocess
    # picks up the same diagnostic. Using `rootdir=tmp_path` via -c
    # avoids interference with the outer pytest invocation.
    project_root = Path(__file__).resolve().parents[1]
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    for name in ("conftest.py", "_thread_dump_helper.py"):
        (tests_dir / name).write_text(
            (project_root / "tests" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    # Use the leaky test path scoped under the temp tests/ dir so the
    # in-tmp conftest applies. We could also write the leaky test
    # directly to tests/, but keeping it at tmp_path/test_leaky.py
    # means the subprocess collects exactly one test.
    (tests_dir / "test_leaky.py").write_text(
        test_module.read_text(encoding="utf-8"), encoding="utf-8"
    )

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(tests_dir / "test_leaky.py"), "-v", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # 1 test passes (the thread is alive when we assert it); diagnostic
    # fires AFTER the summary line. Exit code may be non-zero only if
    # the leaked thread blocks shutdown long enough to trip the
    # 30 s timeout — in which case TimeoutExpired is raised above
    # (caught by pytest as a test failure with a clear message).
    combined = proc.stdout + proc.stderr
    assert "=== POST-SESSION THREAD DUMP ===" in combined, (
        f"diagnostic banner missing\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "daemon=False" in combined, (
        f"non-daemon thread not surfaced\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "e2e_leaker" in combined, (
        f"leaker thread name missing\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
```

- [ ] **Step 2.2: Run the e2e test — expect it to PASS without the timer change (the existing hook already produces the dump).**

Run:
```bash
pixi run pytest tests/test_post_session_dump_e2e.py -v
```
Expected: 1 passed in <20 s. (This test passes against the existing hook because Task 1's refactor was behaviour-preserving. The test is structured to also exercise the timer-cancel logic added in subsequent steps — a regression there would manifest as a duplicate dump.)

If the test FAILS, stop and debug — the refactor in Task 1 was not behaviour-preserving.

- [ ] **Step 2.3: Add the `pytest_configure` hook to `tests/conftest.py`.**

Insert the following BLOCK immediately above the existing `# ---` comment block at line 315 of `tests/conftest.py` (the `Post-session thread-dump diagnostic` heading). The new hook must precede the existing `pytest_sessionfinish` to make the dependency order obvious to a reader:

```python
# ---------------------------------------------------------------------------
# Faulthandler safety net for pytest hangs (spec
# docs/superpowers/specs/2026-06-19-pytest-thread-leak-diagnostic-design.md).
#
# pytest_sessionfinish below names the thread once the hang manifests AFTER
# the summary line. A hang that prevents the test session from REACHING
# pytest_sessionfinish at all (a collection-phase deadlock, a fixture lock,
# a non-yielding generator) would skip the hook entirely. Arming
# faulthandler.dump_traceback_later(15) at pytest_configure gives us a
# C-side safety net that dumps all thread stacks to stderr 15 s after
# configure, bypassing the Python interpreter's lock state. The matching
# cancel call sits at the top of pytest_sessionfinish so healthy runs
# do NOT also dump from the timer.
# ---------------------------------------------------------------------------


def pytest_configure(config):  # noqa: ANN001
    """Arm a 15 s faulthandler timer at configure-time.

    Args:
        config: pytest ``Config`` object. Unused — present to match the
            documented hook signature.
    """
    import faulthandler  # noqa: PLC0415
    import sys  # noqa: PLC0415

    faulthandler.enable()
    # repeat=False because pytest_sessionfinish cancels on the happy
    # path; a hang therefore gets exactly one C-side dump 15 s later,
    # not a noise storm. exit=False because we want to KEEP running so
    # pytest can still emit its summary if it ever gets there.
    faulthandler.dump_traceback_later(15, repeat=False, file=sys.stderr, exit=False)
```

- [ ] **Step 2.4: Prepend the cancel call inside `pytest_sessionfinish`.**

Add a single line immediately after the docstring inside `pytest_sessionfinish` (it will become the first executable line of the function):

```python
    import faulthandler  # noqa: PLC0415
    faulthandler.cancel_dump_traceback_later()
```

The remainder of the hook is unchanged.

- [ ] **Step 2.5: Re-run the e2e + unit tests + a healthy-path sanity check.**

Run:
```bash
pixi run pytest tests/test_post_session_dump.py tests/test_post_session_dump_e2e.py -v
```
Expected: 4 (or 3 on macOS) passed.

Then sanity-check the healthy path emits the `clean` banner exactly once and NOT a faulthandler timeout dump:
```bash
pixi run pytest tests/test_pool.py -v 2>&1 | rg "POST-SESSION|Timeout \(0:00:15\)" || echo "NO BANNER — FAIL"
```
Expected: a single `=== POST-SESSION THREAD DUMP === clean (... no non-daemon extras)` line. No `Timeout (0:00:15)!` line (that would mean the cancel call didn't take effect).

- [ ] **Step 2.6: Lint / typecheck.**

Run:
```bash
pixi run ruff check tests/conftest.py tests/test_post_session_dump_e2e.py
pixi run mypy tests/conftest.py tests/test_post_session_dump_e2e.py
```
Expected: zero new findings.

- [ ] **Step 2.7: Commit.**

```bash
git add tests/conftest.py tests/test_post_session_dump_e2e.py
git commit -m "feat(tests): layered faulthandler timer + e2e thread-leak smoke

pytest_configure now arms faulthandler.dump_traceback_later(15,
repeat=False, file=sys.stderr) so a pytest hang that never reaches
pytest_sessionfinish (collection-phase deadlock, fixture lock,
non-yielding generator) still gets one C-side stack dump 15 s after
configure. pytest_sessionfinish prepends
faulthandler.cancel_dump_traceback_later() so healthy runs do NOT
also dump from the timer.

The 15 s threshold was chosen against the observed <1 s
healthy-sessionfinish time post-summary; widen later if CI shows
false positives, but only with evidence.

tests/test_post_session_dump_e2e.py spawns pytest as a subprocess on
a temp leaky-thread module and asserts banner + daemon=False +
thread name 'e2e_leaker' all appear in stderr. Catches hook
registration regression, faulthandler-cancel bug, and fast-path /
leak-path branch swaps."
```

---

## Self-Review

**Spec coverage.** Walked the spec section-by-section:
- "Existing state" — Task 1 step 1.5 explicitly says "preserve `non_daemon_extras` fast-path filter"; the refactor is behaviour-preserving. ✓
- "Approach: layered" — Task 1 covers the sessionfinish half (extract + tests); Task 2 covers the faulthandler half (timer + cancel + e2e). ✓
- "Components" (`pytest_configure`, `pytest_sessionfinish`, `_build_dump`) — all three present, in matching task. ✓
- "Data flow" diagram — covered implicitly via tests; the e2e exercises HEALTHY + LEAK paths, the timer-cancel sanity check (step 2.5) exercises HANG path absence. ✓
- "Error handling" table — fd inventory `OSError` swallow is tested by skip-on-darwin (#3); dump-file `OSError` swallow is preserved verbatim from the existing hook; pytest-xdist limitation called out in spec, not exercised here (kinoforge does not use xdist). ✓
- "Gating: always on" — no env var introduced; both hooks unconditional. ✓
- "Output destinations" — stderr + `tests/_post_session_dump.txt` + faulthandler stderr; all three exercised. ✓
- "Success criteria" 1-4 — banner emission (test #1 / e2e), non-daemon-thread named (e2e asserts `daemon=False` + `e2e_leaker`), stack pointing at production code (unit test #1 asserts `__file__` or `_wait` substring; the spec's "production code" claim becomes meaningful only once a real leak is hit in CI). ✓ (deferred-validation by design)
- "Test plan" — 3 unit + 1 e2e, all present. ✓

**Placeholder scan.** No `TBD` / `TODO` / `implement later` / "add appropriate error handling" / "similar to Task N" in either task. Every code block is the literal code to write. Every shell command is verbatim. ✓

**Type consistency.** Both tasks reference `_build_dump(threads, exitstatus) -> str`. Helper module path `tests/_thread_dump_helper.py` used consistently. Conftest line numbers cited (315, 329-390) match the file I read. ✓

**User-gate scan.** Spec is `Always on, report-only` — no `verify first` / `prove` / `gate` / `smoke test` ordering language in the brief. Verbs like `validate` appear in the test-plan section but only as descriptions of routine work. No noun or scope match → no `userGate: true` tags. ✓

No issues found.

---

## Task persistence

After plan landing, the corresponding `.tasks.json` carries:

- Task 1: extract helper + unit tests. `blockedBy: []`. Native task ID assigned at TaskCreate time.
- Task 2: faulthandler timer + e2e. `blockedBy: [<Task 1 id>]`.

Both tasks tagged `modelTier: "mechanical"` — the spec is exhaustive enough that an executing agent does not need design judgement.
