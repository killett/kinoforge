"""Live smoke — SeedVR2 3B FP8 2x upscale of a known low-res clip (T17).

RED scaffold per the CLAUDE.md durability rule: any scaffold whose
purpose is to drive live cloud spend MUST be committed (RED is fine —
xfail) BEFORE the spend is invoked. T18 removes the ``xfail`` mark,
runs the smoke against live RunPod, and lands evidence + GREEN.

Pre-flight (T18 will run, not T17):
  - replace `_UPSTREAM_COMMIT = "PLACEHOLDER_REPLACE_BEFORE_LIVE_SPEND"`
    in src/kinoforge/upscalers/seedvr2/__init__.py with a real
    ByteDance-Seed/SeedVR commit SHA (resume gotcha #1 in PROGRESS.md).
  - `pixi run preflight` exits 0.
  - `--no-reuse` so the pod auto-destroys; verify `kinoforge list`
    reports empty AFTER orchestrator exits (NOT mid-run).

Polling cadence per CLAUDE.md ``Live smoke monitoring``: every 60-90s
the runtime probe queries GPU util / CPU / mem / costPerHr; bail
early on 3 consecutive 0% GPU probes (model-load hung, OOM, etc.).
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
_EVIDENCE_DIR = Path(__file__).parent / "evidence" / "2026-06-28-seedvr2-3b-fp8-upscale"
_CFG = (
    Path(__file__).parent.parent.parent
    / "examples"
    / "configs"
    / "upscale-seedvr2-3b.yaml"
)


@pytest.mark.live
@pytest.mark.xfail(
    reason="RED scaffold (T17) — GREEN evidence lands in T18 after live spend",
    strict=False,
)
def test_seedvr2_3b_fp8_upscales_2x() -> None:
    """End-to-end live smoke: upscale a 189 KB clip 2x via SeedVR2 3B FP8."""
    assert _FIXTURE.exists(), f"input fixture missing: {_FIXTURE}"
    assert _CFG.exists(), f"cfg missing: {_CFG}"

    out_dir = _EVIDENCE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(  # noqa: S603,S607 — known kinoforge CLI invocation
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
        timeout=2400,  # 40 min ceiling; SeedVR2 3B FP8 on A100 fits in ~5-10 min
    )
    (_EVIDENCE_DIR / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / "stderr.txt").write_text(proc.stderr)
    assert proc.returncode == 0, (
        f"kinoforge upscale exit {proc.returncode}\n{proc.stderr}"
    )

    # Resolution check via ffprobe — 2x factor exact.
    in_w, in_h = _probe_resolution(_FIXTURE)
    out_files = sorted(out_dir.rglob("*.mp4"))
    assert out_files, "no output mp4 produced"
    out_w, out_h = _probe_resolution(out_files[-1])
    assert (out_w, out_h) == (in_w * 2, in_h * 2), (
        f"expected {in_w * 2}x{in_h * 2}, got {out_w}x{out_h}"
    )

    # Frame-level diff — sha256 of first frame as PNG must differ.
    in_sha = _first_frame_sha256(_FIXTURE)
    out_sha = _first_frame_sha256(out_files[-1])
    assert in_sha != out_sha, "output frame identical to input — no upscale work"

    # Ledger empty post-exit — per --no-reuse + use_no_reuse memory.
    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    pods = json.loads(ledger.stdout).get("instances", [])
    assert pods == [], f"pod survived --no-reuse: {pods}"


def _probe_resolution(path: Path) -> tuple[int, int]:
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
