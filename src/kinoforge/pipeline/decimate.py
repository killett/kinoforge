"""Re-time encoded video to a target fps via ffmpeg's fps filter.

Used for the ``target_fps <= source_fps`` downshift (no GPU) and to trim a
recursive-2x engine's power-of-two overshoot to the exact requested rate.
Reads input from a SEEKABLE temp file, not stdin: an mp4's moov atom lives at
the container tail and ffmpeg cannot seek back to it over pipe:0 (exit 183 on
large inputs). Mirrors :mod:`kinoforge.pipeline.downscale`.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from fractions import Fraction

from kinoforge.core.frames import _default_run


def _fps_arg(target_fps: float) -> str:
    """Serialize *target_fps* for ffmpeg, keeping NTSC rationals exact.

    NTSC-family rates are ``N*1000/1001`` (23.976, 29.97, 59.94, …). Plain
    ``Fraction(29.97).limit_denominator(1001)`` snaps to ``2997/100``, drifting
    off the broadcast-exact ``30000/1001``; detect the family and emit it
    verbatim. Non-fractional / non-NTSC targets fall through to a reduced
    rational.
    """
    if abs(target_fps - round(target_fps)) > 1e-6:
        ntsc_n = target_fps * 1001 / 1000
        if abs(ntsc_n - round(ntsc_n)) < 1e-3:
            return f"fps={round(ntsc_n) * 1000}/1001"
    frac = Fraction(target_fps).limit_denominator(1001)
    if frac.denominator == 1:
        return f"fps={frac.numerator}"
    return f"fps={frac.numerator}/{frac.denominator}"


def _decimate_argv(src_path: str, target_fps: float) -> list[str]:
    """Build the ffmpeg argv re-timing *src_path* to ``target_fps``."""
    return [
        "ffmpeg",
        "-i",
        src_path,
        "-vf",
        _fps_arg(target_fps),
        "-c:a",
        "copy",
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov",
        "pipe:1",
    ]


def decimate_video_fps(
    video_bytes: bytes,
    target_fps: float,
    *,
    run: Callable[[list[str], bytes], bytes] = _default_run,
) -> bytes:
    """Re-time *video_bytes* to *target_fps* using ffmpeg's fps filter.

    Args:
        video_bytes: Encoded input video bytes.
        target_fps: Desired output frame rate; must be > 0.
        run: Injectable subprocess seam ``(argv, stdin) -> stdout`` shared with
            :mod:`kinoforge.core.frames`.

    Returns:
        Encoded MP4 bytes at the requested frame rate.

    Raises:
        ValueError: ``target_fps`` is not positive.
        FrameExtractionError: The default seam hits a missing ffmpeg / non-zero
            exit.
    """
    if target_fps <= 0:
        raise ValueError(f"target_fps must be > 0, got {target_fps}")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(video_bytes)
        src_path = tf.name
    try:
        return run(_decimate_argv(src_path, target_fps), b"")
    finally:
        os.unlink(src_path)
