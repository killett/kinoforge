"""C33 P0 — orphan disambiguation. Stock ubuntu pod, no mutations.

Decisive test for whether RunPod's negative ``uptimeInSeconds`` values
correlate with actual container restarts (advancing ``lastStartedAt``)
or are an API quirk. Resolves the C30 orphan signal.
"""

from __future__ import annotations

import os

import pytest

from .conftest import c33_execute_p0, c33_sidecar_path

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_c33_p0_stock_ubuntu_no_mutation(c30_client, c30_s3) -> None:  # type: ignore[no-untyped-def]
    """Run the C33 P0 probe and assert sidecar shape + verdict membership."""
    if c33_sidecar_path("p0").exists():
        pytest.skip("P0 sidecar already present; idempotent skip")

    sidecar = c33_execute_p0(c30_client, c30_s3)

    assert sidecar["phase"] == "p0"
    assert sidecar["verdict"] in {"orphan_quirk", "orphan_real_restart", "ambiguous"}
    assert sidecar["est_spend_usd"] <= 0.05
    assert sidecar["image"] == "mirror.gcr.io/library/ubuntu:22.04"
    assert len(sidecar["poll_trail"]) >= 20
