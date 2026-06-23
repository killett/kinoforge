"""Tier-4 live smoke: Wan 2.2 14B dual-transformer routing matrix on RunPod.

P2 §7.1 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

Seven RED-scaffolded matrix cases verify per-transformer routing
end-to-end against a real Wan-2.2 14B A100 80GB pod. All cases share
a warm pod via ``--reuse`` to amortize the cold-boot. Spend cap: $2
(matches P1's Tier-4 budget) — enforced via
``tests/_smoke_harness/budget.py:BudgetTracker``.

Cases (per spec §7.1):
  1. Baseline (no LoRA) — reference output.
  2. Arcane high-noise only — style diff vs baseline; early-step.
  3. Arcane low-noise only — style diff vs baseline; late-step.
  4. Arcane pair canonical (h+l) — both effects; sha matches Tier-4
     baseline from P1 §10.
  5. Wrong routing (h→l, l→h) — generation succeeds but perceptibly
     off; capture sha as "proof routing matters" — wrong-routing sha
     != canonical sha.
  6. MoE + auto reject — 400 ``branch_auto_disallowed_on_moe``.
  7. Same ref in two branches (composite key) — both load + generate
     succeeds.

Gated by ``KINOFORGE_LIVE_TESTS=1``; RED via
``pytest.mark.xfail(strict=True)`` — P2 Task 16 fires the matrix and
flips each case GREEN once the pod-side behavior is confirmed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
        reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod smoke",
    ),
]


REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED scaffold — flips GREEN when Task 16 live-fire confirms an "
        "empty-LoRA cold-boot succeeds against the real Wan 2.2 14B pod."
    ),
)
def test_case_1_baseline_no_lora(tmp_path: Path) -> None:
    """Bug catch: cold-boot of empty LoRA stack regresses on Wan 2.2 —
    the routing path adds spurious arity validation that rejects an
    empty stack."""
    raise NotImplementedError("RED scaffold — Task 16 live-fire wires this case.")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED scaffold — flips GREEN when Task 16 confirms high-noise-only "
        "stack lands the LoRA into ``pipe.transformer`` (and only there)."
    ),
)
def test_case_2_arcane_high_noise_only(tmp_path: Path) -> None:
    """Bug catch: ``branch=high_noise`` silently lands into
    ``transformer_2`` (or both), defeating per-stage routing.
    Verification: generated sha differs from baseline (LoRA actually
    applied) AND from the wrong-routing case (right transformer)."""
    raise NotImplementedError("RED scaffold — Task 16 live-fire wires this case.")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED scaffold — flips GREEN when Task 16 confirms low-noise-only "
        "stack lands the LoRA into ``pipe.transformer_2`` (and only there)."
    ),
)
def test_case_3_arcane_low_noise_only(tmp_path: Path) -> None:
    """Bug catch: ``branch=low_noise`` silently lands into the bare
    ``transformer`` because ``load_into_transformer_2`` kwarg was dropped
    from the wire payload."""
    raise NotImplementedError("RED scaffold — Task 16 live-fire wires this case.")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED scaffold — flips GREEN when Task 16 confirms the Arcane h+l "
        "canonical pair generation sha matches the Tier-4 baseline from "
        "P1 §10."
    ),
)
def test_case_4_arcane_pair_canonical_high_plus_low(tmp_path: Path) -> None:
    """Bug catch: routing regression silently degrades the canonical
    pair output — Tier-4 baseline sha changes without anyone noticing.
    Sha-match against ``successful-generations.md §10`` is the
    invariant."""
    raise NotImplementedError("RED scaffold — Task 16 live-fire wires this case.")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED scaffold — flips GREEN when Task 16 confirms wrong-routing "
        "produces a DIFFERENT mp4 sha than the canonical pair (proof "
        "routing matters)."
    ),
)
def test_case_5_wrong_routing_h_into_low_and_l_into_high(tmp_path: Path) -> None:
    """Bug catch: routing is a no-op — wrong-routing sha equals canonical
    sha, meaning the per-transformer dispatch doesn't actually reach the
    transformers. Invariant: wrong_routing_sha != canonical_sha (case
    4)."""
    raise NotImplementedError("RED scaffold — Task 16 live-fire wires this case.")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED scaffold — flips GREEN when Task 16 confirms the Wan-2.2 "
        "pod returns HTTP 400 with reason "
        "``branch_auto_disallowed_on_moe`` for branch=auto requests."
    ),
)
def test_case_6_moe_with_auto_branch_returns_400(tmp_path: Path) -> None:
    """Bug catch: server accepts ``auto`` on Wan 2.2 and routes the
    LoRA into ``pipe.transformer`` only — silently half-applies the
    stack. Pre-load validation gate must reject before any unload/load
    fires."""
    raise NotImplementedError("RED scaffold — Task 16 live-fire wires this case.")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED scaffold — flips GREEN when Task 16 confirms same ref in "
        "two branches loads as two composite-keyed inventory entries "
        "AND generation succeeds (Q6 Option 1 composite identity)."
    ),
)
def test_case_7_same_ref_in_both_branches_composite_key(tmp_path: Path) -> None:
    """Bug catch: composite key collapse — inventory keys (ref, branch)
    accidentally reduce to ref-only, so the second-branch entry
    overwrites the first. Verification: /lora/inventory shows TWO rows
    for the same ref with different branches; generate succeeds."""
    raise NotImplementedError("RED scaffold — Task 16 live-fire wires this case.")
