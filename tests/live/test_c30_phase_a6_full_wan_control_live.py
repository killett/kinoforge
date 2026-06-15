"""C30 Phase A6 — A5 + Wan model downloads + `exec python main.py`.

Walk-down rung 5 (terminal control): full C28-Phase-A boot via direct
GraphQL. If A6 RESTARTED, the bug is in model-download OR ComfyUI
startup. If A6 SURVIVED while A1a-A5 also SURVIVED, the bug does not
reproduce in C30 conditions (NO_REPRODUCTION_BUG_FLED per spec §6).
Requires HF_TOKEN. Gate: A5 SURVIVED.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import PROVISION_A6_LINES

from .conftest import (
    c30_execute_phase,
    c30_read_predecessor,
    c30_sidecar_path,
)

PHASE = "a6"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_a6_full_wan_control(c30_client: Any, c30_s3: Any) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    pred = c30_read_predecessor("a5")
    if pred is None:
        pytest.skip("A6 gated on A5 sidecar; A5 not yet committed")
    if pred["verdict"] != "survived":
        pytest.skip(f"A6 gated on A5=SURVIVED; found A5={pred['verdict']}")

    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        pytest.skip("A6 needs HF_TOKEN for Kijai/WanVideo_comfy downloads")

    verdict = c30_execute_phase(
        c30_client,
        c30_s3,
        phase=PHASE,
        image=IMAGE,
        ports="8188/http",
        provision_script="\n".join(PROVISION_A6_LINES),
        env={"HF_TOKEN": hf_token},
        window_s=1800,  # full Wan boot incl. ~15 GB downloads; 30-min window
    )
    assert verdict in {"survived", "restarted", "ambiguous"}
