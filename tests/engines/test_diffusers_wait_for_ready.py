"""Tests for DiffusersEngine.wait_for_ready."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from kinoforge.core.errors import ProvisionFailed, ProvisionTimeout
from kinoforge.core.interfaces import Instance
from kinoforge.engines.diffusers import DiffusersEngine


def _instance(status: str = "ready") -> Instance:
    return Instance(
        id="pod-d",
        provider="runpod",
        status=status,
        created_at=0.0,
        endpoints={"8000": "https://pod-d-8000.proxy.runpod.net"},
    )


def test_wait_for_ready_returns_on_first_success() -> None:
    inst = _instance()
    seen: list[str] = []

    def _http_get(url: str) -> dict[str, Any]:
        seen.append(url)
        return {"ok": True}

    DiffusersEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
        inst,
        http_get=_http_get,
        sleep=lambda _: None,
        get_instance=lambda _: inst,
        timeout_s=60.0,
    )
    assert seen == ["https://pod-d-8000.proxy.runpod.net/health"]


def test_wait_for_ready_raises_provision_failed_on_terminal_status() -> None:
    inst = _instance("starting")
    terminated = dataclasses.replace(inst, status="terminated")
    statuses = iter([inst, terminated])

    with pytest.raises(ProvisionFailed):
        DiffusersEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
            inst,
            http_get=lambda _: (_ for _ in ()).throw(ConnectionError("no")),
            sleep=lambda _: None,
            get_instance=lambda _: next(statuses),
            timeout_s=60.0,
        )


def test_wait_for_ready_raises_provision_failed_on_empty_endpoints() -> None:
    """Empty endpoints → fast-fail with clear message (parity with ComfyUI)."""
    inst = Instance(
        id="pod-empty",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={},
    )
    with pytest.raises(ProvisionFailed, match="no endpoints"):
        DiffusersEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
            inst,
            http_get=lambda _: {},
            sleep=lambda _: None,
            get_instance=lambda _: inst,
            timeout_s=60.0,
        )


def test_wait_for_ready_raises_provision_timeout_after_deadline() -> None:
    inst = _instance("starting")

    times = iter([0.0, 2.0, 12.0])
    import kinoforge.engines.diffusers as diff_mod

    real = diff_mod.time.monotonic  # type: ignore[attr-defined]
    diff_mod.time.monotonic = lambda: next(times)  # type: ignore[attr-defined]
    try:
        with pytest.raises(ProvisionTimeout):
            DiffusersEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
                inst,
                http_get=lambda _: (_ for _ in ()).throw(ConnectionError("no")),
                sleep=lambda _: None,
                get_instance=lambda _: inst,
                timeout_s=10.0,
            )
    finally:
        diff_mod.time.monotonic = real  # type: ignore[attr-defined]
