"""Provider-agnostic substrate for orchestrator-side heartbeat I/O (B5a).

This module hosts the cross-provider Protocol and capability gate that the
Layer U HeartbeatLoop's provider-side delegation rests on. Concrete
satisfiers live under ``kinoforge.providers.<name>.heartbeat``; this module
must never import them (core-import-ban invariant — see
PROGRESS.md §"Key decisions").

The B5a-shipped set in :data:`_HEARTBEAT_SUPPORTED` is the source of truth
for which providers have a wire-level satisfier. B5b adds ``"skypilot"`` in
one line; downstream consumers (Layer V classify, B1 sweeper, B3 warm-reuse)
gate destructive verdicts via :func:`provider_heartbeat_supported`.
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


# B5a-shipped set. Membership means "a heartbeat substrate is available
# for this provider", not "a wire-level write satisfier ships for this
# provider". Specifically for ``"runpod"``: post-C33 the wire-level
# write substrate (RunPodGraphQLHeartbeatEndpoint.write) is a no-op,
# and the local Ledger serves as the same-host substrate per the B5b
# deferral spec (docs/superpowers/specs/2026-06-18-b5b-deferred-design.md).
# Downstream consumers consult this via provider_heartbeat_supported()
# before treating HEARTBEAT_UNKNOWN as actionable on cloud providers;
# removing ``"runpod"`` would cascade into HEARTBEAT_SUBSTRATE_MISSING
# verdicts from reaper.classify for every RunPod pod — a behaviour
# regression with no compensating safety win.
#
# Future B5b resumption (cross-machine scope) would add ``"skypilot"``
# here once a satisfier ships.
_HEARTBEAT_SUPPORTED: frozenset[str] = frozenset({"local", "runpod"})


def provider_heartbeat_supported(provider_kind: str) -> bool:
    """Whether a wire-level :class:`HeartbeatEndpoint` ships for ``provider_kind``.

    Used by :func:`kinoforge.core.reaper.classify` to emit the new
    ``HEARTBEAT_SUBSTRATE_MISSING`` verdict on providers whose substrate
    has not yet shipped (e.g. SkyPilot pre-B5b). Consumers
    (:func:`kinoforge.core.reaper_actor.act_on_verdict`) hard-pin that
    verdict to no-destroy + WARN-once.

    Args:
        provider_kind: The ``compute.provider`` field value
            (``"local"`` / ``"runpod"`` / ``"skypilot"`` / ...).

    Returns:
        ``True`` when a wire-level :class:`HeartbeatEndpoint` satisfier
        is shipped for this provider; ``False`` otherwise.
    """
    return provider_kind in _HEARTBEAT_SUPPORTED
