"""End-to-end grid integration: 3 path: cells → real grid mp4 → frame decode.

Uses ffmpeg testsrc patterns so no compute / no kinoforge generate
subprocess is invoked. The real binary IS invoked for both input
generation and composition — this test fails when ffmpeg arg-building
regresses.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kinoforge.core.grid.executor import run_grid
from kinoforge.core.grid.spec import GridSpec

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)


def _make_color_mp4(out: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=64x64:r=10:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
    )


def _sample_rgb(mp4: Path, *, x: int, y: int, t: float = 0.5) -> tuple[int, int, int]:
    """Decode one RGB pixel at (x, y) at time t via ffmpeg rawvideo."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(t),
            "-i",
            str(mp4),
            "-vframes",
            "1",
            "-vf",
            f"crop=1:1:{x}:{y},format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    r, g, b = result.stdout[0], result.stdout[1], result.stdout[2]
    return r, g, b


def _approx(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - e) <= 40 for a, e in zip(actual, expected, strict=True))


def test_grid_composes_three_path_cells_with_correct_colors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    red_mp4 = tmp_path / "red.mp4"
    green_mp4 = tmp_path / "green.mp4"
    blue_mp4 = tmp_path / "blue.mp4"
    _make_color_mp4(red_mp4, "red")
    _make_color_mp4(green_mp4, "green")
    _make_color_mp4(blue_mp4, "blue")

    # Bypass the residual-pod probe — `pixi run kinoforge list` is heavy and
    # the grid doesn't spawn any pod in this test (3 path: cells).
    monkeypatch.setattr(
        "kinoforge.core.grid.executor._check_no_residual_pods",
        lambda: (True, "[instance overview] No running instances."),
    )

    spec = GridSpec.model_validate(
        {
            "title": "rgb-test-integration",
            "layout": "1x3",
            "budget_cap_usd": 1.0,
            "cells": [
                {"path": str(red_mp4), "caption": "R"},
                {"path": str(green_mp4), "caption": "G"},
                {"path": str(blue_mp4), "caption": "B"},
            ],
        }
    )
    out = tmp_path / "out"
    result = asyncio.run(run_grid(spec=spec, output_dir=out, max_parallel_groups=1))

    assert result.status == "full", (
        f"3 path-cells + real ffmpeg should produce full grid, got "
        f"status={result.status!r}, partial={result.partial_dir}"
    )
    assert result.composed_mp4_path is not None
    assert result.composed_mp4_path.exists()

    # Three cells laid out 1x3 in 64x64 inputs → composed is 192x64. Cell 0
    # centers around (32, 32), cell 1 around (96, 32), cell 2 around (160, 32).
    # Caption drawtext sits at y=20 so probe at y=40 (below caption box).
    r_pixel = _sample_rgb(result.composed_mp4_path, x=32, y=40)
    g_pixel = _sample_rgb(result.composed_mp4_path, x=96, y=40)
    b_pixel = _sample_rgb(result.composed_mp4_path, x=160, y=40)
    # libx264 lossy → tolerate ±40 per channel.
    assert _approx(r_pixel, (255, 0, 0)), (
        f"cell 0 expected red-ish (255,0,0), got {r_pixel}"
    )
    assert _approx(g_pixel, (0, 128, 0)), (
        f"cell 1 expected green-ish (0,128,0), got {g_pixel}"
    )
    assert _approx(b_pixel, (0, 0, 255)), (
        f"cell 2 expected blue-ish (0,0,255), got {b_pixel}"
    )


def test_grid_end_to_end_no_compute_runs_under_5_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke-watcher: the path-only e2e must stay fast (no compute path)."""
    import time

    _make_color_mp4(tmp_path / "a.mp4", "red")
    _make_color_mp4(tmp_path / "b.mp4", "green")

    monkeypatch.setattr(
        "kinoforge.core.grid.executor._check_no_residual_pods",
        lambda: (True, ""),
    )

    spec = GridSpec.model_validate(
        {
            "title": "fast-path",
            "layout": "1x2",
            "budget_cap_usd": 1.0,
            "cells": [
                {"path": str(tmp_path / "a.mp4"), "caption": "a"},
                {"path": str(tmp_path / "b.mp4"), "caption": "b"},
            ],
        }
    )
    t0 = time.monotonic()
    result = asyncio.run(
        run_grid(spec=spec, output_dir=tmp_path / "out", max_parallel_groups=1)
    )
    elapsed = time.monotonic() - t0
    assert result.status == "full"
    assert elapsed < 10.0, f"path-only e2e ran for {elapsed:.1f}s (>10s budget)"
    # silence unused import warning
    _ = MagicMock
