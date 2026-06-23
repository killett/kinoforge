"""Tier-3 live smoke: branch routing against real Wan 2.1 (1.3B) on RunPod.

P2 §7.1 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

Two RED-scaffolded cases pinned for the Tier-3 fire:

1. ``branch="auto"`` on a Wan-2.1 (single-transformer) pipeline → cold-boot
   succeeds, generate succeeds, no 400 because ``auto`` IS the
   single-transformer-only value.
2. ``branch="high_noise"`` on the same Wan-2.1 pod → ``/lora/set_stack``
   returns HTTP 400 with structured ``branch_routing`` /
   ``branch_unsupported_single_transformer`` body. The server NEVER
   loads the LoRA into a single transformer just because there's only
   one available — Q5 strict-reject contract.

Both are gated by ``KINOFORGE_LIVE_TESTS=1`` and RED-scaffolded via
``pytest.mark.xfail(strict=True)`` — the live-fire task (P2 Task 16)
flips the markers to GREEN after the fire confirms the pod-side
behavior matches.
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
CFG = REPO / "examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED scaffold — flips to GREEN when P2 Task 16 live-fire confirms "
        "``branch=auto`` is the single-transformer-only contract and the "
        "Wan 2.1 1.3B pod successfully generates with auto-branch LoRAs."
    ),
)
def test_auto_branch_succeeds_on_wan21(tmp_path: Path) -> None:
    """Bug catch: server (or LoraEntry) regressed to require an explicit
    ``high_noise``/``low_noise`` on every pipeline, breaking Wan-2.1
    deployments that legitimately use ``auto``."""
    raise NotImplementedError(
        "RED scaffold — fire P2 Task 16 to wire the cold-boot + set_stack "
        "calls against a real Wan 2.1 1.3B RunPod pod and assert "
        "200/inventory mirror succeeds for branch=auto entries."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED scaffold — flips to GREEN when P2 Task 16 live-fire confirms "
        "the Wan 2.1 pod returns HTTP 400 with reason "
        "``branch_unsupported_single_transformer`` for an explicit "
        "branch on a single-transformer pipeline."
    ),
)
def test_explicit_high_noise_branch_rejected_on_wan21(tmp_path: Path) -> None:
    """Bug catch: Wan-2.1 silently collapses explicit ``high_noise`` to
    the bare ``transformer``, returning a successful 200 — Q5
    lenient-collapse contract violation."""
    raise NotImplementedError(
        "RED scaffold — fire P2 Task 16 to wire a /lora/set_stack call "
        "with branch=high_noise against the live Wan 2.1 pod and assert "
        "HTTPException 400 + detail['reason'] == "
        "'branch_unsupported_single_transformer'."
    )
