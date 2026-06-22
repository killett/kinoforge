"""Tier-4 release-gate live smoke: Wan 2.2 14B per-LoRA strength variation.

P1 (2026-06-21). Validates the production-scale wiring on the Wan
2.2 MoE pair: ``set_adapters(adapter_weights=)`` reaches BOTH the
high-noise and the low-noise transformers correctly, generating
visibly distinct outputs at multiple strength values for the
fixed (prompt, seed, LoRA-pair) tuple.

Gated by ``KINOFORGE_LIVE_TESTS=1`` so the smoke is OFF in CI's
default unit-test pass; budget cap $1.50 (Tier-4 ceiling — Wan
2.2 14B on an A100 80GB is ~$2.00/hr nominal).

Per kinoforge's "Commit RED scaffolds before any live spend" rule
(CLAUDE.md), this file is committed in RED form (xfail-marked) so
Task 15's first live invocation has a stable on-disk scaffold to
green out. The Tier-3 RED scaffold (Task 12, sibling file in
live_wan21) lands first — Tier-4 follows the same shape with the
14B cfg and the dual-transformer LoRA pair.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod Tier-4 smoke",
)

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml"
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"

_TAG = "kinoforge-smoke-tier-4-strength"
_BUDGET_CAP = 1.50


@pytest.mark.xfail(
    reason=(
        "RED scaffold — implementation pending Task 15 of "
        "docs/superpowers/plans/2026-06-21-server-lora-strength.md. "
        "Task 15 runs the live execution + visual diff between "
        "strength values on the Wan 2.2 14B MoE pair."
    ),
    strict=True,
    run=True,
)
def test_lora_strength_variation_wan22(tmp_path: Path) -> None:
    """Generate the same (prompt, seed, Arcane-pair) at 3 strengths;
    assert the 3 outputs are pairwise distinct by sha256 AND visually
    different per release-gate visual eval.

    Bug coverage (post-Task 15):
    - ``set_adapters(adapter_weights=)`` reaches BOTH transformers
      in the Wan 2.2 MoE pair (high-noise + low-noise) — not just
      one of them.
    - Strength variation does NOT trigger a cold-boot — same
      capability_key (refs unchanged), warm-reuse keeps the pod.
    - LoRA pair downloads happen exactly once for the shared refs.

    See Task 14 / 15 of the P1 plan for the matrix shape.
    """
    pytest.xfail("RED scaffold — Task 15 ships the live impl")
