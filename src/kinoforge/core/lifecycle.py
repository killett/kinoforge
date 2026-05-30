"""Lifecycle management: deadline math, warm reuse, graceful drain, dead-man liveness.

This module provides the orchestrator-level cost-safety layer for compute instances.
All time is sourced through the injected ``Clock`` so tests can step time deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kinoforge.core.clock import Clock
from kinoforge.core.interfaces import Instance, InstanceSpec, Lifecycle

if TYPE_CHECKING:
    from kinoforge.core.interfaces import ComputeProvider


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def effective_deadline(
    num_segments: int,
    job_timeout_s: float,
    time_buffer_s: float,
) -> float:
    """Return the effective deadline duration for a multi-segment job.

    This is a pure function: it does *not* add the current wall time.
    The caller turns it into an absolute deadline timestamp by adding
    ``clock.now()`` at dispatch time.

    Args:
        num_segments: Number of video segments to be generated.
        job_timeout_s: Per-segment timeout allowance in seconds.
        time_buffer_s: Fixed overhead buffer in seconds (start-up, teardown, etc.).

    Returns:
        Total deadline duration in seconds:
        ``num_segments * job_timeout_s + time_buffer_s``.

    Example:
        >>> effective_deadline(4, 30 * 60, 30 * 60)
        9000.0
    """
    return num_segments * job_timeout_s + time_buffer_s


# ---------------------------------------------------------------------------
# Per-instance state container
# ---------------------------------------------------------------------------


@dataclass
class _InstanceState:
    """Mutable lifecycle state for a single instance.

    Attributes:
        created_at: Unix epoch timestamp when the instance was created.
        idle_since: Timestamp when the last job finished; ``None`` mid-job or
            before the first job.
        in_flight_job: ``(job_id, deadline_timestamp)`` for the currently
            running job, or ``None`` when idle.
        _accepting_new_jobs: Whether new jobs may be dispatched.  Flipped to
            ``False`` when the instance is draining.
    """

    created_at: float
    idle_since: float | None = None
    in_flight_job: tuple[str, float] | None = None
    _accepting_new_jobs: bool = field(default=True)


# ---------------------------------------------------------------------------
# LifecycleManager
# ---------------------------------------------------------------------------


class LifecycleManager:
    """Per-instance lifecycle cost-safety wrapper for a compute provider.

    Tracks idle time, in-flight jobs, graceful drain, and dead-man liveness.
    The manager may track multiple instances (state is keyed by instance id).

    Args:
        provider: The compute provider that owns the instances.  Used to read
            last-heartbeat timestamps via ``provider.last_heartbeat(id)``.
        clock: Injectable clock.  Use ``FakeClock`` in tests.
        lifecycle: Guardrail configuration (timeouts, max lifetime).
        run_id: Opaque run identifier; carried for logging / correlation.

    Example:
        >>> from kinoforge.core.clock import FakeClock
        >>> from kinoforge.core.interfaces import Lifecycle
        >>> from kinoforge.providers.local import LocalProvider
        >>> clock = FakeClock(start=0.0)
        >>> provider = LocalProvider(clock=clock)
        >>> lc = Lifecycle(idle_timeout_s=7200)
        >>> manager = LifecycleManager(provider=provider, clock=clock, lifecycle=lc, run_id="r")
    """

    def __init__(
        self,
        provider: ComputeProvider,
        clock: Clock,
        lifecycle: Lifecycle,
        run_id: str,
    ) -> None:
        """Initialise the manager with provider, clock, and guardrails.

        Args:
            provider: Compute provider; used for heartbeat queries.
            clock: Wall-clock source.
            lifecycle: Guardrail configuration.
            run_id: Opaque correlation identifier.
        """
        self._provider = provider
        self._clock = clock
        self._lifecycle = lifecycle
        self._run_id = run_id
        self._states: dict[str, _InstanceState] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, instance_id: str, created_at: float) -> None:
        """Register a new instance with the manager.

        Must be called after ``provider.create_instance()`` to initialise
        per-instance lifecycle state.

        Args:
            instance_id: The instance identifier returned by the provider.
            created_at: The ``Instance.created_at`` timestamp (sourced from the
                clock at creation time).
        """
        self._states[instance_id] = _InstanceState(created_at=created_at)

    # ------------------------------------------------------------------
    # Job tracking
    # ------------------------------------------------------------------

    def start_job(
        self,
        instance_id: str,
        job_id: str,
        num_segments: int,
    ) -> None:
        """Record a new job starting on the given instance.

        Args:
            instance_id: The instance that will run the job.
            job_id: A unique identifier for this job.
            num_segments: Number of segments, used to compute the effective
                deadline via ``effective_deadline()``.

        Raises:
            RuntimeError: If the instance is draining and no longer accepts
                new jobs.
            KeyError: If ``instance_id`` was never registered.
        """
        state = self._states[instance_id]
        if not state._accepting_new_jobs:
            raise RuntimeError(
                f"instance {instance_id!r} is draining; no new jobs accepted"
            )
        deadline_ts = self._clock.now() + effective_deadline(
            num_segments,
            self._lifecycle.job_timeout_s,
            self._lifecycle.time_buffer_s,
        )
        state.in_flight_job = (job_id, deadline_ts)
        state.idle_since = None

    def finish_job(self, instance_id: str, job_id: str) -> None:
        """Record that a job has finished on the given instance.

        Clears ``in_flight_job`` and sets ``idle_since`` to the current clock
        time.

        Args:
            instance_id: The instance that ran the job.
            job_id: The job that completed (informational; not validated).
        """
        state = self._states[instance_id]
        state.in_flight_job = None
        state.idle_since = self._clock.now()

    # ------------------------------------------------------------------
    # Lifecycle predicates
    # ------------------------------------------------------------------

    def should_reap(self, instance_id: str) -> bool:
        """Return True when the instance is idle and has exceeded idle_timeout.

        Idle means ``in_flight_job is None`` AND ``idle_since is not None``
        (i.e. at least one job has completed).  Mid-job instances always
        return False.

        Args:
            instance_id: The instance to evaluate.

        Returns:
            ``True`` iff idle AND ``clock.now() - idle_since > idle_timeout_s``.
        """
        state = self._states[instance_id]
        if state.in_flight_job is not None:
            return False
        if state.idle_since is None:
            return False
        elapsed_idle = self._clock.now() - state.idle_since
        return elapsed_idle > self._lifecycle.idle_timeout_s

    def should_drain(self, instance_id: str) -> bool:
        """Return True when the instance has exceeded max_lifetime.

        Idempotent: once drained, repeated calls continue to return True and
        keep ``accepting_new_jobs`` False.  Does NOT terminate in-flight jobs
        (graceful drain — the instance finishes current work then tears down).

        Args:
            instance_id: The instance to evaluate.

        Returns:
            ``True`` iff ``clock.now() - created_at >= max_lifetime_s``.
        """
        state = self._states[instance_id]
        elapsed = self._clock.now() - state.created_at
        if elapsed >= self._lifecycle.max_lifetime_s:
            state._accepting_new_jobs = False
            return True
        return False

    def accepting_new_jobs(self, instance_id: str) -> bool:
        """Return whether the instance is still accepting new jobs.

        Args:
            instance_id: The instance to query.

        Returns:
            ``False`` once ``should_drain()`` has tripped; ``True`` otherwise.
        """
        return self._states[instance_id]._accepting_new_jobs

    def in_flight_job(self, instance_id: str) -> tuple[str, float] | None:
        """Return the current in-flight job tuple, or ``None`` if idle.

        Args:
            instance_id: The instance to query.

        Returns:
            ``(job_id, deadline_timestamp)`` or ``None``.
        """
        return self._states[instance_id].in_flight_job

    def is_liveness_OK(self, instance_id: str) -> bool:
        """Return True when the instance is considered live.

        An instance is live when **either**:

        * It has an in-flight job whose absolute deadline has not yet passed
          (the job is still supposed to be running), **or**
        * The time since the most recent heartbeat (or ``created_at`` if no
          heartbeat has been sent) is within ``2 * idle_timeout_s`` (dead-man
          window).

        Using ``created_at`` as the fallback avoids killing brand-new idle
        instances that have not yet had a chance to send a heartbeat.

        Args:
            instance_id: The instance to evaluate.

        Returns:
            ``True`` iff the instance passes at least one liveness condition.
        """
        state = self._states[instance_id]
        now = self._clock.now()

        # Condition 1: in-flight job under its effective deadline
        if state.in_flight_job is not None:
            _job_id, deadline_ts = state.in_flight_job
            if now <= deadline_ts:
                return True

        # Condition 2: dead-man heartbeat window
        raw_hb = self._provider.last_heartbeat(instance_id)  # type: ignore[attr-defined]
        last_signal = max(raw_hb if raw_hb is not None else 0.0, state.created_at)
        dead_man_window = 2 * self._lifecycle.idle_timeout_s
        return (now - last_signal) <= dead_man_window


# ---------------------------------------------------------------------------
# warm_reuse_or_create
# ---------------------------------------------------------------------------


def warm_reuse_or_create(
    provider: ComputeProvider,
    manager: LifecycleManager,
    instance_id_or_none: str | None,
    spec: InstanceSpec,
) -> str:
    """Return a live instance id, creating one if necessary.

    Decision logic:

    * If ``instance_id_or_none`` is ``None`` → always create a new instance.
    * Else if ``manager.should_reap(id)`` is True → destroy the old instance
      and create a new one (old id is no longer valid after this call).
    * Else → return the existing id unchanged (warm reuse).

    The returned id is always registered with ``manager``.

    Args:
        provider: Compute provider used to create/destroy instances.
        manager: The lifecycle manager; new instances are registered here.
        instance_id_or_none: Existing instance id or ``None``.
        spec: Spec used if a new instance must be created.

    Returns:
        A live instance id (may be new or reused).
    """
    if instance_id_or_none is not None and not manager.should_reap(instance_id_or_none):
        # Warm reuse — existing instance is fine
        return instance_id_or_none

    # Tear down existing instance if present
    if instance_id_or_none is not None:
        provider.destroy_instance(instance_id_or_none)

    # Create a fresh instance
    new_instance: Instance = provider.create_instance(spec)
    manager.register(new_instance.id, new_instance.created_at)
    return new_instance.id
