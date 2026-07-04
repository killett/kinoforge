"""Adapter self-registration hub + provider-aware dispatch helpers.

This is the SOLE module in the kinoforge package that imports concrete adapter
implementations.  Every import here triggers the adapter's self-registration
call, making it visible to the registry under its declared name or scheme.

Core and the CLI MUST NOT import concrete adapters directly — they go through
the registry (``kinoforge.core.registry``).  This module is the one permitted
exception: it wires all adapters in one place so the rest of the codebase
stays agnostic of concrete implementations.

Usage::

    import kinoforge._adapters  # noqa: F401

Importing this module is side-effect-only for self-registration; this
module also exposes a small set of cross-provider dispatch helpers
(:func:`build_heartbeat_endpoint_for`) that need to import concrete
providers and therefore cannot live in core.
"""

from typing import TYPE_CHECKING

# Providers
import kinoforge.engines.bedrock_video  # noqa: F401  # self-registers under "bedrock_video"
import kinoforge.engines.comfyui  # noqa: F401
import kinoforge.engines.diffusers  # noqa: F401

# Engines
import kinoforge.engines.fake  # noqa: F401
import kinoforge.engines.fal  # noqa: F401
import kinoforge.engines.hosted  # noqa: F401
import kinoforge.engines.replicate  # noqa: F401  # self-registers under "replicate"
import kinoforge.engines.runway  # noqa: F401  # self-registers under "runway"

# Image engines
import kinoforge.image_engines.fake  # noqa: F401  # self-registers under "fake"
import kinoforge.image_engines.fal  # noqa: F401  # self-registers under "fal"
import kinoforge.image_engines.luma_agents  # noqa: F401  # self-registers under "luma_agents"
import kinoforge.image_engines.replicate  # noqa: F401  # self-registers under "replicate"

# Output sinks
import kinoforge.outputs.local  # noqa: F401  side-effect: register "local" OutputSink
import kinoforge.providers.local  # noqa: F401
import kinoforge.providers.runpod  # noqa: F401
import kinoforge.providers.skypilot  # noqa: F401
import kinoforge.sources.civarchive  # noqa: F401
import kinoforge.sources.civitai  # noqa: F401

# Sources
import kinoforge.sources.http  # noqa: F401
import kinoforge.sources.huggingface  # noqa: F401

# Stores
import kinoforge.stores.gcs  # noqa: F401
import kinoforge.stores.local  # noqa: F401
import kinoforge.stores.s3  # noqa: F401

# Upscalers
import kinoforge.upscalers.flashvsr  # noqa: F401  # self-registers under "flashvsr" (v1 default diffusion VSR)
import kinoforge.upscalers.seedvr2  # noqa: F401  # self-registers under "seedvr2" (extras-stub until Phase 2)
import kinoforge.upscalers.spandrel  # noqa: F401  # self-registers under "spandrel"

# --------------------------------------------------------------------------
# Cross-provider dispatch helpers (live here because they import concrete
# provider modules — disallowed everywhere else in kinoforge.core).
# --------------------------------------------------------------------------

if TYPE_CHECKING:
    from kinoforge.core.balance_endpoints import BalanceEndpoint
    from kinoforge.core.config import Config
    from kinoforge.core.heartbeat_endpoints import HeartbeatEndpoint
    from kinoforge.core.interfaces import ComputeProvider, CredentialProvider
    from kinoforge.core.lora import LoraEntry
    from kinoforge.core.util_endpoints import UtilSnapshotEndpoint
    from kinoforge.engines.diffusers.servers.wan_t2v_server import (
        ArtifactDownloadSpec,
        SetStackRequest,
    )


