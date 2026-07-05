"""Tests for core.frames.ffmpeg_last_frame — shared frame decoder.

Spec: docs/superpowers/specs/2026-05-30-extract-last-frame-design.md §4.1
"""

from __future__ import annotations

from pathlib import Path

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


# --- multi-frame extraction (count + interval modes) -------------------------
#
# Spec: PROGRESS.md RESUME SNAPSHOT 2026-07-04 — count mode spreads `total`
# frames evenly (first + last + evenly spaced between); interval mode takes a
# frame every N seconds plus ALWAYS the last frame; returns PNG bytes per
# frame in temporal order; duration probed via injectable seam.


def _argv_at(path: str, ts: str) -> list[str]:
    """Expected ffmpeg argv for a single frame seek at *ts* seconds."""
    return [
        "ffmpeg",
        "-ss",
        ts,
        "-i",
        path,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]


def _argv_last(path: str) -> list[str]:
    """Expected ffmpeg argv for the last-frame extraction from a path."""
    return [
        "ffmpeg",
        "-sseof",
        "-1",
        "-i",
        path,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]


class _CapturingRun:
    """Fake run seam returning a distinct payload per call, recording argv."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str], stdin: bytes) -> bytes:
        self.argvs.append(argv)
        return f"PNG{len(self.argvs)}".encode()


def test_frames_by_count_seeks_evenly_and_ends_with_last_frame() -> None:
    """count=5 over a 10s video seeks 0 / 2.5 / 5 / 7.5 then last frame.

    Bug this catches: spacing computed as i*D/total (instead of i*D/(total-1))
    would seek 0/2/4/6 and never reach the final quarter of the video.
    Expected timestamps hand-computed: 4 evenly spaced over [0, 10) at
    10/(5-1) = 2.5s apart, 5th frame from end-of-stream.
    """
    from kinoforge.core.frames import ffmpeg_frames_by_count

    run = _CapturingRun()

    out = ffmpeg_frames_by_count("vid.mp4", 5, run=run, probe=lambda path: 10.0)

    assert run.argvs == [
        _argv_at("vid.mp4", "0.000000"),
        _argv_at("vid.mp4", "2.500000"),
        _argv_at("vid.mp4", "5.000000"),
        _argv_at("vid.mp4", "7.500000"),
        _argv_last("vid.mp4"),
    ]
    assert out == [b"PNG1", b"PNG2", b"PNG3", b"PNG4", b"PNG5"]


def test_frames_by_count_total_one_is_last_frame_only_and_never_probes() -> None:
    """count=1 degenerates to the existing last-frame behaviour, no ffprobe.

    Bug this catches: the total==1 path calling the duration probe would add
    an ffprobe dependency (and a failure mode) to the continuity-parity case
    that by spec needs only the last frame.
    """
    from kinoforge.core.frames import ffmpeg_frames_by_count

    run = _CapturingRun()

    def probe_must_not_run(path: str | Path) -> float:
        raise AssertionError("probe called for total==1")

    out = ffmpeg_frames_by_count("vid.mp4", 1, run=run, probe=probe_must_not_run)

    assert run.argvs == [_argv_last("vid.mp4")]
    assert out == [b"PNG1"]


def test_frames_by_count_total_two_is_first_plus_last() -> None:
    """count=2 yields exactly the first frame (t=0) and the last frame.

    Bug this catches: a division-by-zero or off-by-one on (total-1) that
    drops the t=0 frame or seeks somewhere mid-video for a 2-frame request.
    """
    from kinoforge.core.frames import ffmpeg_frames_by_count

    run = _CapturingRun()

    out = ffmpeg_frames_by_count("v.mp4", 2, run=run, probe=lambda path: 8.0)

    assert run.argvs == [
        _argv_at("v.mp4", "0.000000"),
        _argv_last("v.mp4"),
    ]
    assert out == [b"PNG1", b"PNG2"]


@pytest.mark.parametrize("bad_total", [0, -3])
def test_frames_by_count_rejects_non_positive_total(bad_total: int) -> None:
    """total < 1 raises ValueError instead of returning an empty list.

    Bug this catches: a silent empty return would make a caller's arithmetic
    bug (e.g. computed 0 frames) look like a video with no extractable frames.
    """
    from kinoforge.core.frames import ffmpeg_frames_by_count

    def no_run(argv: list[str], stdin: bytes) -> bytes:
        raise AssertionError("run called for invalid total")

    with pytest.raises(ValueError, match="total"):
        ffmpeg_frames_by_count("v.mp4", bad_total, run=no_run, probe=lambda p: 5.0)


def test_frames_by_interval_seeks_every_n_seconds_then_last_frame() -> None:
    """interval=2 over a 5s video seeks 0 / 2 / 4 then appends the last frame.

    Bug this catches: starting the grid at t=N (dropping the first frame) or
    forgetting the forced final last-frame entry. Hand-computed grid: k*2 for
    k=0,1,2 (all < 5.0), then end-of-stream.
    """
    from kinoforge.core.frames import ffmpeg_frames_by_interval

    run = _CapturingRun()

    out = ffmpeg_frames_by_interval("clip.mp4", 2.0, run=run, probe=lambda path: 5.0)

    assert run.argvs == [
        _argv_at("clip.mp4", "0.000000"),
        _argv_at("clip.mp4", "2.000000"),
        _argv_at("clip.mp4", "4.000000"),
        _argv_last("clip.mp4"),
    ]
    assert out == [b"PNG1", b"PNG2", b"PNG3", b"PNG4"]


def test_frames_by_interval_grid_point_on_duration_is_not_seeked() -> None:
    """interval=2 over an exactly-4s video seeks 0 / 2 then last — never t=4.

    Bug this catches: a `<=` boundary check seeking at t == duration, which
    lands past the final frame and makes ffmpeg emit nothing (or a duplicate
    of the forced last frame) for that call.
    """
    from kinoforge.core.frames import ffmpeg_frames_by_interval

    run = _CapturingRun()

    out = ffmpeg_frames_by_interval("c.mp4", 2.0, run=run, probe=lambda path: 4.0)

    assert run.argvs == [
        _argv_at("c.mp4", "0.000000"),
        _argv_at("c.mp4", "2.000000"),
        _argv_last("c.mp4"),
    ]
    assert out == [b"PNG1", b"PNG2", b"PNG3"]


@pytest.mark.parametrize("bad_interval", [0.0, -1.5])
def test_frames_by_interval_rejects_non_positive_interval(
    bad_interval: float,
) -> None:
    """interval <= 0 raises ValueError before any subprocess work.

    Bug this catches: interval=0 entering the grid loop and spinning forever
    (or emitting an unbounded frame list) instead of failing fast.
    """
    from kinoforge.core.frames import ffmpeg_frames_by_interval

    def no_run(argv: list[str], stdin: bytes) -> bytes:
        raise AssertionError("run called for invalid interval")

    with pytest.raises(ValueError, match="interval"):
        ffmpeg_frames_by_interval(
            "c.mp4", bad_interval, run=no_run, probe=lambda p: 5.0
        )


def test_default_probe_duration_parses_ffprobe_csv_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default probe runs ffprobe on the path and parses the CSV duration.

    Bug this catches: argv drift (wrong -show_entries or missing csv=p=0)
    that makes ffprobe print 'duration=5.0' — float() would then raise on
    every real video. Expected value 5.271 comes from the fixture stdout,
    not from the parser.
    """
    from kinoforge.core import frames

    seen: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0
        stdout = b"5.271000\n"
        stderr = b""

    def fake_subprocess_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.append(list(argv))
        return _FakeCompleted()

    monkeypatch.setattr("kinoforge.core.frames.subprocess.run", fake_subprocess_run)

    assert frames._default_probe_duration("some.mp4") == 5.271
    assert seen == [
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            "some.mp4",
        ]
    ]


