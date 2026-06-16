"""C33 Q1 — top-level `Pod.uptimeSeconds` vs wall-clock estimate.

Tests whether the top-level GraphQL field `Pod.uptimeSeconds` reports
the same value as `now_utc - lastStartedAt`, and whether the nested
`runtime.uptimeInSeconds` does the same. Filed 2026-06-15 to
substantiate (or refute) the C33 closeout claim that the nested field
is unreliable on RunPod community cloud.
"""

from __future__ import annotations

import os

import pytest

from .conftest import c33_execute_q1, c33_sidecar_path

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_c33_q1_dual_uptime_fields(c30_client, c30_s3) -> None:  # type: ignore[no-untyped-def]
    """Capture 5-min poll trail of both uptime fields + lastStartedAt-derived estimate."""
    if c33_sidecar_path("q1").exists():
        pytest.skip("Q1 sidecar already present; idempotent skip")
    sidecar = c33_execute_q1(c30_client, c30_s3)
    assert sidecar["phase"] == "q1"
    assert sidecar["image"] == "mirror.gcr.io/library/ubuntu:22.04"
    assert sidecar["est_spend_usd"] <= 0.05
    assert sidecar["n_samples"] >= 10
