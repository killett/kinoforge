"""C30 Phase A3 — A2 + ComfyUI git clone (no pip).

Walk-down rung 2: if A3 RESTARTED, the bug is triggered by the clone
itself (network-bound bash exec, filesystem write under /workspace).
Inverse for A3=RESTARTED is A3 with `mkdir ComfyUI && touch flag` —
covered by manual debugging if hit. Gate: A2 SURVIVED.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import PROVISION_A3_LINES

from .conftest import (
    c30_execute_phase,
    c30_read_predecessor,
    c30_sidecar_path,
)

PHASE = "a3"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_a3_clone_only(c30_client: Any, c30_s3: Any) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    pred = c30_read_predecessor("a2")
    if pred is None:
        pytest.skip("A3 gated on A2 sidecar; A2 not yet committed")
    if pred["verdict"] != "survived":
        pytest.skip(f"A3 gated on A2=SURVIVED; found A2={pred['verdict']}")

    verdict = c30_execute_phase(
        c30_client,
        c30_s3,
        phase=PHASE,
        image=IMAGE,
        ports=None,
        provision_script="\n".join(PROVISION_A3_LINES),
        env={},
    )
    assert verdict in {"survived", "restarted", "ambiguous"}
