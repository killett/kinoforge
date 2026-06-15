"""C33 Q2 — P0-style probe on cloudType=SECURE.

Repeats the C33 P0 orphan-disambiguation probe (stock ubuntu, 10-min
window, no mutation), but forces `cloudType="SECURE"` instead of the
P0 default (which fell back to community cloud). Filed 2026-06-15 to
test whether the negative-uptime quirk seen on community cloud is
community-only or RunPod-wide.
"""

from __future__ import annotations

import os

import pytest

from .conftest import c33_execute_q2, c33_sidecar_path

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_c33_q2_secure_cloud_p0(c30_client, c30_s3) -> None:  # type: ignore[no-untyped-def]
    """Run a P0-shaped probe on `cloudType=SECURE` and assert sidecar shape."""
    if c33_sidecar_path("q2").exists():
        pytest.skip("Q2 sidecar already present; idempotent skip")
    sidecar = c33_execute_q2(c30_client, c30_s3)
    assert sidecar["phase"] == "q2"
    assert sidecar["cloud_type"] == "SECURE"
    assert sidecar["verdict"] in {"orphan_quirk", "orphan_real_restart", "ambiguous"}
    assert sidecar["est_spend_usd"] <= 0.05
    assert len(sidecar["poll_trail"]) >= 20
