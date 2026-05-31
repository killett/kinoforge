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


def test_ffmpeg_last_frame_does_not_wrap_or_swallow_run_exceptions() -> None:
    """Exceptions from run propagate unchanged. Helper does NOT catch or wrap.

    Bug this catches: helper adds a try/except that hides real errors
    (e.g. OSError or any non-FrameExtractionError) under a generic message.
    """

    class _Sentinel(RuntimeError):
        pass

    def boom(argv: list[str], stdin: bytes) -> bytes:
        raise _Sentinel("seam blew up")

    with pytest.raises(_Sentinel, match="seam blew up"):
        ffmpeg_last_frame(b"bad", run=boom)


def test_default_run_raises_frame_extraction_error_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped default `_default_run` raises FrameExtractionError on
    non-zero subprocess.run exit and includes stderr in the message.

    Bug this catches: default path returns silently or raises raw
    CalledProcessError, leaking subprocess details into engine code.
    """
    from kinoforge.core import frames

    class _FakeCompleted:
        returncode = 2
        stdout = b""
        stderr = b"Invalid data found when processing input"

    def fake_subprocess_run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return _FakeCompleted()

    monkeypatch.setattr("kinoforge.core.frames.subprocess.run", fake_subprocess_run)

    with pytest.raises(FrameExtractionError, match="ffmpeg exit 2"):
        frames._default_run(_EXPECTED_ARGV, b"x")


def test_ffmpeg_last_frame_passes_empty_bytes_through_without_special_casing() -> None:
    """Empty video_bytes still calls run; helper does not short-circuit.

    Bug this catches: helper grows an `if not video_bytes: return b""` guard
    that hides what is actually a caller bug (empty artifact bytes), making
    the empty-PNG output silently masquerade as a successful extraction.
    """
    calls: list[bytes] = []

    def fake_run(argv: list[str], stdin: bytes) -> bytes:
        calls.append(stdin)
        return b"PNG"

    out = ffmpeg_last_frame(b"", run=fake_run)

    assert out == b"PNG"
    assert calls == [b""]


def test_default_run_wraps_file_not_found_as_frame_extraction_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffmpeg not on PATH surfaces as FrameExtractionError, not raw FileNotFoundError.

    Bug this catches: production deployments without ffmpeg installed get a
    cryptic FileNotFoundError instead of an actionable 'ffmpeg not found' message.
    """
    from kinoforge.core import frames

    def fake_subprocess_run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise FileNotFoundError(2, "No such file or directory: 'ffmpeg'")

    monkeypatch.setattr("kinoforge.core.frames.subprocess.run", fake_subprocess_run)

    with pytest.raises(FrameExtractionError, match="ffmpeg not found on PATH"):
        frames._default_run(["ffmpeg"], b"x")
