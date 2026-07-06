"""RifeRuntime offline: schedule -> frame ops -> real ffmpeg mux.

No torch, no GPU: a fake ``infer`` seam stands in for the RIFE model so the
schedule + mux plumbing is exercised with a real ffmpeg encode. The live pod
path is proven in Task 12.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from kinoforge.engines.diffusers.servers._video_io import write_mp4
from kinoforge.interpolators.rife._runtime import RifeRuntime

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _src_clip(path: Path, n: int = 16, fps: int = 16) -> None:
    """Write an ``n``-frame ``fps`` clip with per-frame varying content."""
    frames = np.zeros((n, 64, 64, 3), dtype=np.uint8)
    for i in range(n):
        frames[i, :, :, 0] = (i * 16) % 256  # ramp red so frames differ
    write_mp4(frames, fps, path)


def test_16_to_32_doubles_frame_count_at_target_fps(tmp_path: Path) -> None:
    # Bug caught: an off-by-one in the schedule loop (or muxing at source fps)
    # would deliver the wrong frame count / wrong rate. 16fps->32fps over the
    # clip's duration must yield exactly 2x the source frame count.
    src = tmp_path / "src.mp4"
    _src_clip(src, n=16, fps=16)

    calls = {"n": 0}

    def infer(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
        calls["n"] += 1
        return ((a.astype(np.int16) + b.astype(np.int16)) // 2).astype(np.uint8)

    rt = RifeRuntime(weights_dir=tmp_path, model="rife49", infer=infer)
    result = rt.interpolate(src, 32.0, {})

    assert round(result["input_fps"]) == 16
    assert round(result["output_fps"]) == 32
    assert result["output_frame_count"] == 2 * result["input_frame_count"]
    # midpoints (t != 0) actually invoked the model.
    assert calls["n"] > 0

    out = src.parent / result["filename"]
    assert out.exists()
    assert out.stat().st_size > 0
    assert result["size"] == out.stat().st_size
    assert len(result["sha256"]) == 64


def test_passthrough_equal_fps_copies_all_frames(tmp_path: Path) -> None:
    # Bug caught: routing an equal-fps request through the model would waste GPU
    # and could alter frames; equal fps must copy every source frame verbatim
    # (t == 0 for all schedule entries) and never call infer.
    src = tmp_path / "src.mp4"
    _src_clip(src, n=12, fps=24)

    def infer(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
        raise AssertionError("infer must not be called at equal fps")

    rt = RifeRuntime(weights_dir=tmp_path, model="rife49", infer=infer)
    result = rt.interpolate(src, 24.0, {})

    assert result["output_frame_count"] == result["input_frame_count"]
    assert round(result["output_fps"]) == 24
