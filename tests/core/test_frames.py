"""Tests for core.frames.ffmpeg_last_frame — shared frame decoder.

Spec: docs/superpowers/specs/2026-05-30-extract-last-frame-design.md §4.1
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import FrameExtractionError
from kinoforge.core.frames import ffmpeg_last_frame

_EXPECTED_ARGV = [
    "ffmpeg",
    "-sseof",
    "-1",
    "-i",
    "pipe:0",
    "-frames:v",
    "1",
    "-f",
    "image2pipe",
    "-vcodec",
    "png",
    "pipe:1",
]


def test_ffmpeg_last_frame_calls_run_with_canonical_argv() -> None:
    """The exact argv we ship to ffmpeg must be the documented one.

    Bug this catches: anyone reorders flags or silently swaps -vcodec for
    -c:v; the test pins the exact wire format the helper guarantees.
    """
    calls: list[tuple[list[str], bytes]] = []

    def fake_run(argv: list[str], stdin: bytes) -> bytes:
        calls.append((argv, stdin))
        return b"PNG_OUT"

    ffmpeg_last_frame(b"VIDEO_IN", run=fake_run)

    assert len(calls) == 1
    assert calls[0][0] == _EXPECTED_ARGV
    assert calls[0][1] == b"VIDEO_IN"


def test_ffmpeg_last_frame_returns_run_output_verbatim() -> None:
    """Helper passes through bytes from run without re-encoding.

    Bug this catches: helper tries to decode/re-encode the PNG and corrupts
    arbitrary bytes that happen to look like image headers.
    """
    sentinel = b"\x89PNG\r\n\x1a\nDETERMINISTIC"

    def fake_run(argv: list[str], stdin: bytes) -> bytes:
        return sentinel

    assert ffmpeg_last_frame(b"anything", run=fake_run) is sentinel


def test_ffmpeg_last_frame_wraps_run_exception_as_frame_extraction_error() -> None:
    """A raising run is the production failure shape; helper must surface it
    as FrameExtractionError so callers have ONE exception type to catch.

    Bug this catches: callers wrap the wrong exception type and downstream
    error handling misses the real failure mode.
    """

    def boom(argv: list[str], stdin: bytes) -> bytes:
        raise FrameExtractionError("ffmpeg exit 1: invalid input")

    with pytest.raises(FrameExtractionError, match="ffmpeg exit 1"):
        ffmpeg_last_frame(b"bad", run=boom)


def test_default_run_raises_frame_extraction_error_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped default `_default_run` raises FrameExtractionError on
    non-zero subprocess.run exit and includes stderr in the message.

    Bug this catches: default path returns silently or raises raw
    CalledProcessError, leaking subprocess details into engine code.
    """
    import subprocess

    from kinoforge.core import frames

    class _FakeCompleted:
        returncode = 2
        stdout = b""
        stderr = b"Invalid data found when processing input"

    def fake_subprocess_run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    with pytest.raises(FrameExtractionError, match="ffmpeg exit 2"):
        frames._default_run(_EXPECTED_ARGV, b"x")