def build_provider_for(cfg: "Config") -> "ComputeProvider | None":
    """Resolve cfg.compute.provider to a :class:`ComputeProvider`, cfg-aware.

    Wraps :func:`kinoforge.core.registry.get_provider` and applies
    provider-specific cfg injection that the zero-arg registry factory
    cannot do on its own. Today the only such knob is
    ``cfg.compute.cloud`` for skypilot (Phase 53 Stage C) — pinned onto
    :attr:`SkyPilotProvider._clouds` so ``sky.list_accelerators`` /
    ``sky.launch`` receive a ``clouds=`` filter and the operator's
    Lambda/Vast/etc. pin is honoured.

    Lives here (not in core) for the same reason as
    :func:`build_heartbeat_endpoint_for`: it must import a concrete
    provider class to satisfy ``isinstance`` / attribute assignment,
    and ``kinoforge.core.*`` is forbidden from importing
    ``kinoforge.providers.*`` per the core-import-ban invariant.

    Args:
        cfg: The loaded kinoforge config.

    Returns:
        A :class:`ComputeProvider` instance, or ``None`` when
        ``cfg.compute is None`` (hosted-only path).

    Raises:
        UnknownAdapter: ``cfg.compute.provider`` is not registered.
    """
    from kinoforge.core import registry

    if cfg.compute is None:
        return None
    provider = registry.get_provider(cfg.compute.provider)()
    if cfg.compute.provider == "skypilot" and cfg.compute.cloud is not None:
        from kinoforge.providers.skypilot import SkyPilotProvider

        if not isinstance(provider, SkyPilotProvider):
            raise TypeError(
                f"registry returned {type(provider).__name__} for 'skypilot'; "
                "cannot pin cfg.compute.cloud onto a non-SkyPilotProvider"
            )
        provider._clouds = list(cfg.compute.cloud)
    return provider


def build_heartbeat_endpoint_for(
    cfg: "Config",
    creds: "CredentialProvider",
) -> "HeartbeatEndpoint | None":
    """Build the right :class:`HeartbeatEndpoint` for the configured provider.

    Dispatches on ``(cfg.compute.provider, cfg.compute.heartbeat_mode)``.
    Lives in ``_adapters.py`` because it must import the concrete provider
    satisfier modules (``providers/runpod/heartbeat.py``, etc.), which
    ``kinoforge.core.*`` is forbidden from doing per the core-import-ban
    invariant.

    Args:
        cfg: The loaded kinoforge config (must have a ``compute`` block).
        creds: Credential provider that yields ``RUNPOD_API_KEY`` /
            other provider-specific keys.

    Returns:
        A :class:`HeartbeatEndpoint` instance, or ``None`` when the
        operator selected ``heartbeat_mode = "none"`` (backward-compatible
        no-op heartbeat path).

    Raises:
        AuthError: Mode requires a credential that is not set
            (e.g. ``graphql-tag`` without ``RUNPOD_API_KEY``).
        ValidationError: The (provider, mode) pair is incompatible
            (e.g. RunPod with ``ssh-touch``, which is SkyPilot-only).
    """
    from kinoforge.core.errors import AuthError, ValidationError

    if cfg.compute is None:
        return None
    mode = cfg.compute.heartbeat_mode
    if mode == "none":
        return None
    provider = cfg.compute.provider
    if provider == "runpod":
        if mode == "graphql-tag":
            api_key = creds.get("RUNPOD_API_KEY")
            if api_key is None:
                raise AuthError(
                    "RUNPOD_API_KEY must be set when "
                    "compute.heartbeat_mode == 'graphql-tag'"
                )
            from kinoforge.providers.runpod.heartbeat import (
                RunPodGraphQLHeartbeatEndpoint,
            )

            return RunPodGraphQLHeartbeatEndpoint(api_key=api_key)
        raise ValidationError(
            f"runpod does not support compute.heartbeat_mode={mode!r}; "
            "valid values for runpod: 'none', 'graphql-tag'"
        )
    if provider == "skypilot":
        raise ValidationError(
            f"skypilot heartbeat substrate ships in B5b "
            f"(compute.heartbeat_mode={mode!r}); set to 'none' for now"
        )
    if provider == "local":
        # LocalProvider's in-memory _heartbeats dict already covers
        # local-mode tests; no separate substrate satisfier needed.
        return None
    raise ValidationError(f"unknown provider for heartbeat dispatch: {provider!r}")


