"""Opt-in live tests against the real fal.ai queue API (Layer I Task 13).

Gated by two env vars:
- KINOFORGE_LIVE_TESTS=1
- FAL_KEY=<real fal.ai key>

Cost: ~$0.05-$0.20 per run, depending on the model.  Skipped silently in CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

if not (os.getenv("KINOFORGE_LIVE_TESTS") == "1" and os.getenv("FAL_KEY")):
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + FAL_KEY",
        allow_module_level=True,
    )


_CONFIG = "examples/configs/fal-t2v.yaml"


def _run_cli(
    args: list[str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `python -m kinoforge` with the given args, capturing output."""
    return subprocess.run(
        [sys.executable, "-m", "kinoforge", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def test_fal_provision_real(tmp_path: Path) -> None:
    """`kinoforge provision -c fal-t2v.yaml` succeeds against real fal.ai."""
    result = _run_cli(["--state-dir", str(tmp_path), "provision", "--config", _CONFIG])
    assert result.returncode == 0, (
        f"provision failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_fal_generate_short_t2v_real(tmp_path: Path) -> None:
    """`kinoforge generate -c fal-t2v.yaml` produces a real MP4 artifact."""
    result = _run_cli(
        [
            "--state-dir",
            str(tmp_path),
            "generate",
            "--config",
            _CONFIG,
            "--prompt",
            "a cat sitting on a fence",
            "--mode",
            "t2v",
            "--run-id",
            "live-smoke",
        ]
    )
    assert result.returncode == 0, (
        f"generate failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    candidates = list(tmp_path.rglob("*.mp4"))
    assert candidates, f"no .mp4 found under {tmp_path}; cli output:\n{result.stdout}"
    f = candidates[0]
    raw = f.read_bytes()
    # ISO BMFF "ftyp" box at offset 4 - robust MP4 magic-bytes check.
    assert raw[4:8] == b"ftyp", f"file {f} is not an MP4 (bytes 4-8 = {raw[4:8]!r})"
