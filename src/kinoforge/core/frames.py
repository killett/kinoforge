"""Shared ffmpeg-based frame decoders used by every real engine.

Engines call `ffmpeg_last_frame(video_bytes)` to get the last frame as PNG
bytes; QA tooling calls `ffmpeg_frames_by_count` / `ffmpeg_frames_by_interval`
to pull evenly spread frames from an on-disk video. Subprocess and ffprobe
seams are injectable so tests never spawn real binaries.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from kinoforge.core.errors import FrameExtractionError

#: The exact argv shipped to ffmpeg. Reads video from stdin, writes one PNG
#: frame (the last) to stdout. `-sseof -1` seeks to 1s before EOF; combined
#: with `-frames:v 1` ffmpeg emits a single frame at end-of-stream.
_FFMPEG_ARGV: list[str] = [
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


def _default_run(argv: list[str], stdin: bytes) -> bytes:
    """Run *argv* with *stdin* piped; return stdout; raise on non-zero exit.

    Args:
        argv: The ffmpeg command line.
        stdin: Bytes piped to the subprocess on stdin.

    Returns:
        The subprocess's stdout bytes.

    Raises:
        FrameExtractionError: ffmpeg is not on PATH, or the subprocess
            exited non-zero. Message includes a truncated stderr substring
            for diagnostics on non-zero exits.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            argv, input=stdin, capture_output=True, check=False
        )
    except FileNotFoundError as exc:
        raise FrameExtractionError(f"ffmpeg not found on PATH: {exc}") from exc
    if proc.returncode != 0:
        stderr_snip = proc.stderr.decode(errors="replace")[:512]
        raise FrameExtractionError(f"ffmpeg exit {proc.returncode}: {stderr_snip}")
    return proc.stdout


def ffmpeg_last_frame(
    video_bytes: bytes,
    *,
    run: Callable[[list[str], bytes], bytes] = _default_run,
) -> bytes:
    """Decode the last frame of *video_bytes* as PNG bytes.

    Args:
        video_bytes: Encoded video bytes (any format ffmpeg accepts).
        run: Injectable subprocess seam ``(argv, stdin) -> stdout``.

    Returns:
        PNG-encoded last frame as bytes.

    Raises:
        FrameExtractionError: When the default ``_default_run`` seam is used
            and either ``ffmpeg`` is missing from PATH or ffmpeg exits
            non-zero. Custom seams that raise other exception types
            propagate unchanged — wrap or convert them in the seam if a
            single failure type is needed.
    """
    return run(_FFMPEG_ARGV, video_bytes)


_FRAME_OUTPUT_ARGV: list[str] = [
    "-frames:v",
    "1",
    "-f",
    "image2pipe",
    "-vcodec",
    "png",
    "pipe:1",
]


def _argv_at(video_path: str, timestamp_s: float) -> list[str]:
    """Build the ffmpeg argv extracting one PNG frame at *timestamp_s*.

    Args:
        video_path: Path to the video file on disk.
        timestamp_s: Seek position in seconds from the start.

    Returns:
        The full ffmpeg argv.
    """
    return [
        "ffmpeg",
        "-ss",
        f"{timestamp_s:.6f}",
        "-i",
        video_path,
        *_FRAME_OUTPUT_ARGV,
    ]


def _argv_last(video_path: str) -> list[str]:
    """Build the ffmpeg argv extracting the last frame of *video_path*.

    Same `-sseof -1` semantics as `ffmpeg_last_frame`, reading from a path
    instead of stdin.

    Args:
        video_path: Path to the video file on disk.

    Returns:
        The full ffmpeg argv.
    """
    return ["ffmpeg", "-sseof", "-1", "-i", video_path, *_FRAME_OUTPUT_ARGV]


def _default_probe_duration(video_path: str | Path) -> float:
    """Probe the container duration of *video_path* in seconds via ffprobe.

    Args:
        video_path: Path to the video file on disk.

    Returns:
        Duration in seconds.

    Raises:
        FrameExtractionError: ffprobe missing from PATH, non-zero exit, or
            output that does not parse as a float (e.g. ``N/A`` for streams
            without a container duration).
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        raise FrameExtractionError(f"ffprobe not found on PATH: {exc}") from exc
    if proc.returncode != 0:
        stderr_snip = proc.stderr.decode(errors="replace")[:512]
        raise FrameExtractionError(f"ffprobe exit {proc.returncode}: {stderr_snip}")
    raw = proc.stdout.decode(errors="replace").strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise FrameExtractionError(
            f"unparseable ffprobe duration {raw!r} for {video_path}"
        ) from exc


def _default_probe_run(argv: list[str]) -> bytes:
    """Run an ffprobe *argv*; return stdout; raise on missing binary / non-zero.

    Args:
        argv: The ffprobe command line.

    Returns:
        The subprocess's stdout bytes.

    Raises:
        FrameExtractionError: ffprobe missing from PATH or non-zero exit.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        raise FrameExtractionError(f"ffprobe not found on PATH: {exc}") from exc
    if proc.returncode != 0:
        stderr_snip = proc.stderr.decode(errors="replace")[:512]
        raise FrameExtractionError(f"ffprobe exit {proc.returncode}: {stderr_snip}")
    return proc.stdout


