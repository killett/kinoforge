"""C26 Phase A live smoke — FakeEngine intentional stall + STALL_REAP detection.

Body shipped in Task 13. This module is committed in Task 12 (RED) to
satisfy the CLAUDE.md durability rule: any agent-generated tool that
drives live cloud spend must be committed (failing tests / xfail
markers are fine) BEFORE the spend is invoked.

Gated by KINOFORGE_LIVE_RUNPOD=1. Live spend ceiling: $0.05.
Spec: docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md §11 Phase A.
"""

from __future__ import annotations

import os

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.05
_PHASE_A_CFG = "tests/live/cfg_c26_phase_a.yaml"


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the Phase A stall-detection smoke "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


@pytest.mark.xfail(reason="Phase A body ships in Task 13", strict=False)
def test_c26_phase_a_stall_detection_live() -> None:
    """Spin a cheap RunPod pod with FakeEngine + tight stall window; STALL_REAP fires."""
    pytest.xfail("Task 13 ships the live body and sidecar capture")
