"""C30 Phase A0' — alt image (ubuntu:22.04), no ports, `sleep 600`.

If A1a RESTARTED on the runpod/pytorch image, A0' isolates whether the
restart is image-specific (only pytorch image triggers it → image-bug)
or platform-wide (any stock image restarts → platform-bug).
Gate: A1a RESTARTED.
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

PHASE = "a0prime"
IMAGE = "mirror.gcr.io/library/ubuntu:22.04"

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_a0prime_alt_image_sleep(c30_client: Any, c30_s3: Any) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    pred = c30_read_predecessor("a1a")
    if pred is None:
        pytest.skip("A0' gated on A1a sidecar; A1a not yet committed")
    if pred["verdict"] != "restarted":
        pytest.skip(f"A0' gated on A1a=RESTARTED; found A1a={pred['verdict']}")

    verdict = c30_execute_phase(
        c30_client,
        c30_s3,
        phase=PHASE,
        image=IMAGE,
        ports=None,
        # ubuntu:22.04 lacks awscli + aria2 — trap pre-amble installs both.
        # `python3 -m http.server` is not present; pure shell sleep.
        provision_script="apt-get update -qq && sleep 600",
        env={},
    )
    assert verdict in {"survived", "restarted", "ambiguous"}
