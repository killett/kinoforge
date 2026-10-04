"""Live smoke — spandrel RealESRGAN-x2 upscale of a still image (`upscale --image`).

RED scaffold committed BEFORE the live spend per CLAUDE.md. Input is the §35
`kinoforge image` PNG, so this run is also the first image -> upscale chain.
Evidence lands under ``tests/live/evidence/2026-10-03-spandrel-image-upscale/``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)

_ROOT = Path(__file__).parent.parent.parent
_INPUT = (
    _ROOT
    / "output"
    / "20261003-174427_image_luma_agents_uni-1_Photorealistic-cinem.png"
)
_CFG = _ROOT / "examples" / "configs" / "runpod-diffusers-spandrel-x2-upscale.yaml"
_EVIDENCE_DIR = Path(__file__).parent / "evidence" / "2026-10-03-spandrel-image-upscale"


def _dims(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as im:
        return im.size


@pytest.mark.live
def test_spandrel_upscales_a_still_image_2x() -> None:
    assert _INPUT.exists(), f"input PNG missing: {_INPUT}"
    assert _CFG.exists(), f"cfg missing: {_CFG}"
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(  # noqa: S603,S607
        [
            "pixi",
            "run",
            "kinoforge",
            "upscale",
            "--image",
            str(_INPUT),
            "--config",
            str(_CFG),
            "--no-reuse",
        ],
        capture_output=True,
        text=True,
        timeout=2400,
        check=False,
    )
    (_EVIDENCE_DIR / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / "stderr.txt").write_text(proc.stderr)
    assert proc.returncode == 0, proc.stderr

    import re

    m = re.search(r"upscaled: uri='([^']+)'", proc.stdout)
    assert m, f"no upscaled uri line in stdout:\n{proc.stdout}"
    out_path = Path(m.group(1).removeprefix("file://"))
    assert out_path.exists(), out_path
    assert out_path.suffix == ".png", out_path
    assert "_upscaled_spandrel_" in out_path.name, out_path

    evidence = _EVIDENCE_DIR / out_path.name
    shutil.copy2(out_path, evidence)

    in_w, in_h = _dims(_INPUT)
    out_w, out_h = _dims(evidence)
    assert (out_w, out_h) == (in_w * 2, in_h * 2), (
        f"expected {in_w * 2}x{in_h * 2}, got {out_w}x{out_h}"
    )
    assert (
        hashlib.sha256(evidence.read_bytes()).hexdigest()
        != hashlib.sha256(_INPUT.read_bytes()).hexdigest()
    )

    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "No running instances." in ledger.stdout, ledger.stdout
    assert "No instances recorded in ledger." in ledger.stdout, ledger.stdout
