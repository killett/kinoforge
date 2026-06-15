"""C30 Phase A1b — stock pod, ports=8188/http declared, `sleep 600`.

Tests port-healthcheck hypothesis (H1): does the platform restart when
a port is declared but no listener binds? Gate: A1a SURVIVED.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from .conftest import (
    c30_execute_phase,
    c30_read_predecessor,
    c30_sidecar_path,
)

PHASE = "a1b"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_a1b_stock_pod_port_declared_sleep(c30_client: Any, c30_s3: Any) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    pred = c30_read_predecessor("a1a")
    if pred is None:
        pytest.skip("A1b gated on A1a sidecar; A1a not yet committed")
    if pred["verdict"] != "survived":
        pytest.skip(f"A1b gated on A1a=SURVIVED; found A1a={pred['verdict']}")

    verdict = c30_execute_phase(
        c30_client,
        c30_s3,
        phase=PHASE,
        image=IMAGE,
        ports="8188/http",
        provision_script="sleep 600",
        env={},
    )
    assert verdict in {"survived", "restarted", "ambiguous"}
