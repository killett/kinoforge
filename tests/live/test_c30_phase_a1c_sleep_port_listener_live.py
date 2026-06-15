"""C30 Phase A1c — stock pod, ports=8188/http, listener bound (inverse).

If A1b RESTARTED (port declared without listener killed the pod), A1c
binds a trivial HTTP listener on the declared port and should SURVIVE,
confirming H1 (port healthcheck) decisively. Gate: A1b RESTARTED.
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

PHASE = "a1c"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_a1c_stock_pod_port_listener_sleep(c30_client: Any, c30_s3: Any) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    pred = c30_read_predecessor("a1b")
    if pred is None:
        pytest.skip("A1c gated on A1b sidecar; A1b not yet committed")
    if pred["verdict"] != "restarted":
        pytest.skip(f"A1c gated on A1b=RESTARTED; found A1b={pred['verdict']}")

    verdict = c30_execute_phase(
        c30_client,
        c30_s3,
        phase=PHASE,
        image=IMAGE,
        ports="8188/http",
        provision_script="python3 -m http.server 8188 & sleep 600",
        env={},
    )
    assert verdict in {"survived", "restarted", "ambiguous"}
