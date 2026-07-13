"""FlashVSR height-target (1080p) live smoke — upscale-only, fixture-sourced.

Committed BEFORE any live spend per CLAUDE.md. Gated on
``KINOFORGE_LIVE_SPEND=1`` (same gate as ``test_flashvsr_live.py``).

Validates the height-target feature end-to-end WITHOUT Wan: a pre-generated 480²
fixture clip is fed to ``kinoforge upscale`` with ``upscale.scale: 1080p``.
FlashVSR is 4x-native, so UpscaleStage resolves 1080p → 4x (480→1920) and the
orchestrator materialize boundary lanczos-downscales 1920→1080. Delivered
artifact must be 1080×1080 (square, aspect preserved), NOT 1920×1920.

Upscale-only (no 70 GB Wan A14B download): ~8-10 min, ~$0.15 — far cheaper and a
much shorter pod-death window than the render+upscale multi-stage path. One-shot
``--no-reuse`` so the pod auto-destroys.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_LIVE_SPEND_ENV = "KINOFORGE_LIVE_SPEND"
_CFG = "examples/configs/runpod-diffusers-flashvsr-1080p-upscale.yaml"
# 480x480 / 81-frame Wan clip generated 2026-06-30 (also the F-single fixture).
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


def _ffprobe_dims(video: Path) -> tuple[int, int]:
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
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    w, h = r.stdout.strip().split(",")
    return int(w), int(h)


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


def test_flashvsr_1080p_delivers_1080_square(tmp_path: Path) -> None:
    """Upscale a 480² fixture with scale=1080p -> 1080×1080 deliverable.

    Bug caught: the height target failing to resolve to 4x+downscale end-to-end
    — delivering the raw 1920×1920 (downscale never fired) or erroring at the
    cfg / stage / materialize / ffmpeg boundaries.
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
                "upscale",
                "--config",
                _CFG,
                "--video",
                str(_SOURCE),
                "--no-reuse",
            ],
            capture_output=True,
            text=True,
            check=False,
            # 15m: BSA wheel install ~60s + FlashVSR install ~2min + weights
            # ~3min + 4x upscale ~3min + downscale + sink. ~2x cushion.
            timeout=15 * 60,
        )
        assert r.returncode == 0, (
            f"exit={r.returncode}\n--- stdout tail ---\n{r.stdout[-3000:]}\n"
            f"--- stderr tail ---\n{r.stderr[-2000:]}"
        )
        combined = r.stdout + r.stderr
        assert "_upscaled_flashvsr_" in combined, "no upscaled artifact sunk"
        # Materialize boundary logs the downscale — proves the seam fired.
        assert "downscaling upscaled artifact to 1080p" in combined, (
            "materialize-boundary downscale did not fire"
        )
        new = ({p for p in _output_dir().glob("*.mp4")}) - before
        ups = sorted(p for p in new if "_upscaled_flashvsr_" in p.name)
        assert ups, f"no fresh upscaled artifact among {sorted(p.name for p in new)}"
        out_dims = _ffprobe_dims(ups[-1])
        assert out_dims == (1080, 1080), (
            f"expected 1080x1080 height-target deliverable, got {out_dims}"
        )
    except BaseException:
        _destroy_ledger_pods()
        raise
    assert _kinoforge_list_shows_no_pods(), "pod not destroyed post-run (--no-reuse)"
