"""RIFE interpolate-only live smoke — fixture-sourced 480² clip -> 60fps.

Committed BEFORE any live spend per CLAUDE.md durability rules. Gated on
``KINOFORGE_LIVE_SPEND=1`` (same gate as the other live smokes).

Validates the standalone interpolate path end-to-end WITHOUT Wan: a pre-generated
480² fixture clip (16fps) is fed to ``kinoforge interpolate --fps 60``. RIFE is
arbitrary-timestep, so the runtime synthesizes intermediate frames to land
exactly on 60fps. Delivered artifact must probe at 60fps.

Interpolate-only (no 70 GB Wan download): ~4-6 min, a short pod-death window.
One-shot ``--no-reuse`` so the pod auto-destroys.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_LIVE_SPEND_ENV = "KINOFORGE_LIVE_SPEND"
_CFG = "examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml"
# 480x480 / 81-frame 16fps Wan clip generated 2026-06-30 (F-single fixture).
_SOURCE = Path(
    "/workspace/output/"
    "20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4"
)


def _require_live_spend_env() -> None:
    if os.environ.get(_LIVE_SPEND_ENV) != "1":
        pytest.skip(f"live-spend gate: set {_LIVE_SPEND_ENV}=1 to fire a real pod")


def _output_dir() -> Path:
    """Artifact sink — kinoforge writes under CWD/output/."""
    return Path.cwd() / "output"


def _ffprobe_fps(video: Path) -> float:
    r = subprocess.run(  # noqa: S603
        [
            "pixi",
            "run",
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=r_frame_rate",
            "-of",
            "csv=p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    num, den = r.stdout.strip().split("/")
    return float(num) / float(den)


def _kinoforge_list_shows_no_pods() -> bool:
    r = subprocess.run(  # noqa: S603
        ["pixi", "run", "kinoforge", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        "No running instances." in r.stdout
        and "No instances recorded in ledger." in r.stdout
    )


def _destroy_ledger_pods() -> None:
    """Best-effort sweep so an assert failure never leaks a running pod."""
    r = subprocess.run(  # noqa: S603
        ["pixi", "run", "kinoforge", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if "provider=" in line:
            pod_id = line.split()[0]
            subprocess.run(  # noqa: S603
                ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
                capture_output=True,
                text=True,
                check=False,
                timeout=5 * 60,
            )


def test_rife_interpolate_delivers_60fps(tmp_path: Path) -> None:
    """Interpolate a 16fps 480² fixture to 60fps on a live RIFE pod.

    Bug caught: the standalone interpolate path failing end-to-end — the pod
    never installing RIFE, the runtime not synthesizing to the target rate, or
    the interpolated artifact not materializing at the orchestrator boundary.
    Delivered clip must probe at 60fps.
    """
    _require_live_spend_env()
    assert _SOURCE.exists(), f"missing fixture clip {_SOURCE}"
    before = (
        {p for p in _output_dir().glob("*.mp4")} if _output_dir().is_dir() else set()
    )
    try:
        r = subprocess.run(  # noqa: S603
            [
                "pixi",
                "run",
                "kinoforge",
                "interpolate",
                "--config",
                _CFG,
                "--video",
                str(_SOURCE),
                "--fps",
                "60",
                "--no-reuse",
            ],
            capture_output=True,
            text=True,
            check=False,
            # 12m: RIFE install ~1min + weights ~1min + interp ~1min + sink.
            # ~3x cushion for cold-boot variance.
            timeout=12 * 60,
        )
        assert r.returncode == 0, (
            f"exit={r.returncode}\n--- stdout tail ---\n{r.stdout[-3000:]}\n"
            f"--- stderr tail ---\n{r.stderr[-2000:]}"
        )
        combined = r.stdout + r.stderr
        assert "interpolated: uri=" in combined, "no interpolated artifact sunk"
        new = ({p for p in _output_dir().glob("*.mp4")}) - before
        interp = sorted(p for p in new if "interp" in p.name.lower())
        assert interp, (
            f"no fresh interpolated artifact among {sorted(p.name for p in new)}"
        )
        fps = _ffprobe_fps(interp[-1])
        assert round(fps) == 60, f"expected 60fps deliverable, got {fps}"
    except BaseException:
        _destroy_ledger_pods()
        raise
    assert _kinoforge_list_shows_no_pods(), "pod not destroyed post-run (--no-reuse)"
