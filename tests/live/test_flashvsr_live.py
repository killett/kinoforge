"""FlashVSR live-smoke matrix — RED xfail-gated pre-spend scaffold.

Committed BEFORE any live spend per CLAUDE.md durability rule. Each test
un-xfailed atomically in the same commit as its evidence file.

Runs `pixi run kinoforge <subcmd>` end-to-end against RunPod. Reads the
standard prompt from ``examples/configs/prompts/field-realistic.txt``
(memory: ``feedback_standard_test_prompt``). Cost budget guardrails:

  - test_f_single : ~$0.05 (ceiling $0.15)
  - test_f_multi  : ~$0.90 (ceiling $1.20) — A100 80GB @ ~$1.90/hr with
                    a 15-20 min Wan 2.2 A14B download dominates. HARD
                    STOP + investigate if F-multi alone exceeds $1.20.
  - test_f_warm   : ~$0.30 (attaches to F-multi's warm pod; inference
                    + upscale only, no provision)
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
    # kinoforge writes outputs under CWD/output/ (worktree-local) not
    # /workspace/output/ (the shared source dir).
    outs = sorted((Path.cwd() / "output").glob("*_upscaled_flashvsr_*.mp4"))
    assert outs, "no upscaled artifact sunk"
    src_dims = _ffprobe_dims(_F_SINGLE_SOURCE)
    out_dims = _ffprobe_dims(outs[-1])
    assert out_dims == (src_dims[0] * 4, src_dims[1] * 4), (
        f"expected 4x dims got {out_dims} vs src {src_dims}"
    )
    assert _kinoforge_list_shows_no_pods(), "pod not destroyed post-run"


def _output_dir() -> Path:
    """Artifact sink — kinoforge writes under CWD/output/, not /workspace."""
    return Path.cwd() / "output"


def _snapshot_mp4s() -> set[Path]:
    """Return the current set of MP4s in the artifact sink."""
    d = _output_dir()
    return set(d.glob("*.mp4")) if d.is_dir() else set()


def _destroy_ledger_pods() -> None:
    """Best-effort sweep: destroy every pod the ledger still records.

    Keeps a failed live smoke from bleeding pod-hours — assert failures
    in F-multi/F-warm must never leave the A100 running (2026-06-22
    t73xw2apqnfk4q leak, ~$0.40).
    """
    r = subprocess.run(  # noqa: S603
        ["pixi", "run", "kinoforge", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in r.stdout.splitlines():
        line = line.strip()
        if "provider=" in line:
            pod_id = line.split()[0]
            subprocess.run(  # noqa: S603
                ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
                capture_output=True,
                text=True,
                check=False,
                timeout=5 * 60,
            )


def test_f_multi(tmp_path: Path) -> None:
    """F-multi: Wan generate → FlashVSR upscale on the same pod.

    Bug caught: DiffusersEngine.render_provision omits upscaler
    composition (P2 T8 seam regression) → pod boots without FlashVSR
    weights or BSA kernel, first /upscale returns 500. Also catches the
    torch-ABI co-residency break: Wan 2.2 needs torch>=2.6, BSA needs
    the wheel's exact link target (bsa-cu124-torch2.6-v1).

    Deliberately leaves the pod warm (no --no-reuse) — test_f_warm
    attaches to it next. On failure, sweeps the pod before re-raising.
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
                _MULTI_CFG,
                "--prompt",
                prompt,
                "--mode",
                "t2v",
            ],
            capture_output=True,
            text=True,
            check=True,
            # 40m: image pull + provision (~8m) + Wan 2.2 A14B download
            # (~15-20m) + T2V inference + FlashVSR upscale + sink.
            timeout=40 * 60,
        )
        assert "flashvsr-wan21-bfloat16" in r.stdout
        new = _snapshot_mp4s() - before
        wans = sorted(p for p in new if "_diffusers_Wan2.2-" in p.name)
        ups = sorted(p for p in new if "_upscaled_flashvsr_" in p.name)
        assert wans, f"no fresh Wan artifact among {sorted(p.name for p in new)}"
        assert ups, f"no fresh upscaled artifact among {sorted(p.name for p in new)}"
        wan_dims = _ffprobe_dims(wans[-1])
        up_dims = _ffprobe_dims(ups[-1])
        assert up_dims == (wan_dims[0] * 4, wan_dims[1] * 4), (
            f"expected 4x dims got {up_dims} vs wan {wan_dims}"
        )
    except BaseException:
        _destroy_ledger_pods()
        raise


def test_f_warm(tmp_path: Path) -> None:
    """F-warm: second kinoforge generate attaches to F-multi's warm pod.

    Bug caught: warm-reuse scan fails to match the multi-stage
    capability key → cold-creates a second pod, silently doubling spend
    and re-paying the ~25 min provision tax.

    NOTE: no --no-reuse here — that flag SKIPS the warm scan and
    cold-creates (cli/_commands.py:534), which would defeat the test.
    Teardown is explicit `kinoforge destroy` in the finally block.
    """
    _require_live_spend_env()
    prompt = _STANDARD_PROMPT_PATH.read_text().strip() + " variant B"
    before = _snapshot_mp4s()
    try:
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
                "--mode",
                "t2v",
            ],
            capture_output=True,
            text=True,
            check=True,
            # 20m: warm attach skips image pull + provision + downloads.
            # Wan T2V inference + FlashVSR upscale + sink only.
            timeout=20 * 60,
        )
        # Warm hit on the pod F-multi left running; a cold create here
        # means the capability-key match regressed.
        assert "warm-reuse: attached to" in r.stdout, (
            "no warm attach — cold create burned a second pod"
        )
        assert "cold create" not in r.stdout
        new = _snapshot_mp4s() - before
        assert any("_upscaled_flashvsr_" in p.name for p in new), (
            "no fresh upscaled artifact from warm run"
        )
    finally:
        _destroy_ledger_pods()
    assert _kinoforge_list_shows_no_pods(), "pod still alive after teardown"
