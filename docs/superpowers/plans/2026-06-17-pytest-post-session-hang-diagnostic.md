# Pytest post-session hang diagnostic — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append a `pytest_sessionfinish` hook to `tests/conftest.py` that dumps `threading.enumerate()` + `sys._current_frames()` to stderr and to a gitignored local file when any non-`MainThread` thread is still alive after pytest's summary line — so the next CI ubuntu run names the leaking thread instead of cancelling at the 6 h job limit.

**Architecture:** Pure-test-infrastructure change. Hook appended to existing `tests/conftest.py`, no production code touched, no new dependency. Lives in `tests/` to preserve the core-import-ban invariant. Fast path (clean exit) prints a single confirmation line; leak path prints the full thread inventory.

**Tech Stack:** pytest 8.x (already in deps), Python stdlib (`io`, `os`, `sys`, `threading`, `traceback`, `pathlib`). No `pytest-timeout`, no `pytest-faulthandler` — explicitly rejected during brainstorming.

**User decisions (already made):**
- Scope = confirm root cause only; no thread-leak fix in this plan ("Confirm root cause only" selected during brainstorm).
- Diagnostic approach = post-session thread dump ("Post-session thread dump (Recommended)" selected).
- Spec at `docs/superpowers/specs/2026-06-17-pytest-post-session-hang-diagnostic-design.md` approved verbatim.

---

## File structure

| File | Responsibility | Action |
|------|---------------|--------|
| `tests/conftest.py` | Existing shared pytest fixtures + the new `pytest_sessionfinish` hook | Append hook |
| `.gitignore` | Ignore the local-run dump file `tests/_post_session_dump.txt` | Append one line |
| `docs/superpowers/specs/2026-06-17-pytest-post-session-hang-diagnostic-design.md` | Spec (already committed in `69e3f1d`) | No change |

No new files. No new dependencies.

---

## Task 0: Gitignore the local dump file

**Goal:** `tests/_post_session_dump.txt` never reaches git, even on a hang where the operator forgets it exists.

**Files:**
- Modify: `.gitignore` (append one line at the bottom)

**Acceptance Criteria:**
- [ ] `.gitignore` contains the line `tests/_post_session_dump.txt`.
- [ ] `git check-ignore tests/_post_session_dump.txt` exits 0 (file would be ignored).
- [ ] `git status` does not list the dump file as untracked after a pytest run that triggers the slow-path leak dump.

**Verify:** `git check-ignore -v tests/_post_session_dump.txt` → exits 0, prints `.gitignore:<line>:tests/_post_session_dump.txt	tests/_post_session_dump.txt`.

**Steps:**

- [ ] **Step 1: Append the ignore line**

Append to `.gitignore`:

```
# Local-run output of the post-session thread dump (tests/conftest.py
# pytest_sessionfinish hook). Diagnostics only — never committed.
tests/_post_session_dump.txt
```

- [ ] **Step 2: Sanity-check the rule fires**

```bash
touch tests/_post_session_dump.txt
git check-ignore -v tests/_post_session_dump.txt
rm tests/_post_session_dump.txt
```

