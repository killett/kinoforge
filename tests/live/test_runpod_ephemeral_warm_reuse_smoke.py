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

import json
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
INDEX_FILE = STATE_DIR / "_lifecycle" / "ephemeral-index.json"


def _runpod_live_pod_ids() -> set[str]:
    """Query RunPod GraphQL for currently-live pod ids.

    The `kinoforge list` CLI reads the on-disk ledger; under --ephemeral
    the ledger is empty even when the pod is alive. RunPod is the
    ground truth.
    """
    from kinoforge.providers.runpod.util import _default_http_post

    post = _default_http_post(os.environ["RUNPOD_API_KEY"])
    payload = post(
        "https://api.runpod.io/graphql",
        {"query": "{ myself { pods { id desiredStatus } } }"},
    )
    pods = payload["data"]["myself"]["pods"]
    return {
        p["id"]
        for p in pods
        if p.get("desiredStatus") in {"RUNNING", "STARTING", "PROVISIONING"}
    }


def _index_pod_ids() -> set[str]:
    if not INDEX_FILE.exists():
        return set()
    data = json.loads(INDEX_FILE.read_text())
    return {r["id"] for r in data.get("rows", [])}


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

    # Pod must survive between runs. kinoforge list under --ephemeral
    # reads the empty disk ledger; ground truth is the index file +
    # RunPod GraphQL.
    assert pod_id_run1 in _index_pod_ids(), (
        f"pod {pod_id_run1!r} not in ephemeral-index after run #1 — "
        f"write site failed:\n"
        f"{INDEX_FILE.read_text() if INDEX_FILE.exists() else '<missing>'}"
    )
    assert pod_id_run1 in _runpod_live_pod_ids(), (
        f"pod {pod_id_run1!r} not running on RunPod after run #1 — "
        f"survived-pod contract broken"
    )

    # Run #2: should attach
    log2 = tmp_path / "run2.log"
    r2 = _run_generate("a different prompt for run two", log2)
    log2_text = log2.read_text()
    assert r2.returncode == 0, f"run #2 exit {r2.returncode}:\n{log2_text[-4000:]}"

    pod_id_run2: str | None = None
    second_provision: str | None = None
    for line in log2_text.splitlines():
        if "warm-reuse: attached to" in line:
            pod_id_run2 = line.split("attached to ")[1].split()[0].strip(",.")
        elif "running provisioner.provision for instance" in line:
            second_provision = line.split("instance ")[1].split()[0].strip(",.")
    if pod_id_run2 is None and second_provision is None:
        if {pod_id_run1} == _runpod_live_pod_ids():
            pod_id_run2 = pod_id_run1
    assert second_provision is None or second_provision == pod_id_run1, (
        f"run #2 cold-booted a SECOND pod {second_provision!r} instead of "
        f"attaching to run #1's pod {pod_id_run1!r} — discovery channel "
        f"broken:\n{log2_text[-3000:]}"
    )
    assert pod_id_run2 == pod_id_run1, (
        f"run #2 expected to attach to {pod_id_run1!r}, got "
        f"{pod_id_run2!r}:\n{log2_text[-2000:]}"
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
    assert (
        f"destroyed: {pod_id_run1}" in destroy.stdout
        or f"destroyed orphan: {pod_id_run1}" in destroy.stdout
    ), destroy.stdout

    assert pod_id_run1 not in _index_pod_ids(), (
        f"ephemeral-index still has row for destroyed pod {pod_id_run1!r}: "
        f"{INDEX_FILE.read_text()}"
    )
    assert pod_id_run1 not in _runpod_live_pod_ids(), (
        f"RunPod still has pod {pod_id_run1!r} after destroy"
    )
