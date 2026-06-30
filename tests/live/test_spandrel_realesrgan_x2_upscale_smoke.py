"""Live smoke — spandrel RealESRGAN-x2 2x upscale of a known low-res clip (Task 13).

RED scaffold per CLAUDE.md durability rule: scaffold lands BEFORE the
live spend. Task 15 removes the xfail mark + lands GREEN evidence
under ``tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

_FIXTURE = (
    Path(__file__).parent.parent.parent
    / "examples"
    / "configs"
    / "grids"
    / "_fixtures"
    / "wan21_prompt_cell0.mp4"
)
_EVIDENCE_DIR = (
    Path(__file__).parent / "evidence" / "2026-06-29-spandrel-realesrgan-x2-upscale"
)
_CFG = (
    Path(__file__).parent.parent.parent
    / "examples"
    / "configs"
    / "upscale-spandrel-x2.yaml"
)


@pytest.mark.live
@pytest.mark.xfail(
    reason="RED scaffold (Task 13) — GREEN evidence lands in Task 15 after live spend",
    strict=False,
)
def test_spandrel_realesrgan_x2_upscales_2x() -> None:
    assert _FIXTURE.exists(), f"input fixture missing: {_FIXTURE}"
    assert _CFG.exists(), f"cfg missing: {_CFG}"

    out_dir = _EVIDENCE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(  # noqa: S603,S607
        [
            "pixi",
            "run",
            "kinoforge",
            "upscale",
            "--video",
            str(_FIXTURE),
            "--config",
            str(_CFG),
            "--no-reuse",
            "--output-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    (_EVIDENCE_DIR / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / "stderr.txt").write_text(proc.stderr)
    assert proc.returncode == 0, proc.stderr

    in_w, in_h = _probe_dims(_FIXTURE)
    out_files = sorted(out_dir.rglob("*.mp4"))
    assert out_files, "no output mp4 produced"
    out_w, out_h = _probe_dims(out_files[-1])
    assert (out_w, out_h) == (in_w * 2, in_h * 2), (
        f"expected {in_w * 2}x{in_h * 2}, got {out_w}x{out_h}"
    )

    in_sha = _first_frame_sha256(_FIXTURE)
    out_sha = _first_frame_sha256(out_files[-1])
    assert in_sha != out_sha, "output identical to input — no upscale work"

    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    pods = json.loads(ledger.stdout).get("instances", [])
    assert pods == [], f"pod survived --no-reuse: {pods}"


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


def _first_frame_sha256(path: Path) -> str:
    out = subprocess.check_output(  # noqa: S603,S607
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            "select=eq(n\\,0)",
            "-vsync",
            "vfr",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
    )
    return hashlib.sha256(out).hexdigest()