def ffprobe_dims(
    video_path: str | Path,
    *,
    run: Callable[[list[str]], bytes] = _default_probe_run,
) -> tuple[int, int]:
    """Probe ``(width, height)`` of the first video stream via ffprobe.

    Args:
        video_path: Path to the video file on disk.
        run: Injectable seam ``(argv) -> stdout`` so tests spawn no binary.

    Returns:
        ``(width, height)`` in pixels.

    Raises:
        FrameExtractionError: ffprobe missing / non-zero exit, or output that
            does not parse as ``WxH``.
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=s=x:p=0",
        str(video_path),
    ]
    raw = run(argv).decode(errors="replace").strip()
    try:
        w_str, h_str = raw.split("x")
        return int(w_str), int(h_str)
    except ValueError as exc:
        raise FrameExtractionError(
            f"unparseable ffprobe dims {raw!r} for {video_path}"
        ) from exc


def ffprobe_fps(
    video_path: str | Path,
    *,
    run: Callable[[list[str]], bytes] = _default_probe_run,
) -> float:
    """Probe the frame rate of the first video stream via ffprobe.

    Reads ``r_frame_rate`` (the base frame rate, a rational such as ``16/1``
    or ``30000/1001``) rather than ``avg_frame_rate`` — the latter is ``0/0``
    for streamed / variable-frame-rate inputs.

    Args:
        video_path: Path to the video file on disk.
        run: Injectable seam ``(argv) -> stdout`` so tests spawn no binary.

    Returns:
        Frame rate in frames per second.

    Raises:
        FrameExtractionError: ffprobe missing / non-zero exit, output that is
            not a ``num/den`` rational, or a zero denominator.
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    raw = run(argv).decode(errors="replace").strip()
    try:
        num_str, den_str = raw.split("/")
        num, den = float(num_str), float(den_str)
        if den == 0.0:
            raise ValueError("zero denominator")
        return num / den
    except ValueError as exc:
        raise FrameExtractionError(
            f"unparseable ffprobe frame rate {raw!r} for {video_path}"
        ) from exc


def ffmpeg_frames_by_count(
    video_path: str | Path,
    total: int,
    *,
    run: Callable[[list[str], bytes], bytes] = _default_run,
    probe: Callable[[str | Path], float] = _default_probe_duration,
) -> list[bytes]:
    """Extract *total* PNG frames evenly spread through *video_path*.

    Frames are the first frame, the last frame, and ``total - 2`` frames
    evenly spaced between them. ``total == 1`` returns just the last frame
    (matching `ffmpeg_last_frame` semantics) and never probes duration.

    Args:
        video_path: Path to the video file on disk.
        total: Number of frames to extract; must be >= 1.
        run: Injectable subprocess seam ``(argv, stdin) -> stdout``.
        probe: Injectable duration seam ``(video_path) -> seconds``.

    Returns:
        PNG-encoded frames as bytes, in temporal order.

    Raises:
        ValueError: ``total`` is less than 1.
        FrameExtractionError: The default seams hit a missing binary,
            non-zero exit, or unparseable ffprobe output.
    """
    if total < 1:
        raise ValueError(f"total must be >= 1, got {total}")
    path = str(video_path)
    if total == 1:
        return [run(_argv_last(path), b"")]
    duration = probe(video_path)
    timestamps = [i * duration / (total - 1) for i in range(total - 1)]
    frames = [run(_argv_at(path, ts), b"") for ts in timestamps]
    frames.append(run(_argv_last(path), b""))
    return frames


def ffmpeg_frames_by_interval(
    video_path: str | Path,
    interval_s: float,
    *,
    run: Callable[[list[str], bytes], bytes] = _default_run,
    probe: Callable[[str | Path], float] = _default_probe_duration,
) -> list[bytes]:
    """Extract a PNG frame every *interval_s* seconds plus the last frame.

    Frames are taken at t=0, t=interval_s, t=2*interval_s, ... for every
    grid point strictly before the video duration, and the last frame is
    always appended as the final entry.

    Args:
        video_path: Path to the video file on disk.
        interval_s: Seconds between frames; must be > 0.
        run: Injectable subprocess seam ``(argv, stdin) -> stdout``.
        probe: Injectable duration seam ``(video_path) -> seconds``.

    Returns:
        PNG-encoded frames as bytes, in temporal order.

    Raises:
        ValueError: ``interval_s`` is not positive.
        FrameExtractionError: The default seams hit a missing binary,
            non-zero exit, or unparseable ffprobe output.
    """
    if interval_s <= 0:
        raise ValueError(f"interval_s must be > 0, got {interval_s}")
    path = str(video_path)
    duration = probe(video_path)
    frames: list[bytes] = []
    k = 0
    while k * interval_s < duration:
        frames.append(run(_argv_at(path, k * interval_s), b""))
        k += 1
    frames.append(run(_argv_last(path), b""))
    return frames
