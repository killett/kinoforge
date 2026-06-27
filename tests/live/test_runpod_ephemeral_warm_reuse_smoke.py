"""Live smoke — two --ephemeral kinoforge generate calls share a RunPod pod.

Gated by KINOFORGE_LIVE_TESTS=1 (project convention). Skipped silently
otherwise — RED scaffold may be committed at any time without spend.

Requires:
  - .env with RUNPOD_API_KEY + HF_TOKEN
  - `pixi run preflight` exit 0 (no active pods, clean tree, creds present)

Reproduction case for 2026-06-27 ephemeral-warm-reuse-discovery: under
the old discovery channel two back-to-back ``kinoforge --ephemeral
generate`` invocations cold-booted two pods. With the EphemeralIndex
wired they share one pod via the disk-backed discovery row.

Cost cap: ≤ $0.50 (1.3B model on RTX 3070 / A5000 class GPU, ~13-16¢/hr).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live smokes",
)


REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "examples/configs/runpod-comfyui-wan-t2v-1_3b.yaml"
PROMPT_PATH = REPO / "examples/configs/prompts/field-realistic.txt"
STATE_DIR = REPO / ".kinoforge"


def _run_generate(prompt: str, log_path: Path) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as logf:
        return subprocess.run(
            [
                "pixi",
                "run",
                "kinoforge",
                "--ephemeral",
                "generate",
                "--config",
                str(CFG),
                "--mode",
                "t2v",
                "--prompt",
                prompt,
            ],
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=30 * 60,
        )


def _kinoforge_list() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pixi", "run", "kinoforge", "list"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_two_ephemeral_runs_share_pod(tmp_path: Path) -> None:
    """Reproduces the 2026-06-27 bug and proves the fix.

    Required evidence tokens (per plan acceptance):
      - run #1: cold-boot / provision marker in log
      - run #2: warm-attach / attached marker in log within 30s
    """
    prompt = PROMPT_PATH.read_text().strip()

    # Preflight gate
    preflight = subprocess.run(
        ["pixi", "run", "preflight"], capture_output=True, text=True, check=False
    )
    assert preflight.returncode == 0, (
        f"preflight failed; refusing live spend:\n{preflight.stdout}\n"
        f"{preflight.stderr}"
    )

    # Run #1: cold-boot
    log1 = tmp_path / "run1.log"
    r1 = _run_generate(prompt, log1)
    log1_text = log1.read_text()
    assert r1.returncode == 0, f"run #1 exit {r1.returncode}:\n{log1_text[-4000:]}"

    pod_id_run1: str | None = None
    for line in log1_text.splitlines():
        if "running provisioner.provision for instance" in line:
            pod_id_run1 = line.split("instance ")[1].split()[0].strip(",.")
            break
    assert pod_id_run1 is not None, (
        f"run #1 did not emit a provision marker — cold-boot evidence missing:\n"
        f"{log1_text[-2000:]}"
    )

    listing1 = _kinoforge_list()
    assert pod_id_run1 in listing1.stdout, (
        f"pod {pod_id_run1!r} not visible after run #1 — survived-pod contract broken:\n"
        f"{listing1.stdout}"
    )

    # Run #2: should attach
    log2 = tmp_path / "run2.log"
    r2 = _run_generate("a different prompt for run two", log2)
    log2_text = log2.read_text()
    assert r2.returncode == 0, f"run #2 exit {r2.returncode}:\n{log2_text[-4000:]}"

    pod_id_run2: str | None = None
    for line in log2_text.splitlines():
        if "warm-reuse: attached to" in line:
            pod_id_run2 = line.split("attached to ")[1].split()[0].strip(",.")
            break
    assert pod_id_run2 == pod_id_run1, (
        f"run #2 cold-booted pod {pod_id_run2!r} instead of attaching to run #1's "
        f"pod {pod_id_run1!r} — discovery channel broken:\n{log2_text[-2000:]}"
    )

    # Cleanup + post-destroy verification
    destroy = subprocess.run(
        ["pixi", "run", "kinoforge", "destroy", "--id", pod_id_run1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert destroy.returncode == 0, (
        f"destroy failed: stdout={destroy.stdout!r} stderr={destroy.stderr!r}"
    )
    assert f"destroyed: {pod_id_run1}" in destroy.stdout

    listing2 = _kinoforge_list()
    assert "No running instances" in listing2.stdout, listing2.stdout
    assert "No instances recorded in ledger" in listing2.stdout, listing2.stdout

    from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(STATE_DIR)
    remaining = [r for r in EphemeralIndex(store=store).rows() if r.id == pod_id_run1]
    assert remaining == [], (
        f"ephemeral-index still has row for destroyed pod {pod_id_run1!r}: {remaining}"
    )
