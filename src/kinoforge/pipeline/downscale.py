"""Smooth video downscale to a target vertical resolution.

Used at the orchestrator materialize boundary to shrink an overshooting upscale
(e.g. 1920p -> 1080p) after a height-target upscale. Engine-agnostic: operates on
encoded video bytes, aspect preserved, width kept even for h264. The injectable
``run`` seam is shared with core.frames so tests never spawn ffmpeg.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable

from kinoforge.core.frames import _default_run


def _downscale_argv(src_path: str, target_h: int) -> list[str]:
    """Build the ffmpeg argv lanczos-downscaling *src_path* to ``target_h``.

    Input is a seekable FILE, not stdin: an mp4's moov atom lives at the end of
    the container, and ffmpeg cannot seek back to read it from a non-seekable
    pipe (``pipe:0``) — large inputs fail with 'partial file / unspecified pixel
    format' (exit 183). Output stays on stdout (``pipe:1``) with fragmented
    flags, which needs no seeking.
    """
    return [
        "ffmpeg",
        "-i",
        src_path,
        "-vf",
        f"scale=-2:{target_h}:flags=lanczos",
        "-c:a",
        "copy",
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov",
        "pipe:1",
    ]


def downscale_video_bytes(
    video_bytes: bytes,
    target_h: int,
    *,
    run: Callable[[list[str], bytes], bytes] = _default_run,
) -> bytes:
    """Lanczos-downscale *video_bytes* so its height becomes *target_h*.

    Width is auto-computed to preserve aspect ratio and kept even (``-2``) so the
    result is h264-safe. Audio is stream-copied.

    Args:
        video_bytes: Encoded input video bytes (the overshooting upscale).
        target_h: Desired output vertical resolution; positive even integer.
        run: Injectable subprocess seam ``(argv, stdin) -> stdout`` shared with
            :mod:`kinoforge.core.frames`.

    Returns:
        Encoded MP4 bytes at the requested vertical resolution.

    Raises:
        ValueError: ``target_h`` is not a positive even integer.
        FrameExtractionError: The default seam hits a missing ffmpeg or non-zero
            exit.
    """
    if target_h <= 0 or target_h % 2 != 0:
        raise ValueError(f"target_h must be a positive even int, got {target_h}")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(video_bytes)
        src_path = tf.name
    try:
        return run(_downscale_argv(src_path, target_h), b"")
    finally:
        os.unlink(src_path)