def build_balance_endpoint_for(
    cfg: "Config",
    creds: "CredentialProvider",
) -> "BalanceEndpoint":
    """Build the right :class:`BalanceEndpoint` for the configured provider.

    Sister to :func:`build_heartbeat_endpoint_for` but with a different
    failure contract: this helper NEVER raises ``AuthError`` /
    ``ValidationError`` on lookup. Missing-cred / unknown-provider cases
    fall through to :class:`NoBalanceEndpoint`, whose ``read()`` returns
    ``None`` so the cost-render path stays free of provider-dispatch
    failures.

    Args:
        cfg: The loaded kinoforge config.
        creds: Credential provider; the RunPod branch reads
            ``RUNPOD_API_KEY``.

    Returns:
        A :class:`BalanceEndpoint`. RunPod kind → satisfier; everything
        else → :class:`NoBalanceEndpoint`. Hosted engines (no ``compute``
        block) also resolve to :class:`NoBalanceEndpoint`.
    """
    from kinoforge.core.balance_endpoints import NoBalanceEndpoint

    if cfg.compute is None:
        return NoBalanceEndpoint()
    provider = cfg.compute.provider
    if provider == "runpod":
        from kinoforge.providers.runpod.balance import RunPodBalanceEndpoint

        return RunPodBalanceEndpoint(api_key=creds.get("RUNPOD_API_KEY"))
    return NoBalanceEndpoint()


def build_util_endpoint_for(
    cfg: "Config",
    creds: "CredentialProvider",
) -> "UtilSnapshotEndpoint | None":
    """Build the right :class:`UtilSnapshotEndpoint` for the configured provider.

    Returns None when:
      - cfg.compute is None (hosted-only path), OR
      - BOTH stall_reap_enabled AND restart_loop_reap_enabled are False
        on cfg.compute.lifecycle (full kill switch — neither util-aware
        predicate is active so the sampler has no consumer), OR
      - provider_util_supported(cfg.compute.provider) is False
        (e.g. SkyPilot pre-B5b; Bedrock).

    Args:
        cfg: The loaded kinoforge config.
        creds: Credential provider; RunPod branch reads ``RUNPOD_API_KEY``.

    Returns:
        A :class:`UtilSnapshotEndpoint` instance, or ``None``.

    Raises:
        AuthError: RunPod branch requested but ``RUNPOD_API_KEY`` missing.
    """
    from kinoforge.core.errors import AuthError
    from kinoforge.core.util_endpoints import provider_util_supported

    if cfg.compute is None:
        return None
    lifecycle = cfg.compute.lifecycle
    if (
        lifecycle is not None
        and not lifecycle.stall_reap_enabled
        and not lifecycle.restart_loop_reap_enabled
    ):
        return None
    provider = cfg.compute.provider
    if not provider_util_supported(provider):
        return None
    if provider == "runpod":
        api_key = creds.get("RUNPOD_API_KEY")
        if api_key is None:
            raise AuthError(
                "RUNPOD_API_KEY must be set when stall_reap_enabled or "
                "restart_loop_reap_enabled is true on runpod"
            )
        from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

        return RunPodGraphQLUtilEndpoint(api_key=api_key)
    if provider == "local":
        from kinoforge.providers.local.util import LocalUtilEndpoint

        return LocalUtilEndpoint()
    return None


def build_set_stack_request(
    active_stack: "list[LoraEntry]",
    *,
    download_specs: "dict[str, ArtifactDownloadSpec]",
) -> "SetStackRequest":
    """Adapt a resolved LoRA stack to the server's request schema.

    Bridges the :mod:`kinoforge.core.lora` schema (``LoraEntry``) and
    the pod-side server schema (``LoraTarget``). Two distinct Pydantic
    models on purpose (P1 spec §6.3): server runs in a slim pod env
    without ``kinoforge.core`` available, so the wire format is its own
    contract.

    See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §9.2.

    Args:
        active_stack: Ordered LoRA list resolved by
            :func:`kinoforge.core.lora.resolve_active_lora_stack`.
        download_specs: Per-ref download metadata for any ref the pod
            does not yet have on disk. Empty when every ref is already
            in the pod's inventory.

    Returns:
        A :class:`SetStackRequest` ready to POST to ``/lora/set_stack``.
    """
    from kinoforge.engines.diffusers.servers.wan_t2v_server import (
        LoraTarget,
        SetStackRequest,
    )

    return SetStackRequest(
        target=[LoraTarget(ref=lo.ref, strength=lo.strength) for lo in active_stack],
        download_specs=download_specs,
    )
