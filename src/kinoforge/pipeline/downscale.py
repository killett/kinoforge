"""Smooth video downscale to a target vertical resolution.

Used at the orchestrator materialize boundary to shrink an overshooting upscale
(e.g. 1920p -> 1080p) after a height-target upscale. Engine-agnostic: operates on
encoded video bytes, aspect preserved, width kept even for h264. The injectable
``run`` seam is shared with core.frames so tests never spawn ffmpeg.
"""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.core.frames import _default_run


def _downscale_argv(target_h: int) -> list[str]:
    """Build the ffmpeg argv lanczos-downscaling stdin video to ``target_h``."""
    return [
        "ffmpeg",
        "-i",
        "pipe:0",
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
    return run(_downscale_argv(target_h), video_bytes)
