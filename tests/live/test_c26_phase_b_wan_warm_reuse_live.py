"""C26 Phase B live smoke — Wan + ComfyUI 2-CLI cold-skip / PROVEN-PROTECTION.

Body shipped in Task 14. This module is committed in Task 12 (RED) to
satisfy the CLAUDE.md durability rule: any agent-generated tool that
drives live cloud spend must be committed BEFORE the spend is invoked.

Closes the C25 Task 4 deferred acceptance gate: cold-skip ratio
gen2 / gen1 ≤ 0.7 ⇒ CLEAN-PASS, or STALL_REAP fired mid-gen1 ⇒
PROVEN-PROTECTION.

Gated by KINOFORGE_LIVE_RUNPOD=1. Live spend ceiling: $0.55.
Spec: docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md §11 Phase B.
"""

from __future__ import annotations

import os

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.55
_PHASE_B_CFG = "tests/live/cfg_c26_phase_b.yaml"


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the Phase B Wan+ComfyUI smoke "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


@pytest.mark.xfail(reason="Phase B body ships in Task 14", strict=False)
def test_c26_phase_b_wan_warm_reuse_live() -> None:
    """2-CLI fresh-subprocess Wan + ComfyUI cold + warm; CLEAN-PASS or PROVEN-PROTECTION."""
    pytest.xfail("Task 14 ships the live body, 2-CLI subprocesses, and sidecar capture")
