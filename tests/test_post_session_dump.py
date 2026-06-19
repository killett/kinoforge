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
    assert os.path.isdir("/proc/self/fd"), (
        "/proc/self/fd missing on a supposedly linux platform"
    )

    output = _build_dump([], 0)
    assert re.search(r"open fds: \d+ →", output), (
        f"missing fd-count line, got: {output!r}"
    )
