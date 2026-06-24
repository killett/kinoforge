"""Tier-4 release-gate smoke: Wan 2.2 14B + Arcane LoRA pair on A100 80GB.

Drives the 4-step matrix from spec §13.3 against a real pod using the
shared harness (no bespoke _PROXY_UA / _auth_suffix / _destroy etc).

Step 1 — cold-boot, 0 LoRAs    → plain Wan T2V mp4 (no styling).
Step 2 — warm-attach, [high+low] → ArcaneStyle prompt, full styling.
Step 3 — warm-attach, [low]      → partial Arcane styling.
Step 4 — warm-attach, []         → no styling (eviction back to bare).

Gated by KINOFORGE_LIVE_TESTS=1. Manual release gate — invoke via
``pixi run smoke-wan22-live`` from docs/RELEASE-CHECKLIST.md.

Total spend cap: $2 enforced via BudgetTracker.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from tests._smoke_harness import budget, http, matrix, runpod_lifecycle

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod smoke",
)

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml"
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"
OUTPUT_DIR = REPO / "output"

# Arcane Style WAN 2.2 LoRA pair.
LORA_HIGH = "civitai:2197303@2474081"
LORA_LOW = "civitai:2197303@2474073"
TRIGGER = "ArcaneStyle"

_TAG = "kinoforge-smoke-tier-4"
_BUDGET_CAP = 2.00


def _run_cli(*args: str, log_path: Path, timeout: int = 3900) -> str:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as logf:
        proc = subprocess.run(  # noqa: S603
            ["pixi", "run", "kinoforge", *args],
            cwd=str(REPO),
            stdout=logf,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    text = log_path.read_text()
    assert proc.returncode == 0, (
        f"kinoforge {args[0]} exited {proc.returncode}\n"
        f"Last 60 log lines:\n{text[-4000:]}"
    )
    return text


def _extract_pod_id(log_text: str) -> str:
    for pattern in (
        r"running provisioner\.provision for instance (\w+)",
        r"warm-reuse: attached to (\w+)",
    ):
        m = re.search(pattern, log_text)
        if m:
            return m.group(1)
    raise AssertionError(f"could not find pod id in log; tail:\n{log_text[-2000:]}")


def _civitai_artifact_spec(ref: str) -> dict[str, object]:
    """Resolve to a /lora/set_stack download_spec dict."""
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.sources.civitai import CivitAISource

    arts = CivitAISource().resolve(ref, EnvCredentialProvider())
    pick = next((a for a in arts if a.filename.endswith(".safetensors")), arts[0])
    return {
        "url": pick.url,
        "headers": dict(pick.headers or {}),
        "filename": pick.filename,
        "size_hint": pick.size,
    }


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _latest_mp4_after(deadline_s: float) -> Path:
    candidates = [p for p in OUTPUT_DIR.glob("*.mp4") if p.stat().st_mtime > deadline_s]
    assert candidates, f"no mp4 written after {deadline_s} under {OUTPUT_DIR}"
    return max(candidates, key=lambda p: p.stat().st_mtime)


def test_wan22_lora_warm_reuse_4_step_matrix(tmp_path: Path) -> None:
    """4-step warm-reuse matrix on real A100 80GB; total spend < $2.

    Bug coverage:
    - Cold-boot accepts an empty initial LoRA stack.
    - /lora/inventory returns [] when no LoRAs are loaded.
    - /lora/set_stack downloads + loads the Arcane high/low pair.
    - Eviction (full → partial → empty) reclaims VRAM + disk.
    - Same warm pod serves all 4 steps — no cold-boot in between.
    - Each step's mp4 sha differs (LoRA actually affects output).
    """
    pre = subprocess.run(  # noqa: S603
        ["pixi", "run", "preflight"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert pre.returncode == 0, f"preflight failed:\n{pre.stdout}\n{pre.stderr}"

    prompt_plain = PROMPT_FILE.read_text().strip()
    prompt_styled = f"{TRIGGER} {prompt_plain}"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    start_ts = time.time()
    pod_id: str | None = None
    poller: runpod_lifecycle.PodStatPoller | None = None

    try:
        # STEP 1 — cold-boot, 0 LoRAs.
        leg1_log = tmp_path / "step1-cold-boot.log"
        out1 = _run_cli(
            "generate",
            "--config",
            str(CFG),
            "--prompt",
            prompt_plain,
            "--mode",
            "t2v",
            "--run-id",
            "lora-warm-reuse-release-step1",
            log_path=leg1_log,
        )
        pod_id = _extract_pod_id(out1)
        mp4_1 = _latest_mp4_after(start_ts)
        sha_1 = _sha256(mp4_1)

        base_url = runpod_lifecycle.resolve_proxy_url(pod_id)
        poller = runpod_lifecycle.PodStatPoller(pod_id, tmp_path / "pod-stats.log")
        poller.start()

        inv1 = http.get_json(f"{base_url}/lora/inventory", timeout=30)
        assert inv1.get("inventory") == [], (
            f"step-1 cold-boot: expected empty inventory, got {inv1!r}"
        )

        # STEPS 2-4 driven via the shared matrix runner.
        specs = {
            LORA_HIGH: _civitai_artifact_spec(LORA_HIGH),
            LORA_LOW: _civitai_artifact_spec(LORA_LOW),
        }
        steps = [
            matrix.MatrixStep(
                name="step-2-warm-high-low",
                target_stack=[LORA_HIGH, LORA_LOW],
                expected_inventory=[LORA_HIGH, LORA_LOW],
            ),
            matrix.MatrixStep(
                name="step-3-warm-low-only",
                target_stack=[LORA_LOW],
                expected_inventory=[LORA_LOW],
            ),
            matrix.MatrixStep(
                name="step-4-warm-empty",
                target_stack=[],
                expected_inventory=[],
            ),
        ]
        report = matrix.run_matrix(
            cfg_path=CFG,
            pod_proxy_url=base_url,
            steps=steps,
            download_specs=specs,
            generate_per_step=True,
            sha_distinct_required=True,
            pod_id=pod_id,
            prompt=prompt_styled,
        )

        # Step-4 prompt should not include trigger; rerun via CLI for fidelity.
        step4_ts = time.time()
        _run_cli(
            "generate",
            "--config",
            str(CFG),
            "--prompt",
            prompt_plain,
            "--mode",
            "t2v",
            "--instance-id",
            pod_id,
            "--run-id",
            "lora-warm-reuse-release-step4-bare",
            log_path=tmp_path / "step4-warm-empty-bare.log",
        )
        mp4_4 = _latest_mp4_after(step4_ts)
        sha_4 = _sha256(mp4_4)

        # All 4 shas distinct + non-empty mp4s.
        report_shas = [r.mp4_sha for r in report.steps if r.mp4_sha]
        shas = {sha_1, sha_4, *report_shas}
        assert len(shas) == 5, (
            f"expected 5 distinct mp4 shas across the matrix, got {shas!r}"
        )

        inv_final = http.get_json(f"{base_url}/lora/inventory", timeout=30)
        assert inv_final.get("inventory") == [], (
            f"final /lora/inventory should be empty; got {inv_final!r}"
        )

        budget.BudgetTracker(cap_usd=_BUDGET_CAP, pod_id=pod_id).assert_under_cap()
    finally:
        if poller is not None:
            poller.stop()
            poller.join(timeout=2.0)
        # No tag_filter — see live_wan21/test_lora_swap_matrix.py for why.
        runpod_lifecycle.teardown_pod_or_raise(pod_id, repo_root=REPO)
