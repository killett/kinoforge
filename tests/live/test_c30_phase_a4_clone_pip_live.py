"""C30 Phase A4 — A3 + ComfyUI requirements pip install.

Walk-down rung 3: adds the `pip install -q -r requirements.txt` step.
RESTARTED here implicates either a pip-resolved transitive dep or the
network burst of the requirements download. Gate: A3 SURVIVED.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import PROVISION_A4_LINES

from .conftest import (
    c30_execute_phase,
    c30_read_predecessor,
    c30_sidecar_path,
)

PHASE = "a4"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_a4_clone_pip(c30_client: Any, c30_s3: Any) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    pred = c30_read_predecessor("a3")
    if pred is None:
        pytest.skip("A4 gated on A3 sidecar; A3 not yet committed")
    if pred["verdict"] != "survived":
        pytest.skip(f"A4 gated on A3=SURVIVED; found A3={pred['verdict']}")

    verdict = c30_execute_phase(
        c30_client,
        c30_s3,
        phase=PHASE,
        image=IMAGE,
        ports=None,
        provision_script="\n".join(PROVISION_A4_LINES),
        env={},
        window_s=1200,  # pip install is slow; 20-min window
    )
    assert verdict in {"survived", "restarted", "ambiguous"}
