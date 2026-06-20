"""Round-trip tests for the diffusers servers' MP4 encoder helper.

Verifies the encoder produces H.264/yuv420p at the expected
dimensions, frame count, and frame rate. Catches silent codec or
container drift away from the existing Wan 2.1 output profile.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kinoforge.engines.diffusers.servers._video_io import write_mp4


def _ffprobe(path: Path) -> dict[str, Any]:
    """Return ffprobe's JSON metadata for a video file."""
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            str(path),
        ]
    )
    return json.loads(out)


def _make_checkerboard(n_frames: int, h: int, w: int) -> np.ndarray:
    """Return a uint8 (n_frames, h, w, 3) array of red/green/blue alternating frames."""
    frames = np.zeros((n_frames, h, w, 3), dtype=np.uint8)
    palette = [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
    ]
    for i in range(n_frames):
        r, g, b = palette[i % 3]
        frames[i, :, :, 0] = r
        frames[i, :, :, 1] = g
        frames[i, :, :, 2] = b
    return frames


def test_write_mp4_produces_h264_yuv420p(tmp_path: Path) -> None:
    # Bug caught: codec or pix_fmt silently drifting away from the
    # Wan 2.1 reference profile (h264, yuv420p).
    frames = _make_checkerboard(5, 64, 64)
    out = tmp_path / "out.mp4"
    write_mp4(frames, fps=16, path=out)
    assert out.exists() and out.stat().st_size > 0
    meta = _ffprobe(out)
    streams = meta["streams"]
    assert len(streams) == 1
    s = streams[0]
    assert s["codec_name"] == "h264"
    assert s["pix_fmt"] == "yuv420p"


def test_write_mp4_preserves_dimensions(tmp_path: Path) -> None:
    # Bug caught: encoder silently up-scales or crops the input.
    frames = _make_checkerboard(5, 64, 96)
    out = tmp_path / "out.mp4"
    write_mp4(frames, fps=16, path=out)
    meta = _ffprobe(out)
    s = meta["streams"][0]
    assert s["width"] == 96
    assert s["height"] == 64


def test_write_mp4_frame_count_matches_input(tmp_path: Path) -> None:
    # Bug caught: off-by-one in the iterator or GOP-boundary drop.
    frames = _make_checkerboard(11, 64, 64)
    out = tmp_path / "out.mp4"
    write_mp4(frames, fps=16, path=out)
    meta = _ffprobe(out)
    s = meta["streams"][0]
    assert int(s["nb_frames"]) == 11


def test_write_mp4_frame_rate_matches_argument(tmp_path: Path) -> None:
    # Bug caught: ignoring fps argument and emitting at default rate.
    frames = _make_checkerboard(5, 64, 64)
    out = tmp_path / "out.mp4"
    write_mp4(frames, fps=24, path=out)
    meta = _ffprobe(out)
    s = meta["streams"][0]
    assert s["r_frame_rate"] == "24/1"


def test_write_mp4_rejects_wrong_dtype(tmp_path: Path) -> None:
    # Bug caught: silently casting float -> uint8 producing garbled output.
    frames = np.zeros((5, 64, 64, 3), dtype=np.float32)
    out = tmp_path / "out.mp4"
    with pytest.raises(TypeError, match="uint8"):
        write_mp4(frames, fps=16, path=out)
    assert not out.exists()


def test_write_mp4_rejects_wrong_shape(tmp_path: Path) -> None:
    # Bug caught: accepting (H, W, 3) instead of (T, H, W, 3).
    frames = np.zeros((64, 64, 3), dtype=np.uint8)
    out = tmp_path / "out.mp4"
    with pytest.raises(ValueError, match="4-D"):
        write_mp4(frames, fps=16, path=out)
    assert not out.exists()
