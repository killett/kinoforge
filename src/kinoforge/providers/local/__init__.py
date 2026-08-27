"""In-process LocalProvider: simulates instances without any cloud account.

Self-registers under ``"local"`` when this module is imported.  Inject a
``FakeClock`` for deterministic lifecycle / cost-safety testing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from kinoforge.core import registry
from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.interfaces import (
    ComputeProvider,
    FieldSupport,
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

    billed: ClassVar[bool] = False

    class Options(BaseModel):
        """LocalProvider accepts no backend options today — still forbids extras.

        An empty model is a real declaration ("this provider accepts no
        backend options"), not a placeholder for one that was never written.
        """

        model_config = ConfigDict(extra="forbid")

    @classmethod
    def validate_options(cls, raw: Mapping[str, Any]) -> LocalProvider.Options:
        """Parse *raw* into this provider's Options, forbidding unknown keys.

        Args:
            raw: The ``compute.backend_options["local"]`` mapping.

        Returns:
            A validated :class:`LocalProvider.Options`.
        """
        return cls.Options.model_validate(dict(raw))

    @classmethod
    def capabilities(
        cls, shape: WorkloadShape = WorkloadShape.SERVER
    ) -> frozenset[Capability]:
        """In-process provider: real heartbeat dict, real stop, scripted util.

        UTIL_SNAPSHOT is ``providers/local/util.py`` — a scripted in-process
        seam, not a measurement. It stays declared because the endpoint does
        return snapshots and LocalProvider is unbilled, so no money decision
        rides on it.
        """
        return frozenset(
            {
                Capability.HEARTBEAT_READ,
                Capability.UTIL_SNAPSHOT,
                Capability.PAUSE_BILLING,
            }
        )

    @classmethod
    def consumes(cls) -> Mapping[str, FieldSupport]:
        """Declare the near-total non-consumption this provider is honest about.

        :meth:`create_instance` reads exactly one field of the spec —
        ``tags`` — and fabricates everything else. No container is started,
        so ``image``, ``ports``, ``env``, ``run_cmd`` and both provision
        scripts are genuinely dropped (see
        ``tests/providers/test_local_ignores_provision_script.py``), and no
        clock guardrail runs, so ``lifecycle`` is dropped too. That is not a
        gap to be closed; it is what "local" means.

        The three CONSUMED selection fields come from :meth:`find_offers`,
        which passes them to :func:`~kinoforge.core.offers.filter_offers`
        over the synthetic catalog: a VRAM or CUDA floor above both entries
        empties the offer list, and a price ceiling below zero does the same
        (LocalProvider is unbilled, so a non-negative cap is satisfied
        vacuously — satisfied nonetheless).

        ``accelerators`` is UNSUPPORTED and the reason is worth stating:
        the preference list is passed to ``filter_offers``, but both
        synthetic offers share the ``"LOCAL"`` gpu_type, so no ordering an
        operator asks for can ever change the result.

        Returns:
            The declared field-support mapping.
        """
        c, u = FieldSupport.CONSUMED, FieldSupport.UNSUPPORTED
        return {
            # -- placement -------------------------------------------------
            "accelerators": u,  # one synthetic gpu_type; ranking is inert
            "accelerator_count": u,  # nothing is allocated
            "min_vram_gb": c,  # filter_offers excludes below the floor
            "min_cuda": c,  # filter_offers excludes below the floor
            "disk_gb": u,  # nothing is allocated
            "spot": u,  # nothing is allocated
            "max_usd_per_hr": c,  # filter_offers applies it; local is free
            # -- spec ------------------------------------------------------
            "image": u,  # no container is started
            "ports": u,  # nothing listens
            "volume_gb": u,  # no volume
            "volume_mount": u,  # no volume
            "env": u,  # no process to hand it to
            "tags": c,  # Instance.tags
            "run_id": u,  # the id is a fresh uuid4
            "provision_script": u,  # deliberately ignored
            "run_cmd": u,  # nothing is executed
            "image_build_script": u,  # Modal-only split
            "runtime_provision_script": u,  # Modal-only split
            "lifecycle": u,  # no guardrail runs in-process
            "offer": u,  # cost_rate is the literal 0.0
            "backend_options": u,  # Options is empty: no knob to consume
            "diagnostic_env": u,  # RunPod-only overlay
        }

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
    # Real-read override of ComputeProvider.last_heartbeat (default None)
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

registry.register_provider("local", lambda: LocalProvider(), LocalProvider)
