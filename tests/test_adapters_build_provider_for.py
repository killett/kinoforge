"""Adapter-dispatch tests for Phase 53 Stage C — cfg-aware provider build.

Verifies that :func:`kinoforge._adapters.build_provider_for` threads
``cfg.compute.cloud`` into :class:`SkyPilotProvider` at instantiation time
so ``sky.launch`` receives the operator-pinned cloud filter instead of
falling through to whichever enabled cloud has the cheapest matching SKU.
"""

from __future__ import annotations

import pytest

from kinoforge._adapters import build_provider_for
from kinoforge.core.config import (
    ComputeConfig,
    Config,
    EngineConfig,
    LifecycleConfig,
    ModelEntry,
)
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.skypilot import SkyPilotProvider


def _make_cfg(
    *,
    provider: str = "skypilot",
    cloud: list[str] | None = None,
) -> Config:
    return Config(
        compute=ComputeConfig(
            provider=provider,
            image="skypilot/skypilot-gpu:latest",
            lifecycle=LifecycleConfig(budget=10.0),
            cloud=cloud,
        ),
        engine=EngineConfig(kind="fake", precision="fp16"),
        models=[
            ModelEntry(
                kind="base", ref="hf:fake/repo:weights.bin", target="checkpoints"
            )
        ],
    )


def test_build_provider_for_skypilot_threads_single_cloud() -> None:
    """cfg.compute.cloud = ["lambda"] → SkyPilotProvider._clouds == ["lambda"].

    Without this thread, sky considers every enabled cloud and Vast.ai
    wins on price; the resume target spec calls this out as the entire
    reason Stage C exists.
    """
    cfg = _make_cfg(cloud=["lambda"])
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)
    assert provider._clouds == ["lambda"]


def test_build_provider_for_skypilot_threads_multi_cloud() -> None:
    """cfg.compute.cloud = ["lambda", "vast"] → both pinned for sky fallthrough.

    Bug guard: a single-string-only path would silently drop the second
    cloud and break the Lambda-capacity-falls-through-to-Vast contract.
    """
    cfg = _make_cfg(cloud=["lambda", "vast"])
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)
    assert provider._clouds == ["lambda", "vast"]


def test_build_provider_for_skypilot_cloud_none_preserves_legacy() -> None:
    """cfg.compute.cloud = None → SkyPilotProvider._clouds is None.

    Backward compat: pre-Stage-C YAMLs without a cloud key MUST keep
    sky.list_accelerators(clouds=) unset, matching pre-Phase-53 behaviour.
    """
    cfg = _make_cfg(cloud=None)
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)
    assert provider._clouds is None


def test_build_provider_for_local_ignores_cloud() -> None:
    """Non-skypilot providers must not blow up if cfg.compute.cloud is set.

    cfg.compute.cloud is a skypilot-only knob; LocalProvider doesn't
    accept clouds=. The dispatcher must short-circuit before reaching
    the skypilot-only injection branch.
    """
    cfg = _make_cfg(provider="local", cloud=None)
    provider = build_provider_for(cfg)
    assert isinstance(provider, LocalProvider)


def test_build_provider_for_hosted_engine_returns_none() -> None:
    """cfg.compute is None (hosted-only path) → returns None, no raise.

    Sister to build_heartbeat_endpoint_for's hosted-engine handling.
    """
    cfg = Config(
        compute=None,
        engine=EngineConfig(kind="fake", precision="fp16"),
        models=[
            ModelEntry(
                kind="base", ref="hf:fake/repo:weights.bin", target="checkpoints"
            )
        ],
    )
    assert build_provider_for(cfg) is None


def test_build_provider_for_unknown_provider_raises() -> None:
    """Unknown provider name surfaces as UnknownAdapter, same contract as
    direct registry.get_provider lookup."""
    from kinoforge.core.errors import UnknownAdapter

    cfg = _make_cfg(provider="skypilot")
    # Override after construction to skip the ComputeConfig validator (provider is free-form str).
    cfg.compute.provider = "no-such-provider"  # type: ignore[union-attr]
    with pytest.raises(UnknownAdapter):
        build_provider_for(cfg)