def test_default_probe_duration_raises_frame_extraction_error_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffprobe non-zero exit surfaces as FrameExtractionError with stderr.

    Bug this catches: a corrupt/zero-byte video raising raw CalledProcessError
    or ValueError from float('') instead of the domain error type engines
    already handle.
    """
    from kinoforge.core import frames

    class _FakeCompleted:
        returncode = 1
        stdout = b""
        stderr = b"moov atom not found"

    def fake_subprocess_run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return _FakeCompleted()

    monkeypatch.setattr("kinoforge.core.frames.subprocess.run", fake_subprocess_run)

    with pytest.raises(FrameExtractionError, match="ffprobe exit 1"):
        frames._default_probe_duration("bad.mp4")


def test_default_probe_duration_raises_on_non_numeric_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffprobe exit 0 with non-numeric stdout (e.g. 'N/A') is a domain error.

    Bug this catches: streams without a container duration make ffprobe print
    'N/A' with exit 0; bare float() would leak ValueError('could not convert
    string to float') past the FrameExtractionError contract.
    """
    from kinoforge.core import frames

    class _FakeCompleted:
        returncode = 0
        stdout = b"N/A\n"
        stderr = b""

    def fake_subprocess_run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return _FakeCompleted()

    monkeypatch.setattr("kinoforge.core.frames.subprocess.run", fake_subprocess_run)

    with pytest.raises(FrameExtractionError, match="unparseable ffprobe duration"):
        frames._default_probe_duration("no-duration.mp4")
