"""Adapter-dispatch tests for B5a heartbeat-endpoint construction.

Verifies the cross-provider dispatch function in _adapters.py that
:func:`kinoforge.core.orchestrator._resolve_provider` calls to build the
right HeartbeatEndpoint instance from cfg.compute.heartbeat_mode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from kinoforge._adapters import build_heartbeat_endpoint_for
from kinoforge.core.errors import AuthError, ValidationError
from kinoforge.core.interfaces import CredentialProvider

if TYPE_CHECKING:
    from kinoforge.core.config import Config


class _StubCreds(CredentialProvider):
    def __init__(self, mapping: dict[str, str | None] | None = None) -> None:
        self._mapping = mapping or {}

    def get(self, key: str) -> str | None:
        return self._mapping.get(key)


def _make_cfg(provider: str, heartbeat_mode: str, engine_kind: str = "fake") -> Config:
    from kinoforge.core.config import (
        ComputeConfig,
        Config,
        EngineConfig,
        LifecycleConfig,
        ModelEntry,
    )

    return Config(
        compute=ComputeConfig(
            provider=provider,
            image="runpod/base:latest",
            lifecycle=LifecycleConfig(budget=10.0),
            heartbeat_mode=heartbeat_mode,
        ),
        engine=EngineConfig(kind=engine_kind, precision="fp16"),
        models=[
            ModelEntry(
                kind="base", ref="hf:fake/repo:weights.bin", target="checkpoints"
            )
        ],
    )


def test_mode_none_returns_none() -> None:
    cfg = _make_cfg(provider="runpod", heartbeat_mode="none")
    assert build_heartbeat_endpoint_for(cfg, _StubCreds()) is None


def test_runpod_graphql_tag_builds_endpoint() -> None:
    cfg = _make_cfg(provider="runpod", heartbeat_mode="graphql-tag")
    creds = _StubCreds({"RUNPOD_API_KEY": "sk-fake"})
    from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint

    got = build_heartbeat_endpoint_for(cfg, creds)
    assert isinstance(got, RunPodGraphQLHeartbeatEndpoint)


def test_runpod_graphql_tag_raises_auth_error_when_key_missing() -> None:
    """Missing RUNPOD_API_KEY is a startup-time failure — operator must
    see the error before the orchestrator boots a real pod."""
    cfg = _make_cfg(provider="runpod", heartbeat_mode="graphql-tag")
    creds = _StubCreds({})  # no key
    with pytest.raises(AuthError, match="RUNPOD_API_KEY"):
        build_heartbeat_endpoint_for(cfg, creds)


def test_runpod_incompatible_mode_raises_validation_error() -> None:
    """RunPod does not accept ssh-touch (SkyPilot-only mode). Caught at
    dispatch, not at config-load (config-load doesn't know providers)."""
    cfg = _make_cfg(provider="runpod", heartbeat_mode="ssh-touch")
    with pytest.raises(ValidationError, match="runpod"):
        build_heartbeat_endpoint_for(cfg, _StubCreds({"RUNPOD_API_KEY": "sk-fake"}))


def test_skypilot_any_mode_other_than_none_raises_not_implemented() -> None:
    """B5b ships the skypilot satisfier. Pre-B5b, any non-none mode on
    SkyPilot must fail-loud rather than silently no-op (operator could
    be expecting heartbeat substrate that doesn't exist yet)."""
    cfg = _make_cfg(provider="skypilot", heartbeat_mode="ssh-touch")
    with pytest.raises(ValidationError, match="B5b"):
        build_heartbeat_endpoint_for(cfg, _StubCreds())


# C25 runtime guard tests ---------------------------------------------------


def test_runpod_graphql_tag_refuses_unsafe_engine() -> None:
    """C25 guard: comfyui uses provision_script via dockerArgs — pairing it
    with graphql-tag heartbeat mode would overwrite the selfterm script on
    every heartbeat tick.  The guard must fire BEFORE any pod is created."""
    cfg = _make_cfg(
        provider="runpod", heartbeat_mode="graphql-tag", engine_kind="comfyui"
    )
    creds = _StubCreds({"RUNPOD_API_KEY": "sk-fake"})
    with pytest.raises(ValidationError) as exc_info:
        build_heartbeat_endpoint_for(cfg, creds)
    msg = str(exc_info.value)
    assert "C25" in msg, f"Expected 'C25' in error message, got: {msg!r}"
    assert "§9" in msg, f"Expected '§9' in error message, got: {msg!r}"


def test_runpod_graphql_tag_allows_safe_engine() -> None:
    """C25 guard: engine.kind='fake' is in _RUNPOD_HEARTBEAT_SAFE_ENGINES and
    does not inject a provision_script, so graphql-tag mode is permitted."""
    cfg = _make_cfg(provider="runpod", heartbeat_mode="graphql-tag", engine_kind="fake")
    creds = _StubCreds({"RUNPOD_API_KEY": "sk-fake"})
    from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint

    got = build_heartbeat_endpoint_for(cfg, creds)
    assert isinstance(got, RunPodGraphQLHeartbeatEndpoint)
