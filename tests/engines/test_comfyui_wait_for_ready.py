"""Tests for ComfyUIEngine.wait_for_ready (engine-specific readiness polling)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from kinoforge.core.errors import ProvisionFailed, ProvisionTimeout
from kinoforge.core.interfaces import Instance
from kinoforge.engines.comfyui import ComfyUIEngine


def _instance(status: str = "ready") -> Instance:
    return Instance(
        id="pod-abc",
        provider="runpod",
        status=status,
        created_at=0.0,
        endpoints={"8188": "https://pod-abc-8188.proxy.runpod.net"},
    )


def test_wait_for_ready_returns_when_first_http_get_succeeds() -> None:
    """First poll: http_get returns; method returns immediately."""
    inst = _instance()
    calls: list[str] = []

    def _http_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"ok": True}

    ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
        inst,
        http_get=_http_get,
        sleep=lambda _: None,
        get_instance=lambda _: inst,
        timeout_s=60.0,
    )
    assert calls == ["https://pod-abc-8188.proxy.runpod.net/system_stats"]


def test_wait_for_ready_polls_until_http_get_stops_raising() -> None:
    """Endpoint not up yet → retry after sleep → eventually OK."""
    inst = _instance()
    attempt = {"n": 0}

    def _http_get(url: str) -> dict[str, Any]:
        attempt["n"] += 1
        if attempt["n"] < 3:
            raise ConnectionError("pod not up yet")
        return {"ok": True}

    sleeps: list[float] = []

    ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
        inst,
        http_get=_http_get,
        sleep=sleeps.append,
        get_instance=lambda _: inst,
        timeout_s=60.0,
    )
    assert attempt["n"] == 3
    # Two failed polls → two sleeps before the third succeeds.
    assert sleeps == [5.0, 5.0]


def test_wait_for_ready_raises_provision_failed_on_terminal_status() -> None:
    """Pod boot script crashed → status flips terminated → fast-fail."""
    inst = _instance("starting")
    terminated_inst = dataclasses.replace(inst, status="terminated")
    statuses = iter([inst, terminated_inst])

    def _http_get(url: str) -> dict[str, Any]:
        raise ConnectionError("not ready")

    with pytest.raises(ProvisionFailed) as exc_info:
        ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
            inst,
            http_get=_http_get,
            sleep=lambda _: None,
            get_instance=lambda _: next(statuses),
            timeout_s=60.0,
        )
    assert "pod-abc" in str(exc_info.value)
    assert "terminated" in str(exc_info.value)


def test_wait_for_ready_raises_provision_timeout_after_deadline() -> None:
    """Endpoint never comes up → deadline crossed → ProvisionTimeout."""
    inst = _instance("starting")

    times = iter([0.0, 2.0, 12.0])  # exceeds timeout_s=10.0 on third tick

    def _http_get(url: str) -> dict[str, Any]:
        raise ConnectionError("not ready")

    import kinoforge.engines.comfyui as comfyui_mod

    real_monotonic = comfyui_mod.time.monotonic  # type: ignore[attr-defined]

    def _monotonic() -> float:
        return next(times)

    comfyui_mod.time.monotonic = _monotonic  # type: ignore[attr-defined]
    try:
        with pytest.raises(ProvisionTimeout) as exc_info:
            ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
                inst,
                http_get=_http_get,
                sleep=lambda _: None,
                get_instance=lambda _: inst,
                timeout_s=10.0,
            )
    finally:
        comfyui_mod.time.monotonic = real_monotonic  # type: ignore[attr-defined]
    assert "pod-abc" in str(exc_info.value)
    assert "10" in str(exc_info.value)
