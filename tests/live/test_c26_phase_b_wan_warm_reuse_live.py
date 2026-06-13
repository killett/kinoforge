"""C26 Phase B live smoke — Wan + ComfyUI cold-skip OR PROVEN-PROTECTION.

Replays the C25 Task 4 deferred acceptance gate with C26 stall
protections live. Two ``kinoforge generate`` subprocesses 60 s apart on
real Wan 2.1 14B T2V; gen2 should auto-attach via B3 warm-reuse and
finish in < 0.7 × gen1 wall (CLEAN-PASS). If Wan regresses into the
C25 stall symptom (~22 min wall, GPU/CPU near zero), the in-pod stall
window trips STALL_REAP, the pod is destroyed, gen1 aborts via the
cancel token → PROVEN-PROTECTION.

Either outcome closes the C25 Task 4 deferred gate.

Gated by KINOFORGE_LIVE_RUNPOD=1 AND KINOFORGE_LIVE_TESTS=1.
Live spend ceiling: $0.55.
Spec: docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md §11 Phase B.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_TESTS_GATE_ENV = "KINOFORGE_LIVE_TESTS"
_CFG_PATH = Path("tests/live/cfg_c26_phase_b.yaml")
_PROMPT_PATH = Path("/workspace/prompt-field-realistic.txt")
_SIDECAR_PATH = Path("tests/live/_c26_phase_b_smoke_evidence.json")
_BUDGET_USD_CAP = 0.55
_GEN_TIMEOUT_S = 60.0 * 60.0  # 60 min per gen
_BETWEEN_GENS_SLEEP_S = 60.0
_STALL_INDICATORS: tuple[str, ...] = (
    "STALL_REAP",
    "stall_window_s",
    "consecutive_low_util_count",
)


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(f"set {_LIVE_GATE_ENV}=1 to run the Phase B smoke")
    if os.environ.get(_TESTS_GATE_ENV) != "1":
        pytest.skip(f"set {_TESTS_GATE_ENV}=1 to opt into live Wan spend")


def _kinoforge_generate(
    *,
    cfg_path: Path,
    state_dir: Path,
    prompt: str,
    run_id: str,
    timeout_s: float = _GEN_TIMEOUT_S,
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
            str(cfg_path),
            "--prompt",
            prompt,
            "--mode",
            "t2v",
            "--run-id",
            run_id,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
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
        if entry.get("provider_kind") == "runpod" and entry.get("instance_id"):
            return str(entry["instance_id"])
    return None


def _destroy_safely(state_dir: Path, pod_id: str | None) -> None:
    if pod_id is None:
        return
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


def _detect_stall_protection(stderr: str, stdout: str) -> bool:
    """Heuristic: did the cancel token fire because STALL_REAP self-classified?"""
    combined = (stderr or "") + "\n" + (stdout or "")
    return any(token in combined for token in _STALL_INDICATORS)


def test_c26_phase_b_wan_warm_reuse_live() -> None:
    assert _CFG_PATH.exists(), f"cfg missing: {_CFG_PATH}"
    assert _PROMPT_PATH.exists(), f"prompt missing: {_PROMPT_PATH}"

    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    state_dir = (
        Path(os.environ.get("TMPDIR", "/tmp")) / f"c26-phase-b-{int(time.time())}"
    )
    state_dir.mkdir(parents=True, exist_ok=True)

    pod_id: str | None = None
    outcome: str = "INCONCLUSIVE"
    failure_reason: str | None = None
    gen1_elapsed: float | None = None
    gen2_elapsed: float | None = None
    gen1_returncode: int | None = None
    gen2_returncode: int | None = None
    gen1_stderr_tail: str = ""
    gen2_stderr_tail: str = ""

    try:
        # Gen 1 — cold create.
        t0 = time.time()
        r1 = _kinoforge_generate(
            cfg_path=_CFG_PATH,
            state_dir=state_dir,
            prompt=prompt,
            run_id="c26-phase-b-1",
        )
        gen1_elapsed = time.time() - t0
        gen1_returncode = r1.returncode
        gen1_stderr_tail = (r1.stderr or "")[-2000:]
        pod_id = _extract_pod_id_from_ledger(state_dir)

        if r1.returncode != 0:
            # Gen 1 failed. Check whether STALL_REAP fired (PROVEN-PROTECTION)
            # or whether it failed for some unrelated reason.
            if _detect_stall_protection(r1.stderr, r1.stdout):
                outcome = "PROVEN-PROTECTION"
            else:
                outcome = "FAILED"
                failure_reason = f"gen1 rc={r1.returncode} (no STALL indicator)"
        else:
            # Gen 1 succeeded — try gen 2 warm reuse.
            time.sleep(_BETWEEN_GENS_SLEEP_S)
            t0 = time.time()
            r2 = _kinoforge_generate(
                cfg_path=_CFG_PATH,
                state_dir=state_dir,
                prompt=prompt,
                run_id="c26-phase-b-2",
            )
            gen2_elapsed = time.time() - t0
            gen2_returncode = r2.returncode
            gen2_stderr_tail = (r2.stderr or "")[-2000:]
            if r2.returncode != 0:
                if _detect_stall_protection(r2.stderr, r2.stdout):
                    outcome = "PROVEN-PROTECTION"
                else:
                    outcome = "FAILED"
                    failure_reason = f"gen2 rc={r2.returncode}"
            else:
                ratio = gen2_elapsed / gen1_elapsed if gen1_elapsed else 99.0
                if ratio <= 0.7:
                    outcome = "CLEAN-PASS"
                else:
                    outcome = "FAILED"
                    failure_reason = (
                        f"cold-skip ratio {ratio:.3f} > 0.7 "
                        f"(gen1={gen1_elapsed:.0f}s gen2={gen2_elapsed:.0f}s)"
                    )
    finally:
        _destroy_safely(state_dir, pod_id)

    sidecar: dict[str, Any] = {
        "task": "C26 Task 14 — Wan + ComfyUI 2-CLI cold-skip / PROVEN-PROTECTION",
        "outcome": outcome,
        "captured_at": datetime.now().astimezone().isoformat(),
        "pod_id": pod_id,
        "budget_cap_usd": _BUDGET_USD_CAP,
        "gen1_elapsed_s": gen1_elapsed,
        "gen2_elapsed_s": gen2_elapsed,
        "gen1_returncode": gen1_returncode,
        "gen2_returncode": gen2_returncode,
        "cold_skip_ratio": (
            gen2_elapsed / gen1_elapsed
            if (gen2_elapsed is not None and gen1_elapsed)
            else None
        ),
        "failure_reason": failure_reason,
        "gen1_stderr_tail": gen1_stderr_tail,
        "gen2_stderr_tail": gen2_stderr_tail,
    }
    _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2, default=str))

    assert outcome in {"CLEAN-PASS", "PROVEN-PROTECTION"}, (
        f"Phase B did not close C25 Task 4 gate: outcome={outcome!r} "
        f"reason={failure_reason!r}"
    )
