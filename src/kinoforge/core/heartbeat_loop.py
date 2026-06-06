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
from typing import Protocol, runtime_checkable

from kinoforge.core.clock import Clock, RealClock

_log = logging.getLogger(__name__)


@runtime_checkable
class _HeartbeatProvider(Protocol):
    """Structural subset of :class:`ComputeProvider` used by HeartbeatLoop.

    Only the two methods that the loop body calls. Keeping the surface
    minimal matches the project's structural-Protocol pattern (see
    PROGRESS:121 ``_ProvisionConfig`` precedent) and lets tests pass
    duck-typed stubs without inheriting the full ABC.
    """

    def heartbeat(self, instance_id: str) -> None: ...

    def last_heartbeat(self, instance_id: str) -> float | None: ...


@runtime_checkable
class _TouchableLedger(Protocol):
    """Structural subset of :class:`Ledger` used by HeartbeatLoop."""

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
    ) -> None:
        """Initialise the loop; the thread is not started until :meth:`start`."""
        if interval_s <= 0:
            raise ValueError(f"interval_s must be > 0; got {interval_s}")
        self._ledger = ledger
        self._provider = provider
        self._instance_id = instance_id
        self._interval_s = interval_s
        self._clock: Clock = clock or RealClock()
        self._logger = logger_ or _log
        self._join_timeout_s = join_timeout_s
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
        """
        try:
            self._provider.heartbeat(self._instance_id)
            last_hb = self._provider.last_heartbeat(self._instance_id)
            self._ledger.touch(
                self._instance_id,
                last_heartbeat=last_hb,
                heartbeat_thread_tick=self._clock.now(),
            )
        except Exception:  # noqa: BLE001 — single bad tick must not kill the loop
            self._logger.exception("heartbeat tick failed for %s", self._instance_id)
