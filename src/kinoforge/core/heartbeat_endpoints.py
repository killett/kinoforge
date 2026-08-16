"""Provider-agnostic substrate for orchestrator-side heartbeat I/O (B5a).

This module hosts the cross-provider Protocol and capability gate that the
Layer U HeartbeatLoop's provider-side delegation rests on. Concrete
satisfiers live under ``kinoforge.providers.<name>.heartbeat``; this module
must never import them (core-import-ban invariant — see
PROGRESS.md §"Key decisions").

The source of truth for which providers have a wire-level satisfier is now
each provider's own declaration in ``core/capabilities.py`` (Brief 2):
:func:`provider_heartbeat_supported` derives its answer from
:func:`kinoforge.core.capabilities.capabilities_for` instead of an
independent string table. Downstream consumers (Layer V classify, B1
sweeper, B3 warm-reuse) gate destructive verdicts via
:func:`provider_heartbeat_supported`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = ["HeartbeatEndpoint", "provider_heartbeat_supported"]


@runtime_checkable
class HeartbeatEndpoint(Protocol):
    """Provider-agnostic substrate for orchestrator-side heartbeat I/O.

    Contract invariants (satisfied by every wire-level satisfier):

    - ``write(id, ts)`` is idempotent on duplicate ``ts`` (double-write
      same value is a no-op).
    - ``read(id)`` returns the most-recently-written ts for ``id``, or
      ``None`` if the instance is gone, the storage slot was never
      written, or the underlying side-channel was wiped.
    - ``read(id)`` precision is at least 1-second granularity. Sub-second
      precision is permitted but never required by consumers (Layer V
      dead-man window is ``heartbeat_interval_s * 3``, minimum ~30s).
    - Transport failures (HTTP non-2xx, SSH connection refused, GraphQL
      rate-limit) propagate as :class:`~kinoforge.core.errors.TransportError`
      from BOTH write and read.
    - ``read`` returning ``None`` is NOT a transport failure — it is a
      valid "never written / instance gone" answer.
    - ``ts_local`` is a timezone-aware datetime in local TZ per project
      memory ``feedback_local_timezone_only``. Satisfiers store-and-return
      the same TZ; round-trip preserves wall-clock.
    """

    def write(self, instance_id: str, ts_local: datetime) -> None:
        """Record ``ts_local`` as the most-recent heartbeat for ``instance_id``.

        Args:
            instance_id: Provider-local instance identifier.
            ts_local: Timezone-aware datetime in local TZ.

        Raises:
            TransportError: The underlying side-channel write failed.
        """
        ...

    def read(self, instance_id: str) -> datetime | None:
        """Return the most-recent heartbeat for ``instance_id``, or ``None``.

        Args:
            instance_id: Provider-local instance identifier.

        Returns:
            The most-recent written timestamp, or ``None`` if the instance
            is gone or the slot was never written.

        Raises:
            TransportError: The underlying side-channel read failed.
        """
        ...


def provider_heartbeat_supported(provider_kind: str) -> bool:
    """Return True iff ``provider_kind`` declares HEARTBEAT_READ.

    Used by :func:`kinoforge.core.reaper.classify` to emit the new
    ``HEARTBEAT_SUBSTRATE_MISSING`` verdict on providers whose substrate
    has not yet shipped (e.g. SkyPilot pre-B5b). Consumers
    (:func:`kinoforge.core.reaper_actor.act_on_verdict`) hard-pin that
    verdict to no-destroy + WARN-once.

    Derived from the provider's own declaration (Brief 2) rather than a
    string table, so a provider whose ``last_heartbeat`` is the inherited
    ``None`` default cannot be listed as supported.

    Args:
        provider_kind: The ``compute.provider`` field value
            (``"local"`` / ``"runpod"`` / ``"skypilot"`` / ...).

    Returns:
        ``True`` when a wire-level :class:`HeartbeatEndpoint` satisfier
        is shipped for this provider; ``False`` otherwise.
    """
    from kinoforge.core.capabilities import (  # noqa: PLC0415 — avoids an import cycle
        Capability,
        capabilities_for,
    )

    return Capability.HEARTBEAT_READ in capabilities_for(provider_kind)
