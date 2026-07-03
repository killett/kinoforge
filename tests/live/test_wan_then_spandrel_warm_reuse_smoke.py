"""Live smoke — Wan T2V → spandrel multi-stage upscale on the same pod (Task 14).

RED scaffold; GREEN evidence lands in Task 16. Single pod runs both
stages — LRU model registry in wan_t2v_server handles VRAM swap between
Wan + spandrel weights.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)

_PROMPT = (
    (
        Path(__file__).parent.parent.parent
        / "examples"
        / "configs"
        / "prompts"
        / "field-realistic.txt"
    )
    .read_text()
    .strip()
)
_EVIDENCE_DIR = (
    Path(__file__).parent / "evidence" / "2026-06-29-wan-then-spandrel-warm-reuse"
)
_CFG = (
    Path(__file__).parent.parent.parent
    / "examples"
    / "configs"
    / "wan-with-upscale-spandrel.yaml"
)


@pytest.mark.live
def test_wan_t2v_then_spandrel_x2_multi_stage() -> None:
    assert _CFG.exists(), f"cfg missing: {_CFG}"
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = _EVIDENCE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(  # noqa: S603,S607
        [
            "pixi",
            "run",
            "kinoforge",
            "generate",
            "--config",
            str(_CFG),
            "--prompt",
            _PROMPT,
            "--mode",
            "t2v",
            "--no-reuse",
            "--output-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        timeout=5400,  # 90 min ceiling
        check=False,
    )
    (_EVIDENCE_DIR / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / "stderr.txt").write_text(proc.stderr)
    assert proc.returncode == 0, proc.stderr

    out_files = sorted(out_dir.rglob("*.mp4"))
    assert out_files, "no output mp4 produced"

    # Final artifact is the upscaled one; Wan T2V cfg renders 480x480,
    # spandrel 2x → 960x960.
    final = out_files[-1]
    w, h = _probe_dims(final)
    assert (w, h) == (960, 960), f"expected 960x960, got {w}x{h}"

    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "No running instances." in ledger.stdout, (
        f"pod survived --no-reuse:\n{ledger.stdout}"
    )
    assert "No instances recorded in ledger." in ledger.stdout, (
        f"ledger entry survived --no-reuse:\n{ledger.stdout}"
    )


def _probe_dims(path: Path) -> tuple[int, int]:
    out = subprocess.check_output(  # noqa: S603,S607
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(path),
        ],
        text=True,
    ).strip()
    w, h = out.split("x")
    return int(w), int(h)
