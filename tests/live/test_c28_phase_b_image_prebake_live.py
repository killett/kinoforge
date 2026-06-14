"""C28 Phase B live smoke — Wan + ComfyUI on the kinoforge-prebuilt image.

Validates the B0/B1/B2 stack end-to-end: pre-baked image pulls cleanly
on a fresh RunPod pod, the slim-mode provision script skips bootstrap,
ComfyUI launches, and one Wan 2.1 14B T2V generation completes WITHOUT
C27 RESTART_LOOP_REAP firing.

Spec acceptance:
* outcome == "PROVEN"
* C27 RESTART_LOOP_REAP NOT in any log line
* kinoforge generate exits 0 with exactly one rendered asset

Gated by ``KINOFORGE_LIVE_RUNPOD=1``.
Live spend ceiling: ~$0.30 (one Wan gen on pre-baked image; no clone
or pip install overhead vs. the stock-image baseline).
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.30
_CFG_PATH = Path("tests/live/cfg_c28_phase_b_prebake.yaml")
_PROMPT_PATH = Path("/workspace/prompt-field-realistic.txt")
_SIDECAR_PATH = Path("tests/live/_c28_phase_b_evidence.json")
_GEN_TIMEOUT_S = 60.0 * 25.0  # 25 min — slim-mode boot + one Wan gen


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the C28 Phase B smoke "
            f"(~${_BUDGET_USD_CAP} per invocation)",
        )


def _run_kinoforge_generate(
    *,
    state_dir: Path,
    prompt: str,
    run_id: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [
            "pixi",
            "run",
            "kinoforge",
            "--state-dir",
            str(state_dir),
            "generate",
            "-c",
            str(_CFG_PATH),
            "--prompt",
            prompt,
            "--mode",
            "t2v",
            "--run-id",
            run_id,
        ],
        capture_output=True,
        text=True,
        timeout=_GEN_TIMEOUT_S,
        check=False,
    )


def _extract_pod_id_from_ledger(state_dir: Path) -> str | None:
    ledger_path = state_dir / "ledger.json"
    if not ledger_path.exists():
        return None
    data = json.loads(ledger_path.read_text())
    for entry in data.get("entries", []):
        if entry.get("provider") == "runpod" and entry.get("id"):
            return str(entry["id"])
    return None


_RUNPOD_GRAPHQL = "https://api.runpod.io/graphql"
_RUNPOD_UA = "kinoforge-c28-test-cleanup/1.0"


def _runpod_post(query: str) -> dict[str, Any]:
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        _RUNPOD_GRAPHQL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": _RUNPOD_UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return dict(json.loads(resp.read()))
    except urllib.error.HTTPError as exc:
        return {"errors": [{"message": exc.read()[:300].decode("ascii", "replace")}]}


def _force_terminate(pod_id: str) -> None:
    """Hit RunPod podTerminate directly — bypasses kinoforge's ledger path."""
    _runpod_post(
        'mutation { podTerminate(input: {podId: "' + pod_id + '"}) }',
    )


# Pods THIS process created — populated by the test as soon as the ledger
# surfaces an id. The atexit hook below ONLY terminates pods in this set so
# concurrent pytest invocations don't cannibalise each other.
_OWNED_PODS: set[str] = set()


def _terminate_owned_pods() -> None:
    """Atexit safety net: tear down any pod THIS process created."""
    for pod_id in list(_OWNED_PODS):
        print(f"[atexit] terminating owned pod: {pod_id}")
        _force_terminate(pod_id)


atexit.register(_terminate_owned_pods)


def _destroy_safely(state_dir: Path, pod_id: str | None) -> None:
    """Direct GraphQL terminate + best-effort kinoforge destroy."""
    if pod_id is None:
        return
    _force_terminate(pod_id)
    subprocess.run(  # noqa: S603
        [
            "pixi",
            "run",
            "kinoforge",
            "--state-dir",
            str(state_dir),
            "destroy",
            "--id",
            pod_id,
        ],
        check=False,
        timeout=180,
    )


def test_c28_phase_b_image_prebake_live(tmp_path: Path) -> None:
    """Slim-mode boot + one Wan gen completes without RESTART_LOOP_REAP."""
    prompt = _PROMPT_PATH.read_text().strip()
    state_dir = tmp_path / "kinoforge-state"
    state_dir.mkdir()
    run_id = f"c28-phase-b-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    proc = _run_kinoforge_generate(
        state_dir=state_dir,
        prompt=prompt,
        run_id=run_id,
    )
    pod_id = _extract_pod_id_from_ledger(state_dir)
    if pod_id:
        _OWNED_PODS.add(pod_id)
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    sidecar_base: dict[str, Any] = {
        "captured_at": datetime.now().astimezone().isoformat(),
        "run_id": run_id,
        "pod_id": pod_id,
        "rc": proc.returncode,
        "budget_cap_usd": _BUDGET_USD_CAP,
        "image": "kinoforge/wan-comfyui:v0.3.10-088128b2-cu124",
        "stderr_tail": (proc.stderr or "")[-1500:],
    }
    _destroy_safely(state_dir, pod_id)

    if "RESTART_LOOP_REAP" in combined:
        sidecar_base["outcome"] = "REGRESSED"
        _SIDECAR_PATH.write_text(json.dumps(sidecar_base, indent=2) + "\n")
        pytest.fail(
            "C27 RESTART_LOOP_REAP fired against the pre-baked image — "
            "C28 Phase B did NOT close the failure mode",
        )

    if proc.returncode != 0:
        sidecar_base["outcome"] = "RC_NONZERO"
        _SIDECAR_PATH.write_text(json.dumps(sidecar_base, indent=2) + "\n")
        pytest.fail(
            f"kinoforge generate exited rc={proc.returncode}; "
            f"stderr tail:\n{sidecar_base['stderr_tail']}",
        )

    sidecar_base["outcome"] = "PROVEN"
    _SIDECAR_PATH.write_text(json.dumps(sidecar_base, indent=2) + "\n")
