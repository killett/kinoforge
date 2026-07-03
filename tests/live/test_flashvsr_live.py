"""FlashVSR live-smoke matrix — RED xfail-gated pre-spend scaffold.

Committed BEFORE any live spend per CLAUDE.md durability rule. Each test
un-xfailed atomically in the same commit as its evidence file.

Runs `pixi run kinoforge <subcmd>` end-to-end against RunPod. Reads the
standard prompt from ``examples/configs/prompts/field-realistic.txt``
(memory: ``feedback_standard_test_prompt``). Cost budget guardrails:

  - test_f_single : ~$0.05 (ceiling $0.15)
  - test_f_multi  : ~$0.55 (ceiling $1.00) — HARD STOP if the combined
                    F-multi+F-warm spend exceeds $1.20 per plan §7.
  - test_f_warm   : ~$0.10 (shares pod with F-multi)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Live-spend gate. Without this env var, every test in this module short-
# circuits with `pytest.skip` BEFORE any `kinoforge` subprocess fires, so
# CI + regression runs stay free of accidental pod spend. Each live-spend
# task (T8, T9) exports `KINOFORGE_LIVE_SPEND=1` in the same shell it
# invokes pytest from and un-xfails only its own test(s).
_LIVE_SPEND_ENV = "KINOFORGE_LIVE_SPEND"


def _require_live_spend_env() -> None:
    if os.environ.get(_LIVE_SPEND_ENV) != "1":
        pytest.skip(
            f"live-spend gate: set {_LIVE_SPEND_ENV}=1 to fire "
            "real kinoforge pods (T8 / T9)"
        )


_STANDARD_PROMPT_PATH = Path("/workspace/examples/configs/prompts/field-realistic.txt")

_MULTI_CFG = "examples/configs/wan-with-upscale-flashvsr.yaml"
_UPSCALE_ONLY_CFG = "examples/configs/upscale-flashvsr-x4.yaml"

# 480x480 Wan 2.2 clip generated locally on 2026-06-30 as the T8 fixture.
# Small enough (~800 KB) to stream over the pod's PUT /upload path in
# under a second; large enough that ffprobe reports a real 2x dim jump.
_F_SINGLE_SOURCE = Path(
    "/workspace/output/"
    "20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4"
)


def _ffprobe_dims(video: Path) -> tuple[int, int]:
    """Return (width, height) via ``pixi run ffprobe``.

    System ffprobe is not on the container PATH; the pixi env ships it via
    the imageio[ffmpeg] dep + a shim binary.
    """
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
    """Return True iff ``kinoforge list`` reports zero pods AND empty ledger."""
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


def test_f_single(tmp_path: Path) -> None:
    """F-single: standalone kinoforge upscale on a pre-existing Wan clip.

    Bug caught: file:// source path unreachable from pod without the
    PUT /upload seam (P3 T15 blocker regression). Also asserts pod is
    destroyed post-run (--no-reuse contract).
    """
    _require_live_spend_env()
    assert _F_SINGLE_SOURCE.exists(), f"missing fixture Wan clip {_F_SINGLE_SOURCE}"

    r = subprocess.run(  # noqa: S603
        [
            "pixi",
            "run",
            "kinoforge",
            "upscale",
            "--config",
            _UPSCALE_ONLY_CFG,
            "--video",
            str(_F_SINGLE_SOURCE),
            "--no-reuse",
        ],
        capture_output=True,
        text=True,
        check=True,
        # 15m: BSA wheel curl+install ~60s (was 25-30min source compile
        # pre-T7.5); FlashVSR install ~2min; weights fetch ~3min;
        # upscale ~2min. ~8min happy path with 2x cushion.
        timeout=15 * 60,
    )
    assert "flashvsr-wan21-bfloat16" in r.stdout
    outs = sorted(Path("/workspace/output").glob("*_upscaled_flashvsr_*.mp4"))
    assert outs, "no upscaled artifact sunk"
    src_dims = _ffprobe_dims(_F_SINGLE_SOURCE)
    out_dims = _ffprobe_dims(outs[-1])
    assert out_dims == (src_dims[0] * 4, src_dims[1] * 4), (
        f"expected 4x dims got {out_dims} vs src {src_dims}"
    )
    assert _kinoforge_list_shows_no_pods(), "pod not destroyed post-run"


def test_f_multi(tmp_path: Path) -> None:
    """F-multi: Wan generate → FlashVSR upscale on the same pod.

    Bug caught: DiffusersEngine.render_provision omits upscaler
    composition (P2 T8 seam regression) → pod boots without FlashVSR
    weights or BSA kernel, first /upscale returns 500.
    """
    _require_live_spend_env()
    prompt = _STANDARD_PROMPT_PATH.read_text().strip()
    r = subprocess.run(  # noqa: S603
        [
            "pixi",
            "run",
            "kinoforge",
            "generate",
            "--config",
            _MULTI_CFG,
            "--prompt",
            prompt,
        ],
        capture_output=True,
        text=True,
        check=True,
        # 25m: F-single boot budget (~8min) + Wan 2.2 A14B download
        # (~15-20min). BSA source compile gone post-T7.5.
        timeout=25 * 60,
    )
    assert "wan-T2V-done" in r.stdout or "diffusers" in r.stdout
    assert "flashvsr-wan21-bfloat16" in r.stdout
    # Two MP4s expected — Wan raw + FlashVSR upscaled sibling.
    wans = sorted(Path("/workspace/output").glob("*_diffusers_Wan2.2-*.mp4"))
    ups = sorted(Path("/workspace/output").glob("*_upscaled_flashvsr_*.mp4"))
    assert wans and ups, "missing wan or upscaled artifact"


def test_f_warm(tmp_path: Path) -> None:
    """F-warm: second kinoforge generate with warm-reuse; no BSA reinstall.

    Bug caught: warm-reuse tears down the Python env or re-runs the
    provision script → BSA wheel curl+install fires twice (a ~60s tax
    that would mask a genuine LRU-hit regression).
    """
    _require_live_spend_env()
    prompt = _STANDARD_PROMPT_PATH.read_text().strip() + " variant B"
    r = subprocess.run(  # noqa: S603
        [
            "pixi",
            "run",
            "kinoforge",
            "generate",
            "--config",
            _MULTI_CFG,
            "--prompt",
            prompt,
            "--no-reuse",
        ],
        capture_output=True,
        text=True,
        check=True,
        # 15m: warm reuse skips all install steps. Only Wan T2V inference
        # + FlashVSR upscale + sink.
        timeout=15 * 60,
    )
    # LRU hit on the same pod that F-multi warmed; no cold model load.
    assert "LRU hit" in r.stdout or "warm reuse" in r.stdout
    assert "compiling block_sparse_attention" not in r.stdout.lower()
    assert _kinoforge_list_shows_no_pods(), "pod not destroyed after --no-reuse"
