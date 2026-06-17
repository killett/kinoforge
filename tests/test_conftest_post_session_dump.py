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
    """When only MainThread is alive, the hook prints one confirmation line
    and does not enumerate anything.

    Bug catch: a future edit that drops the fast-path guard would dump
    every healthy run with full thread inventory, drowning real signal.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()

    _load_hook()(SimpleNamespace(), 0)

    captured = capsys.readouterr()
    assert captured.err == "=== POST-SESSION THREAD DUMP === clean (1 thread)\n", (
        f"unexpected fast-path stderr: {captured.err!r}"
    )


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
    leaker = threading.Thread(target=stop.wait, name="kf-test-mirror", daemon=False)
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
