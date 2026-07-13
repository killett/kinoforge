"""Tier-3 live smoke: Wan 2.1 1.3B + 2 single LoRAs on RunPod A5000.

Drives the 4-step matrix against a real GPU using the shared
harness. Gated by KINOFORGE_LIVE_TESTS=1; fires weekly via
.github/workflows/smoke-wan21-weekly.yml (Mon 04:00 PT) +
manually via ``pixi run smoke-21b-live``.

The 4 harness fixes (UA, api_key, URLError retry, leak sweep) come
from tests/_smoke_harness/ — no smoke-specific reinvention.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._smoke_harness import budget, civitai, matrix, runpod_lifecycle

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod smoke",
)

REPO = Path(__file__).resolve().parents[3]
CFG = (
    REPO
    / "examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml"
)
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"

_TAG = "kinoforge-smoke-tier-3"
_BUDGET_CAP = 0.30


def _extract_pod_id(log_text: str) -> str:
    m = re.search(r"running provisioner\.provision for instance (\w+)", log_text)
    assert m is not None, f"no pod id in:\n{log_text[-2000:]}"
    return m.group(1)


def _cold_boot(prompt: str, log_path: Path) -> str:
    """Run cold-boot generate; return pod_id."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as f:
        proc = subprocess.run(  # noqa: S603
            [
                "pixi",
                "run",
                "kinoforge",
                "generate",
                "--config",
                str(CFG),
                "--prompt",
                prompt,
                "--mode",
                "t2v",
                "--run-id",
                "smoke-21b-step1",
            ],
            cwd=str(REPO),
            stdout=f,
            stderr=subprocess.STDOUT,
            timeout=1500,
            check=False,
        )
    text = log_path.read_text()
    assert proc.returncode == 0, f"cold-boot failed:\n{text[-3000:]}"
    return _extract_pod_id(text)


def test_lora_swap_matrix_wan21(tmp_path: Path) -> None:
    """4-step single-LoRA matrix end-to-end on real Wan 2.1 1.3B.

    Bug coverage:
    - Cold-boot accepts an empty initial LoRA stack.
    - set_stack [A] downloads + loads.
    - set_stack [B] evicts A + downloads B.
    - set_stack [] clears all adapters.
    - Generated mp4 differs per step (LoRA actually loaded).
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

    cfg = yaml.safe_load(CFG.read_text())
    lora_a, lora_b = cfg["smoke"]["lora_a"], cfg["smoke"]["lora_b"]
    assert "TODO" not in lora_a, "operator did not populate smoke.lora_a"
    assert "TODO" not in lora_b, "operator did not populate smoke.lora_b"

    prompt = PROMPT_FILE.read_text().strip()
    pod_id: str | None = None
    poller: runpod_lifecycle.PodStatPoller | None = None

    try:
        # STEP 1 — cold-boot, 0 LoRAs.
        pod_id = _cold_boot(prompt, tmp_path / "step1-cold-boot.log")
        base_url = runpod_lifecycle.resolve_proxy_url(pod_id)
        poller = runpod_lifecycle.PodStatPoller(pod_id, tmp_path / "pod-stats.log")
        poller.start()

        # STEPS 2-4 — drive matrix with the shared runner.
        specs = {
            lora_a: civitai.resolve(lora_a),
            lora_b: civitai.resolve(lora_b),
        }
        steps = [
            matrix.MatrixStep(
                name="step-2-load-a",
                target_stack=[lora_a],
                expected_inventory=[lora_a],
            ),
            matrix.MatrixStep(
                name="step-3-swap-to-b",
                target_stack=[lora_b],
                expected_inventory=[lora_b],
            ),
            matrix.MatrixStep(
                name="step-4-empty",
                target_stack=[],
                expected_inventory=[],
            ),
        ]
        matrix.run_matrix(
            cfg_path=CFG,
            pod_proxy_url=base_url,
            steps=steps,
            download_specs=specs,
            generate_per_step=True,
            sha_distinct_required=True,
            pod_id=pod_id,
            prompt=prompt,
        )

        budget.BudgetTracker(cap_usd=_BUDGET_CAP, pod_id=pod_id).assert_under_cap()
    finally:
        if poller is not None:
            poller.stop()
            poller.join(timeout=2.0)
        # No tag_filter: cfg.compute.tags is not currently propagated to
        # actual pod tags by the RunPod provider, so a tag-restricted
        # sweep reaps nothing. The smoke owns the workspace exclusively
        # (preflight asserts 0 active pods at start), so unconditional
        # sweep is safe. Tracked as separate cleanup; the leak-sweep
        # cron still catches anything this misses within 45 min.
        runpod_lifecycle.teardown_pod_or_raise(pod_id)
