"""MP4 encoder helper for diffusers-engine servers.

Produces H.264 / yuv420p / crf=19 video matching the existing Wan 2.1
output profile (see successful-generations.md entries #5 + #7).
"""

from __future__ import annotations

from pathlib import Path

import imageio.v3 as iio
import numpy as np


def write_mp4(frames: np.ndarray, fps: int, path: Path) -> None:
    """Encode ``frames`` to H.264/MP4 at ``path``.

    Args:
        frames: 4-D uint8 array of shape ``(num_frames, height, width, 3)``
            with RGB channel order.
        fps: Frames per second; written into the container's time base.
        path: Destination MP4 path. Parent directory must exist.

    Raises:
        TypeError: ``frames`` is not a uint8 numpy array.
        ValueError: ``frames`` is not 4-dimensional with a final
            channel dimension of 3.
    """
    if not isinstance(frames, np.ndarray) or frames.dtype != np.uint8:
        raise TypeError(
            f"write_mp4 expects a uint8 ndarray; got "
            f"{type(frames).__name__} dtype={getattr(frames, 'dtype', None)}"
        )
    if frames.ndim != 4 or frames.shape[-1] != 3:
        raise ValueError(
            f"write_mp4 expects a 4-D (T, H, W, 3) array; got shape {frames.shape}"
        )
    iio.imwrite(
        str(path),
        frames,
        fps=fps,
        codec="libx264",
        pixelformat="yuv420p",
        macro_block_size=1,
        ffmpeg_params=["-crf", "19"],
    )
