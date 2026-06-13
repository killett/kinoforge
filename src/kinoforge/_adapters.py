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
import kinoforge.image_engines.replicate  # noqa: F401  # self-registers under "replicate"

# Output sinks
import kinoforge.outputs.local  # noqa: F401  side-effect: register "local" OutputSink
import kinoforge.providers.local  # noqa: F401
import kinoforge.providers.runpod  # noqa: F401
import kinoforge.providers.skypilot  # noqa: F401
import kinoforge.sources.civitai  # noqa: F401

# Sources
import kinoforge.sources.http  # noqa: F401
import kinoforge.sources.huggingface  # noqa: F401

# Stores
import kinoforge.stores.gcs  # noqa: F401
import kinoforge.stores.local  # noqa: F401
import kinoforge.stores.s3  # noqa: F401

# --------------------------------------------------------------------------
# Cross-provider dispatch helpers (live here because they import concrete
# provider modules — disallowed everywhere else in kinoforge.core).
# --------------------------------------------------------------------------

if TYPE_CHECKING:
    from kinoforge.core.balance_endpoints import BalanceEndpoint
    from kinoforge.core.config import Config
    from kinoforge.core.heartbeat_endpoints import HeartbeatEndpoint
    from kinoforge.core.interfaces import CredentialProvider


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
