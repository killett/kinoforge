"""FlashVSR height-target (1080p) live smoke — RED gate pre-spend scaffold.

Committed BEFORE any live spend per CLAUDE.md durability rule. Gated on
``KINOFORGE_LIVE_SPEND=1`` (same gate as ``test_flashvsr_live.py``) so CI /
regression runs never fire a pod.

Proves the height-target feature end-to-end: ``upscale.scale: 1080p`` on a
480x480 Wan render resolves to FlashVSR's native 4x (480 -> 1920) and then
downscales 1920 -> 1080 at the orchestrator materialize boundary. The delivered
artifact must be 1080x1080 (square aspect preserved), NOT 1920x1920.

Cost: ~$0.90 (ceiling $1.20) — A100 80GB @ ~$1.90/hr dominated by the Wan 2.2
A14B download. One-shot with ``--no-reuse`` so the pod auto-destroys.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_LIVE_SPEND_ENV = "KINOFORGE_LIVE_SPEND"
_STANDARD_PROMPT_PATH = Path("/workspace/examples/configs/prompts/field-realistic.txt")
_CFG = "examples/configs/wan-with-upscale-flashvsr-1080p.yaml"


def _require_live_spend_env() -> None:
    if os.environ.get(_LIVE_SPEND_ENV) != "1":
        pytest.skip(f"live-spend gate: set {_LIVE_SPEND_ENV}=1 to fire a real pod")


def _output_dir() -> Path:
    """Artifact sink — kinoforge writes under CWD/output/, not /workspace."""
    return Path.cwd() / "output"


def _snapshot_mp4s() -> set[Path]:
    d = _output_dir()
    return set(d.glob("*.mp4")) if d.is_dir() else set()


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
    """Best-effort sweep so an assert failure never leaks a running A100."""
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
    """Height-target 1080p on a 480 source -> 1080x1080 deliverable.

    Bug caught: the height target failing to resolve to 4x+downscale
    end-to-end — either delivering the raw 1920x1920 (downscale never fired)
    or erroring at cfg/stage/materialize boundaries.
    """
    _require_live_spend_env()
    prompt = _STANDARD_PROMPT_PATH.read_text().strip()
    before = _snapshot_mp4s()
    try:
        r = subprocess.run(  # noqa: S603
            [
                "pixi",
                "run",
                "kinoforge",
                "generate",
                "--config",
                _CFG,
                "--prompt",
                prompt,
                "--mode",
                "t2v",
                "--no-reuse",
            ],
            capture_output=True,
            text=True,
            check=False,
            # 40m: image pull + provision + Wan 2.2 A14B download (~15-20m)
            # + T2V inference + FlashVSR 4x upscale + downscale + sink.
            timeout=40 * 60,
        )
        assert r.returncode == 0, (
            f"exit={r.returncode}\n--- stdout tail ---\n{r.stdout[-3000:]}\n"
            f"--- stderr tail ---\n{r.stderr[-2000:]}"
        )
        combined = r.stdout + r.stderr
        assert "_upscaled_flashvsr_" in combined, "no upscaled artifact sunk"
        # The materialize boundary logs the downscale — proves the seam fired.
        assert "downscaling upscaled artifact to 1080p" in combined, (
            "materialize-boundary downscale did not fire"
        )
        new = _snapshot_mp4s() - before
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
