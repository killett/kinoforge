"""C30 Phase A1a — stock pod, NO ports declared, `sleep 600`.

Tests whether the RunPod platform restarts a pod with no declared port.
SURVIVED → proceed to A1b (port declared, no listener). RESTARTED →
fork to A0' (alt image, isolate image hypothesis).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from .conftest import (
    c30_execute_phase,
    c30_sidecar_path,
)

PHASE = "a1a"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_a1a_stock_pod_no_port_sleep(c30_client: Any, c30_s3: Any) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    verdict = c30_execute_phase(
        c30_client,
        c30_s3,
        phase=PHASE,
        image=IMAGE,
        ports=None,
        provision_script="sleep 600",
        env={},
    )
    assert verdict in {"survived", "restarted", "ambiguous"}
