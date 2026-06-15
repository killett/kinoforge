"""C30 Phase A5 — A4 + three Kijai/Kosinkadink custom-node clones + pip.

Walk-down rung 4: adds three C28-Phase-A custom nodes (WanVideoWrapper,
KJNodes, VideoHelperSuite) with their pinned refs and pip installs.
RESTARTED here implicates one of those nodes' setup hook or a heavy
transitive pip dep. Gate: A4 SURVIVED.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import PROVISION_A5_LINES

from .conftest import (
    c30_execute_phase,
    c30_read_predecessor,
    c30_sidecar_path,
)

PHASE = "a5"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_a5_custom_nodes(c30_client: Any, c30_s3: Any) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    pred = c30_read_predecessor("a4")
    if pred is None:
        pytest.skip("A5 gated on A4 sidecar; A4 not yet committed")
    if pred["verdict"] != "survived":
        pytest.skip(f"A5 gated on A4=SURVIVED; found A4={pred['verdict']}")

    verdict = c30_execute_phase(
        c30_client,
        c30_s3,
        phase=PHASE,
        image=IMAGE,
        ports=None,
        provision_script="\n".join(PROVISION_A5_LINES),
        env={},
        window_s=1500,  # custom-node pip installs are slow; 25-min window
    )
    assert verdict in {"survived", "restarted", "ambiguous"}
