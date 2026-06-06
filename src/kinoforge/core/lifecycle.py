"""Lifecycle management: deadline math, warm reuse, graceful drain, dead-man liveness.

This module provides the orchestrator-level cost-safety layer for compute instances.
All time is sourced through the injected ``Clock`` so tests can step time deterministically.
"""

from __future__ import annotations

import logging
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kinoforge.core.clock import Clock
from kinoforge.core.errors import BudgetExceeded, TeardownError
from kinoforge.core.interfaces import Instance, InstanceSpec, Lifecycle

if TYPE_CHECKING:
    from kinoforge.core.interfaces import ComputeProvider
    from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Ledger  (Task 18)
# ---------------------------------------------------------------------------


# Keys that ``Ledger.record`` owns. ``Ledger.touch`` silently filters them
# out of its ``**extra`` payload so a future Layer V consumer cannot
# accidentally clobber an instance's identity by passing them through.
_PROTECTED_LEDGER_KEYS: frozenset[str] = frozenset(
    {"id", "provider", "tags", "created_at", "cost_rate_usd_per_hr"}
)


class Ledger:
    """Persistent record of every launched instance, backed by an ArtifactStore.

    Mutating operations (``record`` / ``forget``) take an outer
    cross-process lock from :meth:`ArtifactStore.acquire_lock` before the
    read-modify-write block.  Reads (``entries``) stay lock-free.

    Args:
        store: The :class:`~kinoforge.stores.base.ArtifactStore` used for
            persistence.
        run_id: Namespace within the store.  All ledger data is written under
            ``<run_id>/ledger.json``.

    Example:
        >>> from kinoforge.stores.local import LocalArtifactStore
        >>> from pathlib import Path
        >>> store = LocalArtifactStore(Path("/tmp/test"))
        >>> ledger = Ledger(store=store, run_id="_test")
    """

    _LEDGER_NAME: str = "ledger.json"

    def __init__(
        self,
        store: ArtifactStore,
        run_id: str = "_lifecycle",
        *,
        mutate_ttl_s: float = 30.0,
    ) -> None:
        """Initialise the ledger.

        Args:
            store: Artifact store used for persistence.
            run_id: Namespace/run identifier used within the store.
            mutate_ttl_s: Outer cross-process lease duration for record/forget
                RMW operations.  Default 30s — covers a single read-modify-write
                round-trip including JSON parse/serialize.
        """
        self._store = store
        self._run_id = run_id
        self._mutate_ttl_s = mutate_ttl_s
        self._uri: str | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_uri(self) -> str:
        """Return the store URI for the ledger JSON.

        Delegates to ``self._store.uri_for`` — the universal ABC (Phase 11 /
        Layer A) that every artifact store implements. The previous
        isinstance(LocalArtifactStore) switch was a vestige from before
        that ABC existed.

        Returns:
            Absolute URI string for the ledger JSON file.
        """
        return self._store.uri_for(self._run_id, self._LEDGER_NAME)

    def _read_entries(self) -> list[dict]:  # type: ignore[type-arg]
        """Load existing entries from the store; return empty list if not yet written.

        Returns:
            List of entry dicts.
        """
        uri = self._compute_uri()
        try:
            data = self._store.get_json(uri)
            raw = data.get("entries", [])
            return [e for e in raw if isinstance(e, dict)]
        except FileNotFoundError:
            return []

    def _write_entries(self, entries: list[dict]) -> None:  # type: ignore[type-arg]
        """Persist the full entries list.

        Args:
            entries: Complete list of entry dicts to write.
        """
        self._store.put_json(self._run_id, self._LEDGER_NAME, {"entries": entries})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        instance: Instance,
        *,
        idle_timeout_s: int | None = None,
        max_age_s: int | None = None,
    ) -> None:
        """Append an instance entry to the ledger.

        Reads the current ledger (or starts fresh if none exists), appends
        the new entry, and writes back atomically within this call under
        an outer cross-process lock.

        Args:
            instance: The :class:`~kinoforge.core.interfaces.Instance` to
                record.  Fields ``id``, ``provider``, ``tags``,
                ``created_at``, and ``cost_rate_usd_per_hr`` are stored.
            idle_timeout_s: Optional lifecycle policy snapshot — when
                non-None, persisted into the entry so ``kinoforge status``
                can surface it without re-loading the YAML config.
            max_age_s: Optional lifecycle policy snapshot — same purpose
                as ``idle_timeout_s``.  Sourced at the call site from the
                effective ``Lifecycle.max_lifetime_s`` value (Layer S
                names the persisted key generically; the value is the
                maximum allowed lifetime in seconds).
        """
        with self._store.acquire_lock(
            f"ledger/{self._run_id}", ttl_s=self._mutate_ttl_s
        ):
            entries = self._read_entries()
            entry: dict = {  # type: ignore[type-arg]
                "id": instance.id,
                "provider": instance.provider,
                "tags": dict(instance.tags),
                "created_at": instance.created_at,
                "cost_rate_usd_per_hr": instance.cost_rate_usd_per_hr,
            }
            if idle_timeout_s is not None:
                entry["idle_timeout_s"] = int(idle_timeout_s)
            if max_age_s is not None:
                entry["max_age_s"] = int(max_age_s)
            entries.append(entry)
            self._write_entries(entries)

    def entries(self) -> list[dict]:  # type: ignore[type-arg]
        """Return all recorded entries.

        Returns:
            A list of dicts, each with keys ``id``, ``provider``, ``tags``,
            ``created_at``, and ``cost_rate_usd_per_hr``.
        """
        return self._read_entries()

    def forget(self, instance_id: str) -> None:
        """Remove the entry for ``instance_id`` from the ledger.

        If ``instance_id`` is not present, this is a no-op.  The operation
        is performed under an outer cross-process lock.

        Args:
            instance_id: The instance whose entry should be removed.
        """
        with self._store.acquire_lock(
            f"ledger/{self._run_id}", ttl_s=self._mutate_ttl_s
        ):
            entries = self._read_entries()
            updated = [e for e in entries if e.get("id") != instance_id]
            if len(updated) != len(entries):
                self._write_entries(updated)

    def touch(
        self,
        instance_id: str,
        *,
        last_heartbeat: float | None = None,
        **extra: float | int | str | None,
    ) -> bool:
        """Update fields on an existing ledger entry in place (strict update).

        Strict update: an unknown ``instance_id`` is a silent no-op (returns
        False). Insertion remains the sole responsibility of :meth:`record`
        — touch never resurrects a forgotten entry.

        Args:
            instance_id: Identity of the entry to mutate.
            last_heartbeat: Float seconds-since-epoch heartbeat timestamp.
                ``None`` skips the field entirely.
            **extra: Forward-compat seam for additional fields the touch
                consumer wants to thread through (e.g. the sentinel
                ``heartbeat_thread_tick`` written by
                :class:`kinoforge.core.heartbeat_loop.HeartbeatLoop`).
                Keys in the protected set
                ``{"id", "provider", "tags", "created_at", "cost_rate_usd_per_hr"}``
                are silently filtered so a future caller cannot rewrite
                ``record``-owned fields. ``None`` values are skipped.

        Returns:
            ``True`` iff a disk write happened.  ``False`` on unknown id,
            no-op kwargs (all ``None``), or when every proposed value
            already equals the on-disk value (skip-unchanged guard against
            lock thrash for sub-second-cadence consumers).

        Threading:
            Acquires ``self._store.acquire_lock(f"ledger/{run_id}",
            ttl_s=self._mutate_ttl_s)`` — the same lock key/ttl as
            :meth:`record` and :meth:`forget`.  When all kwargs are
            ``None`` the lock is not acquired (cheap fast-path).

        Sentinel-gate contract (forward-compat — no reaper consumes it
        today): code that consults ``last_heartbeat`` for a reaping or
        destructive decision MUST first check the sentinel field
        ``heartbeat_thread_tick``.  If
        ``now - heartbeat_thread_tick > 3 * heartbeat_interval_s``,
        treat ``last_heartbeat`` as untrustworthy — the writer thread
        may have crashed.  See Layer U spec §3.4 for the rationale.
        """
        proposed: dict[str, float | int | str] = {}
        if last_heartbeat is not None:
            proposed["last_heartbeat"] = float(last_heartbeat)
        for key, value in extra.items():
            if key in _PROTECTED_LEDGER_KEYS or value is None:
                continue
            proposed[key] = value
        if not proposed:
            return False
        with self._store.acquire_lock(
            f"ledger/{self._run_id}", ttl_s=self._mutate_ttl_s
        ):
            entries = self._read_entries()
            for entry in entries:
                if entry.get("id") == instance_id:
                    changed = False
                    for key, value in proposed.items():
                        if entry.get(key) != value:
                            entry[key] = value
                            changed = True
                    if changed:
                        self._write_entries(entries)
                    return changed
        return False


