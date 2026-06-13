"""Layer W: long-running sweeper-daemon substrate.

Mirrors Layer U HeartbeatLoop (eager first tick, Event.wait sleep, broad
try/except per iter, daemon thread + bounded join). Calls Layer V sweep()
on each tick, folds the SweepReport into cumulative stats, and writes the
synthetic sweeper:<host> ledger entry as the daemon's own liveness signal.

Public surface:
  - SweeperLoop (start, stop, reload)
  - _SweeperStats (consumed by sweeper_metrics renderers + ledger.touch)
  - _DeferredCounts (per-reason breakdown of skipped sweeps)
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import Policy, Verdict
from kinoforge.core.reaper_actor import SweepReport, sweep
from kinoforge.stores.base import ArtifactStore  # noqa: TC001  (runtime type)

_log = logging.getLogger(__name__)


@dataclass
class _DeferredCounts:
    """Per-reason breakdown of sweep-deferrals."""

    session_claim: int = 0
    heartbeat_unknown_skipped: int = 0
    heartbeat_substrate_missing: int = 0


@dataclass
class _SweeperStats:
    """Cumulative tally across the daemon's lifetime.

    Folded forward each tick; never reset until process exit. The ledger
    sweeper-tick entry carries a snapshot of every counter so ``status``
    survives daemon restarts on cloud-store-backed ledgers.
    """

    sweeps_total: int = 0
    destroys_total: int = 0
    errors_total: int = 0
    last_sweep_ts: float = 0.0
    deferred: _DeferredCounts = field(default_factory=_DeferredCounts)

    def fold(self, report: SweepReport, *, now: float) -> None:
        """Tally one SweepReport into the cumulative counters.

        Args:
            report: The SweepReport returned by this tick's sweep().
            now: Wall-clock seconds; stored as last_sweep_ts.

        Side effects: increments counters; emits INFO log per
        deferred-session-claim action with its reason (the B7 holder-pid
        diagnostic). HEARTBEAT_SUBSTRATE_MISSING + HEARTBEAT_UNKNOWN are
        counted from ``report.snapshot`` because act_on_verdict returns
        no_op for these (no entry in ``report.actions``).
        """
        self.sweeps_total += 1
        self.last_sweep_ts = now
        for action in report.actions:
            if action.action == "destroyed_and_forgot":
                self.destroys_total += 1
            elif action.action == "deferred-session-claim":
                self.deferred.session_claim += 1
                _log.info(
                    "sweep deferred for %s — %s",
                    action.instance_id,
                    action.reason or "session-claim",
                )
            elif action.action == "failed":
                self.errors_total += 1
        for _entry, verdict in report.snapshot.values():
            if verdict == Verdict.HEARTBEAT_SUBSTRATE_MISSING:
                self.deferred.heartbeat_substrate_missing += 1
            elif verdict == Verdict.HEARTBEAT_UNKNOWN:
                self.deferred.heartbeat_unknown_skipped += 1

    def snapshot_for_ledger(self) -> dict[str, Any]:
        """Return the ``ledger.touch`` ``**extra`` kwargs for this tick."""
        return {
            "sweeps_total": self.sweeps_total,
            "destroys_total": self.destroys_total,
            "errors_total": self.errors_total,
            "deferred_session_claim": self.deferred.session_claim,
            "deferred_heartbeat_unknown_skipped": self.deferred.heartbeat_unknown_skipped,
            "deferred_heartbeat_substrate_missing": self.deferred.heartbeat_substrate_missing,
        }

    def snapshot_for_log(self) -> str:
        """Compact one-liner for SIGUSR1 dump."""
        d = self.deferred
        return (
            f"sweeps={self.sweeps_total} destroys={self.destroys_total} "
            f"errors={self.errors_total} "
            f"deferred(session={d.session_claim},"
            f"hb_unk={d.heartbeat_unknown_skipped},"
            f"hb_miss={d.heartbeat_substrate_missing})"
        )


class SweeperLoop:
    """Background thread that periodically calls Layer V sweep().

    Mirrors HeartbeatLoop (core/heartbeat_loop.py) on lifecycle: eager
    first tick, Event.wait sleep, daemon=True thread, bounded join, broad
    try/except in _tick_once.

    Args:
        store: ArtifactStore for sweep()'s cross-process lock.
        ledger: Ledger to read entries from and touch the
            ``sweeper:<host>`` liveness entry on.
        registry_get_provider: Usually
            ``kinoforge.core.registry.get_provider``.
        thresholds: Mapping forwarded to classify() (idle_timeout_s,
            max_lifetime_s, heartbeat_interval_s, grace_after_session_s).
        interval_s: Seconds between successive ticks. Must be > 0.
        host: Identifier baked into the synthetic ledger key
            ``sweeper:<host>``. Usually socket.gethostname() at CLI.
        policy: Verdict-action policy. DEFAULT_APPLY_POLICY or its
            opt-in extensions built via sweeper_policy_from_cfg.
        clock: Wall-clock source. Defaults to RealClock.
        stats: Inject when the caller wants pre-existing counters
            (e.g. to survive a reload). Default fresh _SweeperStats.
        logger_: Optional logger override.
        join_timeout_s: Bound on stop()'s join(). Default 5.0s — absorbs
            worst-case act_on_verdict cloud round-trip.
        _sweep_fn: Test-only injection seam for the sweep callable.
            Defaults to ``kinoforge.core.reaper_actor.sweep``.

    Raises:
        ValueError: when interval_s <= 0 at __init__ or reload().
    """

    def __init__(
        self,
        *,
        store: ArtifactStore,
        ledger: Ledger,
        registry_get_provider: Callable[[str], Callable[[], Any]],
        thresholds: Mapping[str, Any],
        interval_s: float,
        host: str,
        policy: Policy,
        clock: Clock | None = None,
        stats: _SweeperStats | None = None,
        logger_: logging.Logger | None = None,
        join_timeout_s: float = 5.0,
        _sweep_fn: Callable[..., SweepReport] = sweep,
    ) -> None:
        """Construct the loop; thread not yet started until start()."""
        if interval_s <= 0:
            raise ValueError(f"interval_s must be > 0; got {interval_s}")
        self._store = store
        self._ledger = ledger
        self._registry_get_provider = registry_get_provider
        self._thresholds: dict[str, Any] = dict(thresholds)
        self._clock: Clock = clock or RealClock()
        self._interval_s = float(interval_s)
        self._host = host
        self._policy = policy
        self._stats = stats or _SweeperStats()
        self._logger = logger_ or _log
        self._join_timeout_s = join_timeout_s
        self._sweep_fn = _sweep_fn
        self._stop = threading.Event()
        self._reload_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name=f"kinoforge-sweeper-{host}",
            daemon=True,
        )

    @property
    def stats(self) -> _SweeperStats:
        """Expose cumulative stats for SIGUSR1 dump."""
        return self._stats

    def start(self) -> None:
        """Start the background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to stop and join with a bounded timeout."""
        self._stop.set()
        self._thread.join(self._join_timeout_s)

    def reload(
        self,
        *,
        policy: Policy | None = None,
        thresholds: Mapping[str, Any] | None = None,
        interval_s: float | None = None,
    ) -> None:
        """Swap policy / thresholds / interval atomically.

        Acquired under _reload_lock so a tick mid-flight either sees the
        old set entirely or the new set entirely; never torn across two
        fields. Wakes ``_stop.wait(...)`` so a new interval takes effect
        on the next tick rather than after the old interval elapses.

        Raises:
            ValueError: when interval_s <= 0.
        """
        with self._reload_lock:
            if policy is not None:
                self._policy = policy
            if thresholds is not None:
                self._thresholds = dict(thresholds)
            if interval_s is not None:
                if interval_s <= 0:
                    raise ValueError(f"interval_s must be > 0; got {interval_s}")
                self._interval_s = float(interval_s)
        self._stop.set()
        self._stop.clear()

    def _run(self) -> None:
        """Eager first tick, then sleep-and-tick until stopped."""
        while not self._stop.is_set():
            self._tick_once()
            self._stop.wait(self._interval_s)

    def _tick_once(self) -> None:
        """One sweep + persistence cycle, wrapped in broad try/except.

        Any exception from sweep, fold, or ledger.touch is logged via
        ``logger.exception`` (full stack) and swallowed. The loop is the
        only defence against silent thread death; future contributors
        must NOT lift this try/except outside the loop body.
        """
        try:
            with self._reload_lock:
                policy = self._policy
                thresholds = dict(self._thresholds)
            report = self._sweep_fn(
                self._store,
                self._ledger,
                self._registry_get_provider,
                thresholds,
                self._clock,
                policy=policy,
            )
            now = self._clock.now()
            self._stats.fold(report, now=now)
            self._ledger.touch(
                f"sweeper:{self._host}",
                last_heartbeat=now,
                heartbeat_thread_tick=now,
                **self._stats.snapshot_for_ledger(),
            )
        except Exception:  # noqa: BLE001
            self._stats.errors_total += 1
            self._logger.exception("sweep tick failed on host=%s", self._host)
