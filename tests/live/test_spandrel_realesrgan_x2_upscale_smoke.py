"""Live smoke — spandrel RealESRGAN-x2 2x upscale of a known low-res clip (Task 13).

RED scaffold per CLAUDE.md durability rule: scaffold lands BEFORE the
live spend. Task 15 removes the xfail mark + lands GREEN evidence
under ``tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)

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
def test_spandrel_realesrgan_x2_upscales_2x() -> None:
    assert _FIXTURE.exists(), f"input fixture missing: {_FIXTURE}"
    assert _CFG.exists(), f"cfg missing: {_CFG}"
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

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
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    (_EVIDENCE_DIR / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / "stderr.txt").write_text(proc.stderr)
    assert proc.returncode == 0, proc.stderr

    # Parse the upscaled artifact URI from stdout ("upscaled: uri='...'")
    import re as _re

    m = _re.search(r"upscaled: uri='([^']+)'", proc.stdout)
    assert m, f"no upscaled uri line in stdout:\n{proc.stdout}"
    uri = m.group(1)
    assert uri.startswith("file://"), f"expected file:// uri, got {uri!r}"
    out_path = Path(uri.removeprefix("file://"))
    assert out_path.exists(), (
        f"orchestrator-reported output missing on disk: {out_path}"
    )

    # Copy the output mp4 into the evidence dir for inspection without re-spend.
    import shutil as _shutil

    evidence_mp4 = _EVIDENCE_DIR / out_path.name
    _shutil.copy2(out_path, evidence_mp4)

    in_w, in_h = _probe_dims(_FIXTURE)
    out_w, out_h = _probe_dims(evidence_mp4)
    assert (out_w, out_h) == (in_w * 2, in_h * 2), (
        f"expected {in_w * 2}x{in_h * 2}, got {out_w}x{out_h}"
    )

    in_sha = _first_frame_sha256(_FIXTURE)
    out_sha = _first_frame_sha256(evidence_mp4)
    assert in_sha != out_sha, "output identical to input — no upscale work"

    # Pod-cleanup verification per CLAUDE.md durability rule.
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
