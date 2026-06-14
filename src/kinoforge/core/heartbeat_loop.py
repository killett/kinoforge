"""Threaded periodic heartbeat poll + ledger persistence (Layer U T2).

A ``HeartbeatLoop`` runs inside an active ``deploy_session`` and pings
``provider.heartbeat(id)`` then ``ledger.touch(id, last_heartbeat=...,
heartbeat_thread_tick=clock.now())`` on a configured cadence.  The
sentinel ``heartbeat_thread_tick`` is the load-bearing crash-safety
signal: a stale sentinel relative to the configured cadence means the
loop has stopped writing, even if ``last_heartbeat`` looks fresh.

The design defends against silent thread death (Layer U spec §3.4):

1. Every tick is wrapped in ``try/except Exception`` and routed to
   ``logger.exception`` — a single bad tick can never kill the loop.
2. The sentinel ``heartbeat_thread_tick`` is written alongside every
   ``last_heartbeat`` so future reaper code can distinguish fresh from
   silent-crashed.  See the contract note on
   :meth:`kinoforge.core.lifecycle.Ledger.touch`.
3. The thread is ``daemon=True`` and ``stop()`` calls
   ``join(timeout=join_timeout_s)`` — a wedged thread cannot block
   process exit.  Provider-native cleanup (RunPod selfterm, SkyPilot
   autostop, LocalProvider process containment) catches any orphan pod.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.errors import TransportError
from kinoforge.core.reaper import _stall_reap_predicate
from kinoforge.core.util_counter import _update_counter, _update_uptime_counter
from kinoforge.core.util_endpoints import UtilSnapshot

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.util_endpoints import UtilSnapshotEndpoint

_log = logging.getLogger(__name__)


@runtime_checkable
class _HeartbeatProvider(Protocol):
    """Structural subset of :class:`ComputeProvider` used by HeartbeatLoop.

    Only the methods the loop body invokes. ``destroy_instance`` is
    invoked exclusively by the C26 STALL_REAP path and is duck-typed at
    runtime via ``hasattr`` so existing B5a tests with two-method spies
    keep working unchanged.
    """

    def heartbeat(self, instance_id: str) -> None: ...

    def last_heartbeat(self, instance_id: str) -> float | None: ...


@runtime_checkable
class _TouchableLedger(Protocol):
    """Structural subset of :class:`Ledger` used by HeartbeatLoop.

    ``forget`` is invoked exclusively by the C26 STALL_REAP path and is
    duck-typed at runtime via ``hasattr``.
    """

    def touch(
        self,
        instance_id: str,
        *,
        last_heartbeat: float | None = None,
        **extra: float | int | str | None,
    ) -> bool: ...


@runtime_checkable
class HeartbeatLoopProtocol(Protocol):
    """Structural protocol for the deploy_session heartbeat-loop seam.

    Lets tests substitute non-threaded spies for HeartbeatLoop without
    inheriting the full class. Mirrors the project's structural-Protocol
    pattern (see :class:`_HeartbeatProvider`).
    """

    def start(self) -> None:
        """Begin emitting heartbeat ticks."""
        ...

    def stop(self) -> None:
        """Signal the loop to stop and join with a bounded timeout."""
        ...


class HeartbeatLoop:
    """Background thread that pings provider + persists heartbeat to ledger.

    Args:
        ledger: The :class:`~kinoforge.core.lifecycle.Ledger` to update.
        provider: The :class:`~kinoforge.core.interfaces.ComputeProvider`
            whose ``heartbeat()`` is invoked and whose
            ``last_heartbeat()`` is read into the ledger.
        instance_id: Identity of the instance whose heartbeat is tracked.
        interval_s: Seconds between successive ticks.  Must be > 0.
        clock: Injected clock for the sentinel; defaults to ``RealClock``.
        logger_: Optional logger; defaults to the module-level logger.
        join_timeout_s: Bound on ``stop()``'s ``join()`` call.  Default
            2.0s.  A wedged thread will not block process exit because
            the thread is daemon.

    Example:
        >>> # Typical use is inside deploy_session — see Layer U T3.
        >>> from kinoforge.core.heartbeat_loop import HeartbeatLoop
        >>> # loop = HeartbeatLoop(ledger=..., provider=..., instance_id=...,
        >>> #                      interval_s=30.0)
        >>> # loop.start(); try: ... finally: loop.stop()
    """

    def __init__(
        self,
        *,
        ledger: _TouchableLedger,
        provider: _HeartbeatProvider,
        instance_id: str,
        interval_s: float,
        clock: Clock | None = None,
        logger_: logging.Logger | None = None,
        join_timeout_s: float = 2.0,
        util_endpoint: UtilSnapshotEndpoint | None = None,
        cancel_token: CancelToken | None = None,
        provider_kind: str | None = None,
        stall_window_s: float | None = None,
        stall_gpu_threshold: float = 5.0,
        stall_cpu_threshold: float = 20.0,
        restart_loop_window_s: float | None = None,
        restart_loop_uptime_threshold_s: float = 90.0,
    ) -> None:
        """Initialise the loop; the thread is not started until :meth:`start`.

        Args:
            ledger: Ledger to update each tick.
            provider: Compute provider whose ``heartbeat`` + ``last_heartbeat``
                the loop polls.
            instance_id: Identity of the instance whose heartbeat is tracked.
            interval_s: Seconds between ticks. Must be > 0.
            clock: Injected clock; defaults to RealClock.
            logger_: Optional logger; defaults to the module logger.
            join_timeout_s: Bound on ``stop()``'s join. Default 2 s.
            util_endpoint: C26 util sampler. ``None`` keeps B5a behaviour
                (no util read, no counter, no STALL).
            cancel_token: C26 token signalled when STALL_REAP fires so the
                outer ``deploy_session`` aborts in-flight work cleanly.
            provider_kind: Carried for the substrate-gate check inside
                :func:`_stall_reap_predicate`. ``None`` skips the gate.
            stall_window_s: C26 cfg threshold (effective window in
                seconds). ``None`` = kill switch — no STALL ever fires.
            stall_gpu_threshold: GPU util % strict-< threshold for
                ``_update_counter``.
            stall_cpu_threshold: CPU % strict-< threshold for
                ``_update_counter``.
            restart_loop_window_s: C27 cfg threshold (effective window
                in seconds). ``None`` = kill switch — no RESTART fires.
            restart_loop_uptime_threshold_s: uptime-seconds strict-<
                threshold for ``_update_uptime_counter``.
        """
        if interval_s <= 0:
            raise ValueError(f"interval_s must be > 0; got {interval_s}")
        self._ledger = ledger
        self._provider = provider
        self._instance_id = instance_id
        self._interval_s = interval_s
        self._clock: Clock = clock or RealClock()
        self._logger = logger_ or _log
        self._join_timeout_s = join_timeout_s
        self._util_endpoint = util_endpoint
        self._cancel_token = cancel_token
        self._provider_kind = provider_kind
        self._stall_window_s = stall_window_s
        self._stall_gpu_threshold = stall_gpu_threshold
        self._stall_cpu_threshold = stall_cpu_threshold
        self._restart_loop_window_s = restart_loop_window_s
        self._restart_loop_uptime_threshold_s = restart_loop_uptime_threshold_s
        self._counter = 0
        self._uptime_counter = 0
        self._prev_uptime: int | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"kinoforge-hb-{instance_id}",
            daemon=True,
        )

    def start(self) -> None:
        """Start the background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to stop and join with a bounded timeout.

        Returns even if the thread is wedged inside a blocking
        ``provider.heartbeat`` call — the thread is daemon, so process
        exit is never blocked.
        """
        self._stop.set()
        self._thread.join(self._join_timeout_s)

    # ------------------------------------------------------------------
    # Internal — thread body
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Loop body: eager first tick, then sleep-and-tick until stopped."""
        while not self._stop.is_set():
            self._tick_once()
            # Use Event.wait so stop() can wake the sleep immediately.
            self._stop.wait(self._interval_s)

    def _tick_once(self) -> None:
        """One observation + persistence cycle, wrapped in broad try/except.

        Any exception from the provider or the ledger is logged via
        ``logger.exception`` (full stack trace) and swallowed.  The loop
        is the only Layer 1 defense against silent thread death; future
        contributors must NOT lift this try/except outside the loop body.

        When ``util_endpoint`` is set (C26), the tick also samples util,
        updates the consecutive-low counter, persists the 7 new ledger
        fields, and self-classifies for STALL_REAP. On STALL_REAP the
        loop destroys the pod, forgets the ledger entry, signals the
        cancel token, and stops the thread.
        """
        now = self._clock.now()
        try:
            self._provider.heartbeat(self._instance_id)
            last_hb = self._provider.last_heartbeat(self._instance_id)
            extra: dict[str, float | int | str | None] = {
                "heartbeat_thread_tick": now,
            }
            snap = self._read_util_safely()
            if self._util_endpoint is not None:
                self._counter = _update_counter(
                    self._counter,
                    prev_uptime_s=self._prev_uptime,
                    snap=snap,
                    gpu_threshold=self._stall_gpu_threshold,
                    cpu_threshold=self._stall_cpu_threshold,
                )
                self._uptime_counter = _update_uptime_counter(
                    self._uptime_counter,
                    snap=snap,
                    uptime_threshold_s=self._restart_loop_uptime_threshold_s,
                )
                if snap is not None and snap.uptime_seconds is not None:
                    self._prev_uptime = snap.uptime_seconds
                extra.update(
                    self._build_util_extra(
                        now=now,
                        snap=snap,
                        counter=self._counter,
                        uptime_counter=self._uptime_counter,
                    )
                )
            self._ledger.touch(
                self._instance_id,
                last_heartbeat=last_hb,
                **extra,
            )
            if self._util_endpoint is not None:
                self._maybe_fire_stall_reap(now=now)
        except Exception:  # noqa: BLE001 — single bad tick must not kill the loop
            self._logger.exception("heartbeat tick failed for %s", self._instance_id)

    def _read_util_safely(self) -> UtilSnapshot | None:
        """Return util snapshot or None when the endpoint is unset / errors out."""
        if self._util_endpoint is None:
            return None
        try:
            return self._util_endpoint.read_util(self._instance_id)
        except TransportError:
            self._logger.warning(
                "util read transport error for %s; counter preserved",
                self._instance_id,
            )
            return None

    @staticmethod
    def _build_util_extra(
        *,
        now: float,
        snap: UtilSnapshot | None,
        counter: int,
        uptime_counter: int,
    ) -> dict[str, float | int | str | None]:
        """Build the util-related ledger fields plus the tick timestamp.

        C27 adds ``consecutive_low_uptime_count`` alongside C26's
        ``consecutive_low_util_count`` in both branches.
        """
        base: dict[str, float | int | str | None] = {
            "util_thread_tick": now,
            "consecutive_low_util_count": counter,
            "consecutive_low_uptime_count": uptime_counter,
        }
        if snap is None:
            return base
        return {
            **base,
            "last_gpu_util_percent": snap.gpu_util_percent,
            "last_cpu_percent": snap.cpu_percent,
            "last_memory_percent": snap.memory_percent,
            "last_disk_percent": snap.disk_percent,
            "last_uptime_seconds": snap.uptime_seconds,
        }

    def _maybe_fire_stall_reap(self, *, now: float) -> None:
        """Self-classify the entry; on STALL_REAP destroy + cancel + stop."""
        if self._stall_window_s is None:
            return
        sentinel_window = 3.0 * self._interval_s
        entry: dict[str, float | int | str | None] = {
            "id": self._instance_id,
            "consecutive_low_util_count": self._counter,
            "util_thread_tick": now,
        }
        if self._provider_kind is not None:
            entry["provider"] = self._provider_kind
        fire = _stall_reap_predicate(
            entry,
            now=now,
            sentinel_window=sentinel_window,
            heartbeat_interval_s=self._interval_s,
            stall_window_s=self._stall_window_s,
        )
        if not fire:
            return
        self._logger.warning(
            "STALL_REAP fired for %s (counter=%d, window=%.0fs)",
            self._instance_id,
            self._counter,
            self._stall_window_s,
        )
        destroy = getattr(self._provider, "destroy_instance", None)
        if destroy is not None:
            try:
                destroy(self._instance_id)
            except Exception:  # noqa: BLE001 — best-effort destroy
                self._logger.exception(
                    "STALL_REAP destroy failed for %s", self._instance_id
                )
        forget = getattr(self._ledger, "forget", None)
        if forget is not None:
            try:
                forget(self._instance_id)
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "STALL_REAP ledger.forget failed for %s", self._instance_id
                )
        if self._cancel_token is not None:
            self._cancel_token.set()
        self._stop.set()