# ---------------------------------------------------------------------------
# destroy_confirmed  (Task 18)
# ---------------------------------------------------------------------------


def destroy_confirmed(
    provider: ComputeProvider,
    instance_id: str,
    *,
    retries: int = 3,
    clock: Clock | None = None,  # noqa: ARG001  (reserved for future use)
    sleep_s: float = 0.5,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Destroy an instance and poll until it is confirmed gone.

    Calls ``provider.destroy_instance(instance_id)`` then polls
    ``provider.list_instances()`` to verify the instance has disappeared.
    Retries up to ``retries`` times, sleeping between attempts.

    If the instance is still present after all retries an ERROR is logged and
    :class:`~kinoforge.core.errors.TeardownError` is raised.

    Note:
        Pass ``sleep=lambda _: None`` in tests to skip real sleeps.

    Args:
        provider: The compute provider that owns the instance.
        instance_id: The instance to destroy and confirm gone.
        retries: Maximum number of destroy+poll attempts (default 3).
        clock: Optional clock for time-tracking (currently reserved for
            future use; does not affect sleep behaviour).
        sleep_s: Seconds to sleep between poll attempts (default 0.5).
        sleep: Injectable sleep callable; defaults to ``time.sleep``.
            Pass ``lambda _: None`` in unit tests.

    Raises:
        TeardownError: If the instance is still visible after all retries.
    """
    _sleep: Callable[[float], None] = sleep if sleep is not None else _time.sleep

    for attempt in range(1, retries + 1):
        provider.destroy_instance(instance_id)
        live_ids = {inst.id for inst in provider.list_instances()}
        if instance_id not in live_ids:
            return  # confirmed gone
        if attempt < retries:
            _sleep(sleep_s)

    # Final check failed — alert loudly
    _log.error(
        "destroy_confirmed: failed to confirm destruction of %r after %d retries",
        instance_id,
        retries,
    )
    raise TeardownError(f"failed to confirm destruction of {instance_id!r}")


# ---------------------------------------------------------------------------
# reap  (Task 18)
# ---------------------------------------------------------------------------


def reap(
    provider: ComputeProvider,
    lifecycle_manager: LifecycleManager,
    ledger: Ledger,
    policy: object = None,  # DEFERRED — accept and ignore
) -> list[str]:
    """Sweep provider instances and destroy those that should be reaped or are over-age.

    For each live instance:

    * If ``lifecycle_manager.should_reap(id)`` is True (idle past idle_timeout) →
      destroy via :func:`destroy_confirmed` and forget from ledger.
    * If the instance is over ``max_lifetime_s`` (``clock.now() - created_at >
      max_lifetime_s``) → destroy and forget.

    Also removes ledger entries that no longer correspond to live instances
    (already gone).

    The orchestrator state (``LifecycleManager._states``) is not mutated by
    this function; it is a read-only external sweeper.

    Args:
        provider: The compute provider to sweep.
        lifecycle_manager: Provides ``should_reap`` and clock/lifecycle access.
        ledger: Persistent instance ledger; entries are forgotten after
            confirmed destruction.
        policy: Reserved for future extensibility — accepted and ignored.

    Returns:
        List of instance ids that were destroyed during this sweep.
    """
    del policy  # DEFERRED

    clock = lifecycle_manager._clock
    max_lifetime_s = lifecycle_manager._lifecycle.max_lifetime_s
    now = clock.now()

    live_instances = provider.list_instances()
    live_ids = {inst.id for inst in live_instances}

    # Forget ledger entries for instances already gone outside our knowledge
    for stale in ledger.entries():
        if stale["id"] not in live_ids:
            ledger.forget(stale["id"])

    destroyed: list[str] = []

    for inst in live_instances:
        # Check idle reap
        if inst.id in lifecycle_manager._states:
            if lifecycle_manager.should_reap(inst.id):
                destroy_confirmed(provider, inst.id, sleep=lambda _: None)
                ledger.forget(inst.id)
                destroyed.append(inst.id)
                continue

        # Check over-age via ledger (cost_rate_usd_per_hr or created_at)
        matching = [e for e in ledger.entries() if e["id"] == inst.id]
        entry: dict | None = matching[0] if matching else None  # type: ignore[type-arg]
        created_at = (
            float(entry["created_at"]) if entry is not None else inst.created_at
        )
        if now - created_at > max_lifetime_s:
            destroy_confirmed(provider, inst.id, sleep=lambda _: None)
            ledger.forget(inst.id)
            destroyed.append(inst.id)

    return destroyed


# ---------------------------------------------------------------------------
# BudgetTracker  (Task 18)
# ---------------------------------------------------------------------------


class BudgetTracker:
    """Monitors cumulative estimated spend per instance and enforces a budget ceiling.

    Spend is estimated as::

        accrued = (clock.now() - created_at) / 3600 * cost_rate_usd_per_hr

    where ``cost_rate_usd_per_hr`` is read from the ledger (not live instance
    state) so it works even after an instance is no longer in memory.

    Args:
        lifecycle_manager: Used for correlation context (run_id, clock).
        ledger: Persistent record of launched instances (provides
            ``created_at`` and ``cost_rate_usd_per_hr``).
        clock: Wall-clock source for computing elapsed time.
        budget_usd: Maximum spend in USD; :meth:`enforce` tears down and
            raises when ``accrued > budget_usd``.

    Example:
        >>> from kinoforge.core.clock import FakeClock
        >>> from kinoforge.core.interfaces import Lifecycle
        >>> # See tests for full usage pattern
    """

    def __init__(
        self,
        lifecycle_manager: LifecycleManager,
        ledger: Ledger,
        clock: Clock,
        budget_usd: float,
    ) -> None:
        """Initialise the tracker.

        Args:
            lifecycle_manager: Lifecycle manager for this run.
            ledger: Ledger holding instance cost metadata.
            clock: Wall-clock source.
            budget_usd: Budget ceiling in USD.
        """
        self._manager = lifecycle_manager
        self._ledger = ledger
        self._clock = clock
        self._budget_usd = budget_usd

    def _entry_for(self, instance_id: str) -> dict:  # type: ignore[type-arg]
        """Return the ledger entry for ``instance_id`` or raise ``KeyError``."""
        entry = next(
            (e for e in self._ledger.entries() if e["id"] == instance_id), None
        )
        if entry is None:
            raise KeyError(f"no ledger entry for instance {instance_id!r}")
        return entry

    def accrued(self, instance_id: str) -> float:
        """Return the estimated cumulative spend for ``instance_id`` in USD.

        Computed as::

            (clock.now() - created_at) / 3600 * cost_rate_usd_per_hr

        Args:
            instance_id: The instance whose spend to estimate.

        Returns:
            Estimated spend in USD (float).

        Raises:
            KeyError: No ledger entry found for ``instance_id``.
        """
        entry = self._entry_for(instance_id)
        elapsed_h = (self._clock.now() - float(entry["created_at"])) / 3600.0
        rate = float(entry["cost_rate_usd_per_hr"])
        return elapsed_h * rate

    def over_budget(self, instance_id: str) -> bool:
        """Return ``True`` when accrued spend exceeds the budget ceiling.

        Args:
            instance_id: The instance to evaluate.

        Returns:
            ``True`` iff ``accrued(instance_id) > budget_usd``.
        """
        return self.accrued(instance_id) > self._budget_usd

    def enforce(self, instance_id: str, provider: ComputeProvider) -> None:
        """Tear down the instance and raise if over budget.

        Order of operations: destroy first, then raise.  This ensures cleanup
        happens even if the caller does not catch :exc:`BudgetExceeded`.

        Args:
            instance_id: The instance to check and potentially destroy.
            provider: Compute provider used to destroy the instance.

        Raises:
            BudgetExceeded: If ``over_budget(instance_id)`` is True.  The
                instance is already destroyed when this is raised.
        """
        if not self.over_budget(instance_id):
            return

        spend = self.accrued(instance_id)
        destroy_confirmed(provider, instance_id, sleep=lambda _: None)
        self._ledger.forget(instance_id)

        raise BudgetExceeded(
            f"instance {instance_id!r} crossed budget {self._budget_usd} USD"
            f" (accrued ~{spend:.2f})"
        )
