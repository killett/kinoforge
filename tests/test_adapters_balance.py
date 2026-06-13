"""Tests for the B2 build_balance_endpoint_for dispatch helper."""

from __future__ import annotations

import pytest

from kinoforge._adapters import build_balance_endpoint_for
from kinoforge.core.balance_endpoints import NoBalanceEndpoint
from kinoforge.providers.runpod.balance import RunPodBalanceEndpoint


class _FakeCompute:
    def __init__(self, provider: str) -> None:
        self.provider = provider


class _FakeEngine:
    def __init__(self, kind: str) -> None:
        self.kind = kind


class _FakeCfg:
    """Minimal Config-shaped object — only the attrs the dispatcher reads."""

    def __init__(
        self,
        *,
        compute: _FakeCompute | None = None,
        engine: _FakeEngine | None = None,
    ) -> None:
        self.compute = compute
        self.engine = engine


class _FakeCreds:
    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._mapping = mapping

    def get(self, key: str) -> str | None:
        return self._mapping.get(key)


def test_runpod_provider_returns_runpod_balance_endpoint() -> None:
    cfg = _FakeCfg(
        compute=_FakeCompute("runpod"),
        engine=_FakeEngine("comfyui"),
    )
    creds = _FakeCreds({"RUNPOD_API_KEY": "rp_test"})
    endpoint = build_balance_endpoint_for(cfg, creds)  # type: ignore[arg-type]
    assert isinstance(endpoint, RunPodBalanceEndpoint)


@pytest.mark.parametrize("provider", ["local", "skypilot", "unknown", ""])
def test_non_runpod_provider_returns_no_balance_endpoint(provider: str) -> None:
    cfg = _FakeCfg(
        compute=_FakeCompute(provider),
        engine=_FakeEngine("comfyui"),
    )
    creds = _FakeCreds({})
    endpoint = build_balance_endpoint_for(cfg, creds)  # type: ignore[arg-type]
    assert isinstance(endpoint, NoBalanceEndpoint)


@pytest.mark.parametrize(
    "engine_kind", ["replicate", "runway", "hosted", "fal", "bedrock_video"]
)
def test_hosted_engine_no_compute_returns_no_balance_endpoint(engine_kind: str) -> None:
    cfg = _FakeCfg(compute=None, engine=_FakeEngine(engine_kind))
    creds = _FakeCreds({})
    endpoint = build_balance_endpoint_for(cfg, creds)  # type: ignore[arg-type]
    assert isinstance(endpoint, NoBalanceEndpoint)


def test_runpod_with_missing_api_key_does_not_raise() -> None:
    """BUG CATCH: dispatch MUST NOT raise on missing cred — the satisfier's
    own None-short-circuit handles missing-cred rendering."""
    cfg = _FakeCfg(
        compute=_FakeCompute("runpod"),
        engine=_FakeEngine("comfyui"),
    )
    creds = _FakeCreds({})  # No RUNPOD_API_KEY
    endpoint = build_balance_endpoint_for(cfg, creds)  # type: ignore[arg-type]
    assert isinstance(endpoint, RunPodBalanceEndpoint)
    assert endpoint.read() is None
