"""Adapter-dispatch tests — cfg-aware provider build.

Verifies that :func:`kinoforge._adapters.build_provider_for` threads
``compute.backend_options.skypilot`` into :class:`SkyPilotProvider` at
instantiation time so ``sky.launch`` receives the operator-pinned cloud
filter (and ``retry_until_up``) instead of falling through to whichever
enabled cloud has the cheapest matching SKU. The pin lived at the portable
``compute.cloud`` before compute-seam S1.
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
    PlacementConfig,
)
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.skypilot import SkyPilotProvider


def _make_cfg(
    *,
    provider: str = "skypilot",
    cloud: list[str] | None = None,
    retry_until_up: bool | None = None,
    region: str | None = None,
) -> Config:
    skypilot: dict[str, object] = {}
    if cloud is not None:
        skypilot["clouds"] = cloud
    if retry_until_up is not None:
        skypilot["retry_until_up"] = retry_until_up
    return Config(
        compute=ComputeConfig(
            provider=provider,
            image="skypilot/skypilot-gpu:latest",
            lifecycle=LifecycleConfig(budget=10.0),
            placement=PlacementConfig(region=region),
            backend_options={"skypilot": skypilot} if skypilot else {},
        ),
        engine=EngineConfig(kind="fake", precision="fp16"),
        models=[
            ModelEntry(
                kind="base", ref="hf:fake/repo:weights.bin", target="checkpoints"
            )
        ],
    )


def test_build_provider_for_skypilot_threads_single_cloud() -> None:
    """clouds = ["lambda"] → SkyPilotProvider._clouds == ["lambda"].

    Without this thread, sky considers every enabled cloud and Vast.ai
    wins on price; the resume target spec calls this out as the entire
    reason the pin exists.
    """
    cfg = _make_cfg(cloud=["lambda"])
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)
    assert provider._clouds == ["lambda"]


def test_build_provider_for_skypilot_threads_multi_cloud() -> None:
    """clouds = ["lambda", "vast"] → both pinned for sky fallthrough.

    Bug guard: a single-string-only path would silently drop the second
    cloud and break the Lambda-capacity-falls-through-to-Vast contract.
    """
    cfg = _make_cfg(cloud=["lambda", "vast"])
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)
    assert provider._clouds == ["lambda", "vast"]


def test_build_provider_for_skypilot_cloud_none_preserves_legacy() -> None:
    """No skypilot namespace → SkyPilotProvider._clouds is None.

    Backward compat: YAMLs without a cloud pin MUST keep
    sky.list_accelerators(clouds=) unset.
    """
    cfg = _make_cfg(cloud=None)
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)
    assert provider._clouds is None


def test_build_provider_for_local_ignores_cloud() -> None:
    """Non-skypilot providers must not reach the skypilot injection branch.

    The cloud pin is a skypilot-only knob; LocalProvider doesn't accept
    clouds=. The dispatcher must short-circuit before reaching it.
    """
    cfg = _make_cfg(provider="local", cloud=None)
    provider = build_provider_for(cfg)
    assert isinstance(provider, LocalProvider)


def test_build_provider_for_skypilot_threads_retry_until_up() -> None:
    """retry_until_up reaches SkyPilotProvider._retry_until_up.

    Bug caught (verification finding F6): the flag exists on the provider
    constructor but nothing maps config onto it, so an operator asking sky
    to keep retrying across zones silently gets a single-shot launch.
    """
    provider = build_provider_for(_make_cfg(cloud=["lambda"], retry_until_up=True))
    assert isinstance(provider, SkyPilotProvider)
    assert provider._retry_until_up is True


def test_build_provider_for_skypilot_retry_until_up_defaults_off() -> None:
    """No namespace → retry_until_up stays False (pre-S1 launch behaviour)."""
    provider = build_provider_for(_make_cfg())
    assert isinstance(provider, SkyPilotProvider)
    assert provider._retry_until_up is False


def test_build_provider_for_skypilot_threads_region() -> None:
    """compute.placement.region reaches SkyPilotProvider._region.

    Bug caught (the other half of verification finding F6): the constructor
    knob existed and no YAML could reach it, so sky's optimizer picked the
    region by quota — asia-southeast1 has been observed on a project whose
    standing rule is Oregon.
    """
    provider = build_provider_for(_make_cfg(cloud=["aws"], region="us-west-2"))
    assert isinstance(provider, SkyPilotProvider)
    assert provider._region == "us-west-2"


def test_build_provider_for_skypilot_region_defaults_to_none() -> None:
    """No region in the cfg leaves the optimizer free, as before S2.

    Bug caught: pinning a default here would relocate every existing
    skypilot config in one commit, silently.
    """
    provider = build_provider_for(_make_cfg(cloud=["aws"]))
    assert isinstance(provider, SkyPilotProvider)
    assert provider._region is None


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
