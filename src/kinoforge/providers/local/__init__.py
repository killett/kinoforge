"""In-process LocalProvider: simulates instances without any cloud account.

Self-registers under ``"local"`` when this module is imported.  Inject a
``FakeClock`` for deterministic lifecycle / cost-safety testing.
"""

from __future__ import annotations

import uuid

from kinoforge.core import registry
from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.interfaces import (
    ComputeProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)
from kinoforge.core.offers import filter_offers

# ---------------------------------------------------------------------------
# Synthetic offer catalogue
# ---------------------------------------------------------------------------

_SYNTHETIC_OFFERS: list[Offer] = [
    Offer(
        id="local-1",
        gpu_type="LOCAL",
        vram_gb=80,
        cuda="12.8",
        cost_rate_usd_per_hr=0.0,
        mode="pod",
    ),
    Offer(
        id="local-2",
        gpu_type="LOCAL",
        vram_gb=48,
        cuda="12.8",
        cost_rate_usd_per_hr=0.0,
        mode="pod",
    ),
]


# ---------------------------------------------------------------------------
# LocalProvider
# ---------------------------------------------------------------------------


class LocalProvider(ComputeProvider):
    """In-process compute provider; no network or cloud account required.

    All instances are simulated in-process.  Time is sourced from the injected
    ``Clock`` so cost-safety tests can step time deterministically.

    Args:
        clock: Wall-clock source.  Defaults to ``RealClock()`` when ``None``.

    Example:
        >>> from kinoforge.core.clock import FakeClock
        >>> from kinoforge.core.interfaces import InstanceSpec
        >>> p = LocalProvider(clock=FakeClock(start=0.0))
        >>> inst = p.create_instance(InstanceSpec(image="test"))
        >>> inst.status
        'ready'
    """

    name: str = "local"

    def __init__(self, clock: Clock | None = None) -> None:
        """Initialise the provider.

        Args:
            clock: Injectable clock; ``RealClock()`` is used when ``None``.
        """
        self._clock: Clock = clock if clock is not None else RealClock()
        self._instances: dict[str, Instance] = {}
        self._heartbeats: dict[str, float] = {}

    # ------------------------------------------------------------------
    # ComputeProvider interface
    # ------------------------------------------------------------------

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        """Return synthetic local offers that satisfy ``reqs``.

        Delegates filtering to ``filter_offers`` so callers' hardware
        overrides are fully respected.

        Args:
            reqs: Hardware requirements to filter against.

        Returns:
            Filtered (and sorted) list of ``Offer`` objects.
        """
        return filter_offers(_SYNTHETIC_OFFERS, reqs)

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Create and record a new in-process instance.

        Args:
            spec: Instance specification (image, lifecycle, tags, …).

        Returns:
            A newly created ``Instance`` with ``status == "ready"`` and
            ``created_at`` set from the injected clock.
        """
        instance_id = f"local-{uuid.uuid4().hex[:8]}"
        instance = Instance(
            id=instance_id,
            provider=self.name,
            status="ready",
            created_at=self._clock.now(),
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=0.0,
        )
        self._instances[instance_id] = instance
        return instance

    def get_instance(self, instance_id: str) -> Instance:
        """Return the instance for ``instance_id`` or raise ``KeyError``.

        Args:
            instance_id: The instance identifier to look up.

        Returns:
            The stored ``Instance``.

        Raises:
            KeyError: No instance is registered under ``instance_id``.
        """
        return self._instances[instance_id]

    def list_instances(self) -> list[Instance]:
        """Return all live (non-destroyed) instances.

        Returns:
            A list of ``Instance`` objects in insertion order.
        """
        return list(self._instances.values())

    def stop_instance(self, instance_id: str) -> None:
        """Transition an instance to ``"stopped"`` status.

        Args:
            instance_id: The instance to stop.

        Raises:
            KeyError: No instance registered under ``instance_id``.
        """
        self._instances[instance_id].status = "stopped"

    def destroy_instance(self, instance_id: str) -> None:
        """Remove an instance from the registry (idempotent).

        Calling this method a second time for the same ``instance_id`` is safe
        and does not raise.

        Args:
            instance_id: The instance to destroy.
        """
        self._instances.pop(instance_id, None)
        self._heartbeats.pop(instance_id, None)

    def heartbeat(self, instance_id: str) -> None:
        """Record the current clock time as the last heartbeat for an instance.

        Args:
            instance_id: The instance that sent a heartbeat.
        """
        self._heartbeats[instance_id] = self._clock.now()

    def endpoints(self, instance: Instance) -> dict[str, str]:
        """Return the endpoint map for ``instance``.

        Args:
            instance: The instance whose endpoints to return.

        Returns:
            ``{"generate": "local://<instance.id>"}``
        """
        return {"generate": f"local://{instance.id}"}

    # ------------------------------------------------------------------
    # Extra accessor (not on base interface — exposed for tests / T17/18)
    # ------------------------------------------------------------------

    def last_heartbeat(self, instance_id: str) -> float | None:
        """Return the last recorded heartbeat time, or ``None`` if never set.

        Args:
            instance_id: The instance to query.

        Returns:
            The last heartbeat timestamp (seconds since Unix epoch) or
            ``None`` if ``heartbeat()`` has not yet been called for this
            instance.
        """
        return self._heartbeats.get(instance_id)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

registry.register_provider("local", lambda: LocalProvider())
