"""Provider-agnostic substrate for orchestrator-side util sampling (C26).

Hosts the cross-provider Protocol + capability gate that HeartbeatLoop's
per-tick util read rests on. Concrete satisfiers live under
``kinoforge.providers.<name>.util``; this module must never import them
(core-import-ban invariant).

The C26-shipped set in :data:`_UTIL_SUPPORTED` mirrors B5a's
``_HEARTBEAT_SUPPORTED``. Downstream consumers (Layer V classify,
``_adapters.build_util_endpoint_for``) gate util-aware behaviour via
:func:`provider_util_supported`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["UtilSnapshot", "UtilSnapshotEndpoint", "provider_util_supported"]


@dataclass(frozen=True)
class UtilSnapshot:
    """Per-tick provider-side resource metrics.

    All fields Optional — providers surface different subsets. classify()
    treats ``None`` as 'data unavailable' (does not contribute to STALL
    AND-clause).

    Attributes:
        gpu_util_percent: MAX across pod's GPU devices.
        cpu_percent: Container CPU percentage.
        memory_percent: Container memory percentage.
        disk_percent: Container disk percentage (None if provider does not surface).
        uptime_seconds: Container uptime; decrease tick-over-tick = restart blip.
    """

    gpu_util_percent: float | None
    cpu_percent: float | None
    memory_percent: float | None
    disk_percent: float | None
    uptime_seconds: int | None


@runtime_checkable
class UtilSnapshotEndpoint(Protocol):
    """Provider-agnostic substrate for util sampling.

    Contract invariants (every satisfier honours):

    - ``read_util(id)`` returns ``None`` when the instance is gone, the
      storage slot was never written, or all upstream fields are
      unavailable.
    - Transport failures (HTTP non-2xx, GraphQL rate-limit, SSH refused)
      propagate as
      :class:`~kinoforge.core.errors.TransportError`. Consumers tolerate.
    - ``read_util`` is idempotent and side-effect-free (no provider-side
      mutation, no ledger writes).
    """

    def read_util(self, instance_id: str) -> UtilSnapshot | None:
        """Return a snapshot for ``instance_id`` or ``None`` (see class invariants)."""
        ...


_UTIL_SUPPORTED: frozenset[str] = frozenset({"local", "modal", "runpod"})


def provider_util_supported(provider_kind: str) -> bool:
    """Whether a wire-level :class:`UtilSnapshotEndpoint` satisfier ships.

    Args:
        provider_kind: The ``compute.provider`` field value.

    Returns:
        ``True`` when a wire-level satisfier is shipped; ``False`` otherwise.
    """
    return provider_kind in _UTIL_SUPPORTED
