"""Shared ffmpeg-based last-frame decoder used by every real engine.

Engines call `ffmpeg_last_frame(video_bytes)` to get the last frame as PNG
bytes; the subprocess seam is injectable so tests never spawn a real ffmpeg.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

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
        FrameExtractionError: ffmpeg exited non-zero or *run* raised.
    """
    return run(_FFMPEG_ARGV, video_bytes)
