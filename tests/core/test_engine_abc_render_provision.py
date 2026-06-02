"""Lockdown tests for the new GenerationEngine ABC methods."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import Instance, RenderedProvision
from kinoforge.engines.fake import FakeEngine
from kinoforge.engines.hosted import HostedAPIEngine


def test_fake_engine_render_provision_returns_stub_payload() -> None:
    """FakeEngine returns a deterministic stub so orchestrator-wiring tests can spy."""
    engine = FakeEngine()
    rp = engine.render_provision({})
    assert isinstance(rp, RenderedProvision)
    assert rp.script == "echo fake"
    assert rp.run_cmd == ["sleep", "infinity"]
    assert rp.image == "fake:latest"
    assert rp.ports == ["8000"]
    assert rp.env_required == []


def test_fake_engine_wait_for_ready_returns_immediately() -> None:
    """FakeEngine never blocks; the orchestrator's wiring is exercised."""
    engine = FakeEngine()
    instance = Instance(id="fake-1", provider="local", status="ready", created_at=0.0)
    calls: list[str] = []

    def _http_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"ok": True}

    def _sleep(_: float) -> None:
        pass

    def _get_instance(_: str) -> Instance:
        return instance

    engine.wait_for_ready(
        instance,
        http_get=_http_get,
        sleep=_sleep,
        get_instance=_get_instance,
        timeout_s=10.0,
    )
    assert calls == []  # no polling needed for fake


def test_hosted_engine_render_provision_raises_not_implemented() -> None:
    """HostedAPIEngine has requires_compute=False; render_provision must refuse."""
    engine = HostedAPIEngine(
        creds=None,
        http_get=lambda _: {},
        http_post=lambda _url, _body: {},
    )
    with pytest.raises(
        NotImplementedError, match="does not support remote provisioning"
    ):
        engine.render_provision({})
