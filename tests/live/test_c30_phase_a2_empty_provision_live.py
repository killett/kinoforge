"""C30 Phase A2 — empty provision (cd + sleep) under direct GraphQL.

Walk-down rung 1: stock pod with the C28 trap pre-amble alone, no
ComfyUI clone, no pip install, no custom nodes, no models. If A2
RESTARTED, the bug is in the pre-amble or stock-pod boot. If A2
SURVIVED, advance to A3 (add clone). Gate: A1a SURVIVED AND A1b SURVIVED.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import PROVISION_A2_LINES

from .conftest import (
    c30_execute_phase,
    c30_read_predecessor,
    c30_sidecar_path,
)

PHASE = "a2"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_a2_empty_provision(c30_client: Any, c30_s3: Any) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    for upstream in ("a1a", "a1b"):
        pred = c30_read_predecessor(upstream)
        if pred is None:
            pytest.skip(f"A2 gated on {upstream} sidecar; not yet committed")
        if pred["verdict"] != "survived":
            pytest.skip(
                f"A2 gated on {upstream}=SURVIVED; found {upstream}={pred['verdict']}"
            )

    verdict = c30_execute_phase(
        c30_client,
        c30_s3,
        phase=PHASE,
        image=IMAGE,
        ports=None,
        provision_script="\n".join(PROVISION_A2_LINES),
        env={},
    )
    assert verdict in {"survived", "restarted", "ambiguous"}
