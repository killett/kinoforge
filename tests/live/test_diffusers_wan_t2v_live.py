"""Live smoke for Wan 2.2 T2V-A14B via DiffusersEngine.

Gated by KINOFORGE_LIVE_TESTS=1. Runs three real ``kinoforge generate``
invocations against RunPod:

  1. Cold boot on the new diffusers cfg with field-realistic.txt.
  2. Warm reuse on the same cfg with field-dreamlike.txt.
  3. Cold boot on the existing 5B Kijai cfg with field-realistic.txt,
     while the 14B pod is still alive — proves capability-key
     isolation forces a new pod.

Pass criteria:
  - All three invocations exit 0.
  - Two distinct MP4s land in /workspace/output for the 14B cfg.
  - 14B warm-leg log contains "warm-reuse: attached to <pod_id>".
  - 5B leg's pod_id differs from the 14B pod_id.
  - Both 14B output sha256s differ (distinct prompts → distinct bytes).

Teardown destroys both pods. Total budget ~$3.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live smokes",
)


REPO = Path(__file__).resolve().parents[2]
CFG_14B = REPO / "examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml"
CFG_5B = REPO / "examples/configs/runpod-comfyui-wan-2_2-5b-t2v.yaml"
PROMPT_REALISTIC = REPO / "examples/configs/prompts/field-realistic.txt"
PROMPT_DREAMLIKE = REPO / "examples/configs/prompts/field-dreamlike.txt"
OUTPUT_DIR = REPO / "output"


def _run_generate(cfg: Path, prompt_path: Path, log_path: Path) -> str:
    """Run ``kinoforge generate`` and return the captured combined log text."""
    prompt = prompt_path.read_text()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as logf:
        proc = subprocess.run(
            [
                "pixi",
                "run",
                "kinoforge",
                "generate",
                "--config",
                str(cfg),
                "--prompt",
                prompt,
                "--mode",
                "t2v",
            ],
            cwd=str(REPO),
            stdout=logf,
            stderr=subprocess.STDOUT,
            # 65 min per leg: headroom over the cfg's 60m boot_timeout
            # so a slow HF Hub day doesn't truncate the subprocess
            # before the orchestrator gets to surface ProvisionTimeout.
            # Task 8 attempt #11 timed out at the orchestrator's 25m
            # boot_timeout while the download was 28/41 files in;
            # attempt #12 timing assumes 60m boot_timeout + 5m
            # generation + ~25m model load.
            timeout=3900,
        )
    log_text = log_path.read_text()
    assert proc.returncode == 0, (
        f"kinoforge generate failed with exit {proc.returncode}\n"
        f"Last 60 log lines:\n{log_text[-4000:]}"
    )
    return log_text


def _extract_pod_id(log_text: str) -> str:
    m = re.search(r"running provisioner\.provision for instance (\w+)", log_text)
    assert m is not None, "could not find pod id in log"
    return m.group(1)


def _extract_warm_attach_pod_id(log_text: str) -> str:
    m = re.search(r"warm-reuse: attached to (\w+)", log_text)
    assert m is not None, "expected warm-reuse log line not found"
    return m.group(1)


def _destroy(pod_id: str) -> None:
    subprocess.run(
        ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
        cwd=str(REPO),
        timeout=120,
        check=False,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _latest_output(slug_fragment: str) -> Path:
    """Return the newest MP4 in OUTPUT_DIR whose filename contains ``slug_fragment``."""
    candidates = [p for p in OUTPUT_DIR.glob("*.mp4") if slug_fragment in p.name]
    assert candidates, f"no MP4 matching {slug_fragment!r} found in {OUTPUT_DIR}"
    return max(candidates, key=lambda p: p.stat().st_mtime)


def test_wan22_native_t2v_a14b_cold_then_warm_then_cross_cap_key(
    tmp_path: Path,
) -> None:
    pre = subprocess.run(
        ["pixi", "run", "preflight"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert pre.returncode == 0, f"preflight failed:\n{pre.stdout}\n{pre.stderr}"

    pod_14b: str | None = None
    pod_5b: str | None = None
    try:
        # === Leg 1: 14B cold boot, field-realistic ===
        cold_log = _run_generate(
            CFG_14B,
            PROMPT_REALISTIC,
            log_path=tmp_path / "14b-cold.log",
        )
        pod_14b = _extract_pod_id(cold_log)
        cold_mp4 = _latest_output("Photorealistic-cinem")
        cold_sha = _sha256(cold_mp4)
        assert cold_mp4.stat().st_size > 100_000, "cold MP4 suspiciously small"
        assert cold_mp4.read_bytes()[4:8] == b"ftyp"

        # === Leg 2: 14B warm reuse, field-dreamlike ===
        warm_log = _run_generate(
            CFG_14B,
            PROMPT_DREAMLIKE,
            log_path=tmp_path / "14b-warm.log",
        )
        warm_pod = _extract_warm_attach_pod_id(warm_log)
        assert warm_pod == pod_14b, (
            f"warm-reuse hit wrong pod: warm_pod={warm_pod!r} pod_14b={pod_14b!r}"
        )
        warm_mp4 = _latest_output("Photorealistic-yet-d")
        warm_sha = _sha256(warm_mp4)
        assert warm_mp4.stat().st_size > 100_000
        assert warm_mp4.read_bytes()[4:8] == b"ftyp"
        assert cold_sha != warm_sha, (
            "cold and warm MP4 shas match — possible cached-output bug"
        )

        # === Leg 3: 5B cold boot WHILE 14B pod still alive ===
        cross_log = _run_generate(
            CFG_5B,
            PROMPT_REALISTIC,
            log_path=tmp_path / "5b-cross.log",
        )
        pod_5b = _extract_pod_id(cross_log)
        assert pod_5b != pod_14b, (
            f"cross-cap-key test FAILED: 5B reused 14B pod "
            f"pod_5b={pod_5b!r} pod_14b={pod_14b!r}"
        )
        assert "warm-reuse: attached" not in cross_log, (
            "5B unexpectedly logged warm-reuse"
        )
    finally:
        # If a leg's _run_generate raised on subprocess.run rc != 0, the
        # caller's `pod_14b` / `pod_5b` were never assigned because the
        # exception fired BEFORE _extract_pod_id ran. Recover the IDs by
        # re-reading the per-leg log files. This makes the teardown
        # idempotent across failure modes and prevents pod leaks
        # discovered by Task 8 attempts #25-27.
        for log_name, pod_var in (
            ("14b-cold.log", "pod_14b"),
            ("5b-cross.log", "pod_5b"),
        ):
            log_path = tmp_path / log_name
            if not log_path.exists():
                continue
            try:
                pod_id = _extract_pod_id(log_path.read_text())
            except AssertionError:
                continue
            if pod_var == "pod_14b" and not pod_14b:
                pod_14b = pod_id
            elif pod_var == "pod_5b" and not pod_5b:
                pod_5b = pod_id
        if pod_14b:
            _destroy(pod_14b)
        if pod_5b:
            _destroy(pod_5b)
