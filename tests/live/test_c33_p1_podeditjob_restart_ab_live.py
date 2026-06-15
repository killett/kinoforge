"""C33 P1 — main hypothesis A/B. One PodEditJob mutation, watch lastStartedAt.

Runs only if P0 verdict == orphan_quirk (the C30 negative-uptime rule
was over-broad). Otherwise the P0 finding takes precedence and P1 is
deferred per spec §7 routing.
"""

from __future__ import annotations

import json
import os

import pytest

from .conftest import c33_execute_p1, c33_sidecar_path

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_c33_p1_one_podeditjob_then_observe(c30_client, c30_s3) -> None:  # type: ignore[no-untyped-def]
    """Issue one PodEditJob mutation against a stable pod; assert verdict."""
    p0_path = c33_sidecar_path("p0")
    if not p0_path.exists():
        pytest.skip("P0 sidecar absent; run P0 first")
    p0 = json.loads(p0_path.read_text())
    if p0["verdict"] != "orphan_quirk":
        pytest.skip(
            f"P0 verdict={p0['verdict']} — P1 deferred per spec §7. "
            f"Routing: orphan_real_restart → C34c characterization, "
            f"ambiguous → rerun P0 once before P1."
        )

    if c33_sidecar_path("p1").exists():
        pytest.skip("P1 sidecar already present; idempotent skip")

    sidecar = c33_execute_p1(c30_client, c30_s3)

    assert sidecar["phase"] == "p1"
    assert sidecar["verdict"] in {"confirmed", "denied", "ambiguous"}
    assert sidecar["est_spend_usd"] <= 0.05