Expected: line printed by `git check-ignore`; rm succeeds.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore(tests): gitignore post-session thread-dump artifact"
```

---

## Task 1: Add `pytest_sessionfinish` thread-dump hook

**Goal:** When pytest finishes a session with more than one live thread, write a banner-prefixed inventory of every thread's name, daemon flag, alive state, and Python stack frame to stderr AND to `tests/_post_session_dump.txt`. When only `MainThread` is alive, emit a single confirmation line and return immediately.

**Files:**
- Modify: `tests/conftest.py` — append hook at the end of the file
- Test: `tests/test_conftest_post_session_dump.py` — new file

**Acceptance Criteria:**
- [ ] `pytest_sessionfinish` hook is defined at module scope in `tests/conftest.py`.
- [ ] Fast path: when `threading.enumerate()` returns one thread, stderr receives exactly the line `=== POST-SESSION THREAD DUMP === clean (1 thread)\n` and `tests/_post_session_dump.txt` is not touched (or contains the same line — both acceptable).
- [ ] Slow path: when `threading.enumerate()` returns two or more threads, stderr receives a multi-line dump beginning with `=== POST-SESSION THREAD DUMP === pid=<pid> exitstatus=<int> n_threads=<n>` and the dump file mirrors that content byte-for-byte.
- [ ] Dump includes, for each thread, a line of the form `thread name=<repr> ident=<int> daemon=<bool> alive=<bool>[ (main)]` followed by either the Python stack frames (`traceback.format_stack`) or `<no Python frame — likely in C extension>` when `sys._current_frames()` has no entry for that thread.
- [ ] On linux the dump ends with a line of the form `open fds: <n> → [<sorted file-descriptor names>]`. On macOS / non-linux the `OSError` from `os.listdir('/proc/self/fd/')` is swallowed and that line is absent.
- [ ] Hook never raises. A `try` around the file-mirror is the only error swallowing — the stderr write is not guarded (a failure there is itself diagnostic).
- [ ] Hook signature matches pytest's `pytest_sessionfinish(session, exitstatus)`.

**Verify:**
```
pixi run -- pytest tests/test_conftest_post_session_dump.py -v -p no:cacheprovider
```
Expected: all 4 cases pass; stderr of the test process contains the test-injected banner strings.

**Steps:**

- [ ] **Step 1: Write failing tests** — create `tests/test_conftest_post_session_dump.py`

```python
"""Tests for the pytest_sessionfinish thread-dump hook in tests/conftest.py.

The hook is appended at module scope; we test it by direct import + invocation
(not via a nested pytest run, which would be slow and brittle).
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _load_hook() -> Any:
    """Import the hook function from tests.conftest.

    Lives behind a helper so a future rename surfaces here, not silently
    in every test.
    """
    import tests.conftest as conftest_mod  # noqa: PLC0415

    return conftest_mod.pytest_sessionfinish


def test_fast_path_single_thread_clean_line(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When only MainThread is alive, the hook prints one confirmation line
    and does not enumerate anything.

    Bug catch: a future edit that drops the fast-path guard would dump
    every healthy run with full thread inventory, drowning real signal.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()

    _load_hook()(SimpleNamespace(), 0)

    captured = capsys.readouterr()
    assert (
        captured.err == "=== POST-SESSION THREAD DUMP === clean (1 thread)\n"
    ), f"unexpected fast-path stderr: {captured.err!r}"


def test_slow_path_lists_every_live_thread(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-daemon thread that outlives the main flow must appear in the
    dump with its name, daemon flag, and a Python stack frame.

    Bug catch: a future edit that filters on daemon-only or that uses
    threading.active_count() (which lies about pre-shutdown state on
    some platforms) would hide the very leak this hook was written for.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()

    stop = threading.Event()

    def _leak() -> None:
        stop.wait()

    leaker = threading.Thread(target=_leak, name="kf-test-leaker", daemon=False)
    leaker.start()
    try:
        _load_hook()(SimpleNamespace(), 1)
        captured = capsys.readouterr()
        assert (
            "=== POST-SESSION THREAD DUMP === pid=" in captured.err
        ), f"missing banner: {captured.err!r}"
        assert "n_threads=" in captured.err
        assert "kf-test-leaker" in captured.err, (
            f"leaking thread not in dump: {captured.err!r}"
        )
        assert "daemon=False" in captured.err
        # Stack frame line from _leak's stop.wait() must appear.
        assert "stop.wait" in captured.err or "wait(" in captured.err, (
            "expected the leaker's Python stack frame in the dump"
        )
    finally:
        stop.set()
        leaker.join(timeout=5.0)
        assert not leaker.is_alive(), "test cleanup: leaker thread did not exit"


def test_slow_path_mirrors_dump_to_file_byte_for_byte(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local-run convenience file must mirror the stderr dump exactly.

    Bug catch: an earlier draft of the hook used sys.stderr.getvalue(),
    which only returns content when stderr is a StringIO (e.g. under
    capsys). Under a real run, the file would be empty and the operator
    would have no grep target for the leak.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()

    stop = threading.Event()
    leaker = threading.Thread(
        target=stop.wait, name="kf-test-mirror", daemon=False
    )
    leaker.start()
    try:
        _load_hook()(SimpleNamespace(), 0)
        captured = capsys.readouterr()
        dump_file = tmp_path / "tests" / "_post_session_dump.txt"
        assert dump_file.exists(), "dump file was not created on slow path"
        assert dump_file.read_text() == captured.err, (
            "dump file content does not match stderr verbatim"
        )
    finally:
        stop.set()
        leaker.join(timeout=5.0)


def test_hook_signature_matches_pytest_protocol() -> None:
    """The hook must accept exactly (session, exitstatus) per the pytest
    plugin protocol. A signature drift would silently disable the hook
    (pytest would skip it on call-site arity mismatch).
    """
    import inspect  # noqa: PLC0415

    sig = inspect.signature(_load_hook())
    params = list(sig.parameters)
    assert params == ["session", "exitstatus"], (
        f"hook signature must be (session, exitstatus); got {params!r}"
    )
```

- [ ] **Step 2: Run failing tests** — confirm RED

```
pixi run -- pytest tests/test_conftest_post_session_dump.py -v --no-header
```

Expected: 4 errors / failures, all of the form `AttributeError: module 'tests.conftest' has no attribute 'pytest_sessionfinish'`.

- [ ] **Step 3: Append the hook to `tests/conftest.py`**

At the very end of `tests/conftest.py` (after the last existing fixture), append:

```python
# ---------------------------------------------------------------------------
# Post-session thread-dump diagnostic (spec
# docs/superpowers/specs/2026-06-17-pytest-post-session-hang-diagnostic-design.md).
#
# Pytest finishes its test summary then returns from main(); if any
# non-daemon thread is still alive at that point, threading._shutdown()
# blocks the interpreter indefinitely (observed on ubuntu CI run
# 27693732183 — 6 h job cancel; macOS exits cleanly so the leak is
# linux-platform-primitive-bound). This hook surfaces the live-thread
# inventory + stack frames so the next CI run names the leaker instead
# of timing out at 6 h.
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001
    """Dump live threads + open FDs to stderr after the test summary line.

    Args:
        session: The pytest ``Session`` object. Unused — present to match the
            documented hook signature.
        exitstatus: The integer exit status pytest will return. Echoed in the
            banner so a green vs. red session is distinguishable in CI logs.
    """
    import io  # noqa: PLC0415
    import os  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import threading  # noqa: PLC0415
    import traceback  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    threads = threading.enumerate()
    if len(threads) <= 1:
        # Fast path: only MainThread alive → no leak.
        sys.stderr.write("=== POST-SESSION THREAD DUMP === clean (1 thread)\n")
        return

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
        frame = frames.get(t.ident)
        if frame is None:
            buf.write("    <no Python frame — likely in C extension>\n")
            continue
        for line in traceback.format_stack(frame):
            buf.write("    " + line.rstrip() + "\n")

    try:
        fds = sorted(os.listdir("/proc/self/fd/"))
        buf.write(f"  open fds: {len(fds)} → {fds}\n")
    except OSError:
        # macOS / non-linux — no /proc. Skip the FD inventory.
        pass

    payload = buf.getvalue()
    sys.stderr.write(payload)
    try:
        Path("tests/_post_session_dump.txt").write_text(payload)
    except OSError:
        # cwd not writable (sandboxed CI step) — stderr is authoritative.
        pass
```

- [ ] **Step 4: Run tests to confirm GREEN**

```
pixi run -- pytest tests/test_conftest_post_session_dump.py -v --no-header
```

Expected: 4 passed.

- [ ] **Step 5: Run the full suite + sanity-check the hook fires on a real session**

```
pixi run -- pytest -q --no-header 2>&1 | rg "POST-SESSION THREAD DUMP" | head -3
```

Expected: at least one match. If it says `clean (1 thread)`, the leak is currently not reproducing locally and we wait for CI to surface it. If it lists thread names + stacks, capture the output for the follow-up fix spec.

- [ ] **Step 6: Lint + typecheck the changed file**

```
pixi run -- ruff check tests/conftest.py tests/test_conftest_post_session_dump.py
pixi run -- ruff format --check tests/conftest.py tests/test_conftest_post_session_dump.py
pixi run -- mypy tests/conftest.py
```

Expected: clean for all three (mypy's `tests.*` section is ignored in `pyproject.toml`, so the check is informational on the test file but must pass on conftest).

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_conftest_post_session_dump.py
git commit -m "$(cat <<'EOF'
feat(tests): pytest_sessionfinish thread-dump hook for post-session hang diagnosis

Appends a pytest_sessionfinish hook to tests/conftest.py that, when
threading.enumerate() returns more than MainThread at session end,
dumps every live thread's name, daemon flag, alive state, Python
stack frame, and (on linux) the open-FD inventory — to stderr and
to tests/_post_session_dump.txt (gitignored).

Diagnoses the ubuntu-CI post-pytest hang on commit 4190c8c
(run 27693732183) where pytest exits cleanly at 76 s and the
runner cancels at 6 h. macOS exits cleanly without the hook, so the
leak is linux-platform-bound — most likely epoll / inotify on one of
the kinoforge.core thread sites (heartbeat_loop, pool, sweeper).

Confirm-root-cause-only per the brainstormed spec. Follow-up spec
will use the dump to identify the leaking test and patch the
teardown.

Spec: docs/superpowers/specs/2026-06-17-pytest-post-session-hang-diagnostic-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Validate diagnostic in CI

**Goal:** Push the diagnostic and confirm the next CI run on ubuntu-latest emits the dump banner before the 6 h job timeout — naming at least one non-`MainThread` non-daemon thread plus a stack pointing into a kinoforge module.

**Files:**
- Modify: none in this task — push only.

**Acceptance Criteria:**
- [ ] `git push origin main` succeeds with Tasks 0 + 1 + (already-shipped Stage C) included.
- [ ] GitHub Actions run linked from the push status starts within 2 min.
- [ ] The ubuntu-latest job log contains the literal substring `=== POST-SESSION THREAD DUMP ===` after the pytest summary line.
- [ ] At least one of the listed threads has `daemon=False` and a non-`MainThread` name.
- [ ] The Python stack for that thread names a module in `src/kinoforge/`.
- [ ] If macOS run is green, it should print the fast-path `clean (1 thread)` line — confirms the hook fired AND macOS is unaffected.

**Verify:**
```
gh run watch <run-id> --exit-status
gh api repos/killett/kinoforge/actions/jobs/<ubuntu-job-id>/logs | rg "POST-SESSION THREAD DUMP" -A 40
```
Expected: the rg call returns the banner + thread inventory + stack frames.

**Steps:**

- [ ] **Step 1: Push to origin**

```bash
git push origin main
```

Expected: the previously-buffered commits (Stage C `687f7d4`, c30 sentinel `56df709`, spec `69e3f1d`, gitignore + diagnostic from Tasks 0-1) all land on `origin/main`.

- [ ] **Step 2: Watch the next CI run**

```bash
gh run list --limit 1
gh run watch $(gh run list --limit 1 --json databaseId -q '.[0].databaseId') --exit-status
```

Expected: the watch command tracks the run until completion. macOS likely exits cleanly (with or without the 2 pre-existing failures); ubuntu either exits cleanly OR hangs and gets cancelled — either is fine for diagnosis as long as the dump fired.

- [ ] **Step 3: Pull the ubuntu job log + extract the dump**

```bash
RUN_ID=$(gh run list --limit 1 --json databaseId -q '.[0].databaseId')
UBUNTU_JOB=$(gh run view "$RUN_ID" --json jobs -q '.jobs[] | select(.name | contains("ubuntu")) | .databaseId')
gh api "repos/killett/kinoforge/actions/jobs/${UBUNTU_JOB}/logs" > /tmp/ubuntu-ci-log.txt
rg "POST-SESSION THREAD DUMP" -A 80 /tmp/ubuntu-ci-log.txt | tee /tmp/post-session-dump.txt
```

Expected: `/tmp/post-session-dump.txt` contains the banner, every live thread, and stack frames.

- [ ] **Step 4: Record the finding**

Append a short closeout block to PROGRESS.md under the active workstream:

```markdown
**Post-session hang diagnostic (2026-06-17 spec) FIRED on CI run <RUN_ID>.**
Banner present in ubuntu-latest log; leaking thread named: <thread-name>.
Stack points at: <file:line>. Full dump archived at
/tmp/post-session-dump.txt (local) — fix planned in
docs/superpowers/specs/<follow-up-date>-pytest-thread-leak-fix-design.md.
```

Commit:

```bash
git add PROGRESS.md
git commit -m "docs(progress): post-session hang diagnostic fired — leaker identified"
git push origin main
```

- [ ] **Step 5: Confirm follow-up spec is created (out-of-scope hand-off)**

The follow-up fix spec is NOT in this plan. After the diagnostic fires, hand off to a fresh `/superpowers-extended-cc:brainstorming` session with the dump as input. No code change in this step — closure note only.

---

## Self-review

**Spec coverage:**
- Spec § "Where the diagnostic lives" → Task 1 Step 3 (append to `tests/conftest.py`).
- Spec § "What the hook captures" → Task 1 Step 3 (hook body) + Task 1 Acceptance Criteria items 3, 4, 5.
- Spec § "Where the output goes" → Task 0 (gitignore) + Task 1 (stderr + file).
- Spec § "Activation gate" → Task 1 acceptance criteria (always on, fast-path one-liner).
- Spec § "What it does NOT do" → enforced by what is NOT in this plan: no pytest-timeout, no per-test fixture, no production-code change.
- Spec § "Success criteria" → Task 2 Acceptance Criteria items 3, 4, 5.
- Spec § "Risk" / "Test plan" → covered by Task 1's test file + Task 2's CI validation.

**Placeholder scan:** no "TBD" / "TODO" / "implement later" / vague-error-handling phrases. Every step shows the actual content. Task 2 Step 4 leaves `<thread-name>` and `<file:line>` as fill-in-from-real-output markers — these are post-CI findings, not plan placeholders.

**Type consistency:** the hook signature `pytest_sessionfinish(session, exitstatus)` matches across the test file, the conftest body, and Task 1 Acceptance Criteria. The dump-file path `tests/_post_session_dump.txt` is identical in `.gitignore`, the hook, the tests, and the spec.

**User-gate scan:** Tasks 0 + 1 are regular work (no Scope/Proof keyword matches). Task 2 mentions "confirm" + "diagnose" + "verify that … fires" — keyword `confirm` (Verbs bucket) co-occurs with `prove`-equivalent "fires" (Proof bucket) AND Scope-equivalent "the next CI run" (ordering commitment). Triggers the user-gate rule. Tag Task 2 as `userGate: true`; the HOW is concrete (gh run watch + log grep on `=== POST-SESSION THREAD DUMP ===`) so `requiresUserSpecification` stays false.

---

## Out-of-scope (deferred)

- Fix for the underlying thread leak. Follow-up spec only after Task 2 surfaces the dump.
- `pytest-timeout` CI safety net.
- Audit of every `Thread(...)` site in `src/kinoforge/`.
- `multiprocessing.active_children()` inclusion in the dump (open question in the spec; revisit only if Task 2 points at `pool.py`).
