"""Tier-3 live smoke: Wan 2.1 1.3B per-LoRA strength variation.

P1 (2026-06-21). Validates that ``set_adapters(adapter_weights=)``
actually reaches the pipeline by generating the SAME (prompt, seed,
LoRA-ref) tuple at multiple strength values and asserting the
outputs differ by sha256.

Gated by ``KINOFORGE_LIVE_TESTS=1`` so the smoke is OFF in CI's
default unit-test pass; budget cap $0.30 (Tier-3 ceiling — Wan
2.1 1.3B on an A5000 is ~$0.20/hr nominal).

Per kinoforge's "Commit RED scaffolds before any live spend" rule
(CLAUDE.md), this file is committed in RED form (xfail-marked) so
Task 13's first live invocation has a stable on-disk scaffold to
green out. A mid-spend session crash that lost this file would
force the next session to rewrite the test before retrying — a
much bigger blast radius than the $0.30 spend itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod Tier-3 smoke",
)

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml"
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"

_TAG = "kinoforge-smoke-tier-3-strength"
_BUDGET_CAP = 0.30


@pytest.mark.xfail(
    reason=(
        "RED scaffold — implementation pending Task 13 of "
        "docs/superpowers/plans/2026-06-21-server-lora-strength.md. "
        "Task 13 runs the live execution + visual diff between "
        "strength values."
    ),
    strict=True,
    run=True,
)
def test_lora_strength_variation_wan21(tmp_path: Path) -> None:
    """Generate the same (prompt, seed, LoRA-ref) at 3 strengths;
    assert the 3 outputs are pairwise distinct by sha256.

    Bug coverage (post-Task 13):
    - ``set_adapters(adapter_weights=)`` actually changes the
      pipeline output, not just the inventory metadata.
    - Strength variation does NOT trigger a cold-boot — same
      capability_key (refs unchanged), warm-reuse keeps the pod.
    - LoRA-side downloads happen exactly once for the shared ref.

    See Task 12 / 13 of the P1 plan for the matrix shape.
    """
    pytest.xfail("RED scaffold — Task 13 ships the live impl")
