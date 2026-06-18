"""Tests for the pytest_sessionfinish thread-dump hook in tests/conftest.py.

The hook is appended at module scope; we test it by direct import + invocation
(not via a nested pytest run, which would be slow and brittle).
"""

from __future__ import annotations

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
    """When every non-main thread is a daemon, the hook prints one
    confirmation line and does not enumerate anything.

    threading.enumerate is patched to return only MainThread so the test
    is isolated from leaked non-daemon threads created by other test
    modules (e.g. test_pool_cancel.py).

    Bug catch: a future edit that drops the daemon-filter guard would
    dump every healthy run with full thread inventory, drowning real signal.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()

    main = threading.main_thread()
    # Patch enumerate to return only the main thread — no non-daemon extras.
    monkeypatch.setattr(threading, "enumerate", lambda: [main])

    _load_hook()(SimpleNamespace(), 0)

    captured = capsys.readouterr()
    assert captured.err.startswith("=== POST-SESSION THREAD DUMP === clean "), (
        f"unexpected fast-path stderr: {captured.err!r}"
    )
    assert "no non-daemon extras" in captured.err
    assert captured.err.endswith(")\n")
    # Pin the thread-count token so a future edit that drops it from the
    # banner (e.g. switching to a generic "clean" with no count) regresses
    # loudly — `enumerate` is patched to return exactly [main], so the
    # banner must read "clean (1 threads,".
    assert "clean (1 threads," in captured.err


def test_fast_path_daemon_thread_alive_still_clean(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon thread alongside MainThread must NOT trigger the slow
    path. Daemon threads cannot block threading._shutdown(), so they are
    not the leak this diagnostic was written for.

    threading.enumerate is patched to a controlled list (main + one daemon)
    so the test is independent of any leaked non-daemon threads from other
    test modules.

    Bug catch: regressing the daemon filter back to a naive len(threads)
    check would surface a pytest internal daemon thread as a false-leak
    on every healthy run.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()

    stop = threading.Event()
    daemon_t = threading.Thread(
        target=stop.wait, name="kf-test-daemon-noise", daemon=True
    )
    try:
        # Start AND assert under one try/finally so a failure between
        # start() and the assertion still hits the join — daemon threads
        # don't block shutdown, but leaking them past the test still
        # contradicts the test's isolation goal.
        daemon_t.start()
        main = threading.main_thread()
        # Patch enumerate to return exactly [main, daemon_t].
        monkeypatch.setattr(threading, "enumerate", lambda: [main, daemon_t])

        _load_hook()(SimpleNamespace(), 0)
        captured = capsys.readouterr()
        assert "no non-daemon extras" in captured.err, (
            f"daemon thread incorrectly triggered slow path: {captured.err!r}"
        )
    finally:
        stop.set()
        if daemon_t.is_alive():
            daemon_t.join(timeout=5.0)


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
        assert "=== POST-SESSION THREAD DUMP === pid=" in captured.err, (
            f"missing banner: {captured.err!r}"
        )
        assert "n_threads=" in captured.err
        assert "kf-test-leaker" in captured.err, (
            f"leaking thread not in dump: {captured.err!r}"
        )
        assert "daemon=False" in captured.err
        # At least one Python stack frame marker must appear in the dump.
        assert "File " in captured.err, (
            "expected at least one Python stack frame ('File ' marker) in the dump"
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
    leaker = threading.Thread(target=stop.wait, name="kf-test-mirror", daemon=False)
    leaker.start()
    try:
        _load_hook()(SimpleNamespace(), 0)
        captured = capsys.readouterr()
        dump_file = tmp_path / "tests" / "_post_session_dump.txt"
        assert dump_file.exists(), "dump file was not created on slow path"
        assert dump_file.read_text(encoding="utf-8") == captured.err, (
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
