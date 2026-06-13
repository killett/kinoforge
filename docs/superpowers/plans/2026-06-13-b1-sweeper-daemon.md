# B1 — Layer W `kinoforge sweeper` daemon — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a long-running `kinoforge sweeper` daemon that calls Layer V `sweep()` on a configurable cadence, exposes liveness via a synthetic ledger entry, and surfaces `kinoforge sweeper start | stop | status | metrics` CLI.

**Architecture:** Daemon loop mirrors `core/heartbeat_loop.py:HeartbeatLoop` (eager first tick, `Event.wait` sleep, broad per-iter `try/except`, daemon thread + bounded join). Sweep + act_on_verdict come from Layer V unchanged. Liveness signal is a synthetic `sweeper:<host>` ledger entry — materialised once via `Ledger.record(Instance)` at startup, then `Ledger.touch` per tick. `sweep()` gains a one-line `eid.startswith("sweeper:")` filter so the daemon does not reap its own liveness entry. Metrics render `kinoforge_sweeper_*` Prom gauges as siblings of B2 (`kinoforge cost --prom`).

**Tech Stack:**
- Python 3.x (stdlib `threading`, `signal`, `socket`)
- Pydantic v2 (`SweeperConfig` model)
- Existing kinoforge substrates: Layer V (`core/reaper.py`, `core/reaper_actor.py`), Layer U (`core/heartbeat_loop.py`), B5a (`core/heartbeat_endpoints.py`), B7 (`core/session_claim.py` lock semantics; `Ledger.read`)
- pytest + pixi for tests; pre-commit for ruff / ruff-format / mypy
- Spec: `docs/superpowers/specs/2026-06-13-b1-sweeper-daemon-design.md` (committed `55cb2e6`)

**Live spend:** $0. `FakeProvider` + `LocalProvider` + subprocess-isolated start/stop tests cover the contract end-to-end.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/kinoforge/core/sweeper.py` | CREATE (~140 LOC) | `SweeperLoop` class + `_SweeperStats` + `_DeferredCounts`. Background-thread substrate. Mirrors `HeartbeatLoop`. |
| `src/kinoforge/core/sweeper_metrics.py` | CREATE (~70 LOC) | Pure renderers: `render_status_human`, `render_status_json`, `render_metrics_prom`. No I/O. |
| `src/kinoforge/core/reaper_actor.py` | MODIFY (+~3 LOC) | One-line filter in `sweep()` per-entry loop: `if eid.startswith("sweeper:"): continue`. |
| `src/kinoforge/core/config.py` | MODIFY (+~30 LOC) | `SweeperConfig` pydantic model + `Config.sweeper` field + `sweeper_policy_from_cfg(cfg)` bridge. |
| `src/kinoforge/cli/_commands.py` | MODIFY (+~180 LOC) | `_cmd_sweeper_start` / `_stop` / `_status` / `_metrics`. Signal handlers. Synthetic `Instance` record at startup. |
| `src/kinoforge/cli/_main.py` | MODIFY (+~15 LOC) | `sweeper` subparser with `start / stop / status / metrics` subcommands. |
| `tests/core/test_sweeper.py` | CREATE | Loop shape + signal handlers + stats fold + reload. ~12 tests. |
| `tests/core/test_sweeper_metrics.py` | CREATE | Human / JSON / Prom format locks. ~6 tests. |
| `tests/core/test_reaper_sweep.py` | MODIFY (+1 test) | Synthetic-id filter assertion. |
| `tests/core/test_config.py` | MODIFY (+3 tests) | `SweeperConfig` defaults + validator + policy bridge. |
| `tests/cli/test_cmd_sweeper.py` | CREATE | Status / metrics / banner / synthetic-record offline. ~7 tests. |
| `tests/cli/test_cmd_sweeper_xprocess.py` | CREATE | Subprocess start→SIGTERM→stop + SIGHUP reload. ~3 tests. |
| `tests/test_core_invariant.py` | MODIFY (+1 test) | `core/sweeper_metrics.py` is pure (no threading / subprocess / time / pathlib / ledger). |
| `examples/configs/sweeper.yaml` | CREATE | Documented sweeper YAML config. |
| `README.md` | MODIFY | Sweeper-daemon section (textfile-collector cron pattern; systemd posture). |
| `PROGRESS.md` | MODIFY | B1 closeout strike with commit sha (final task). |
| `warm-reuse-tasks.txt` | MODIFY | B1 closeout summary replacing the open-task block at lines 523–555 (final task). |

---

## Task a: `SweeperLoop` + `_SweeperStats` substrate

**Goal:** Land the background-thread substrate that calls `sweep()` per cadence, folds report into cumulative stats, writes the `sweeper:<host>` ledger entry, and supports atomic SIGHUP reload.

**Files:**
- Create: `src/kinoforge/core/sweeper.py`
- Test: `tests/core/test_sweeper.py`

**Acceptance Criteria:**
- [ ] `SweeperLoop(interval_s=0)` and `SweeperLoop(interval_s=-1)` both raise `ValueError`.
- [ ] Eager first tick fires before any `Event.wait` (mirrors `HeartbeatLoop` at `heartbeat_loop.py:152-156`).
- [ ] `stop()` sets `_stop` and `_thread.join(join_timeout_s)`; returns within `join_timeout_s` even when the in-flight tick is wedged.
- [ ] A `sweep()` exception inside `_tick_once` is caught; `stats.errors_total` increments; loop continues to next tick.
- [ ] Each successful tick calls `ledger.touch(f"sweeper:{host}", heartbeat_thread_tick=now, last_heartbeat=now, **stats.snapshot_for_ledger())`.
- [ ] `_SweeperStats.fold(report, now=...)` increments `sweeps_total`, sets `last_sweep_ts=now`, increments `destroys_total` per `destroyed_and_forgot`, increments `errors_total` per `failed`, increments `deferred.session_claim` per `deferred-session-claim` AND emits INFO log with the action's `reason`.
- [ ] `_SweeperStats.fold` counts `HEARTBEAT_SUBSTRATE_MISSING` + `HEARTBEAT_UNKNOWN` from `report.snapshot` (these never appear in `actions` because `act_on_verdict` returns `no_op`).
- [ ] `reload(policy=..., thresholds=..., interval_s=...)` swaps fields atomically under `_reload_lock`; an in-flight `_tick_once` either reads the full old set or the full new set.
- [ ] `reload(interval_s=N)` wakes the in-flight `_stop.wait(...)` immediately so the new cadence applies on the next tick.

**Verify:** `pixi run pytest tests/core/test_sweeper.py -v` → 12 passed.

**Steps:**

- [ ] **Step 1: RED — write `tests/core/test_sweeper.py`**

```python
"""Layer W: SweeperLoop substrate tests.

Mirrors the offline-only style of tests/core/test_heartbeat_loop.py.
FakeClock + spy ledger + manual SweepReport fabrication; no real
ArtifactStore, no real provider.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.reaper import Policy, Verdict
from kinoforge.core.reaper_actor import ActionResult, SweepReport
from kinoforge.core.sweeper import (
    SweeperLoop,
    _DeferredCounts,
    _SweeperStats,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _SpyLedger:
    def __init__(self) -> None:
        self.touches: list[tuple[str, dict[str, Any]]] = []

    def touch(
        self,
        instance_id: str,
        *,
        last_heartbeat: float | None = None,
        **extra: Any,
    ) -> bool:
        record = {"last_heartbeat": last_heartbeat, **extra}
        self.touches.append((instance_id, record))
        return True


def _make_report(
    *,
    actions: list[ActionResult] | None = None,
    snapshot: Mapping[str, tuple[Mapping[str, Any], Verdict]] | None = None,
) -> SweepReport:
    return SweepReport(snapshot=snapshot or {}, actions=actions or [])


def _spawn_loop(
    *,
    sweep_fn,
    ledger=None,
    clock=None,
    interval_s: float = 0.05,
    policy: Policy | None = None,
    host: str = "test-host",
    stats: _SweeperStats | None = None,
    join_timeout_s: float = 1.0,
) -> tuple[SweeperLoop, _SpyLedger, _SweeperStats]:
    led = ledger or _SpyLedger()
    st = stats or _SweeperStats()
    loop = SweeperLoop(
        store=object(),  # never touched by sweep_fn injection below
        ledger=led,
        registry_get_provider=lambda name: (lambda: None),
        thresholds={
            "idle_timeout_s": 7200.0,
            "max_lifetime_s": 28800.0,
            "heartbeat_interval_s": 30.0,
            "grace_after_session_s": 300.0,
        },
        clock=clock or FakeClock(start=1000.0),
        interval_s=interval_s,
        host=host,
        policy=policy or Policy(act_verdicts=frozenset()),
        stats=st,
        join_timeout_s=join_timeout_s,
        _sweep_fn=sweep_fn,  # test-only injection point
    )
    return loop, led, st


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_interval_s_must_be_positive() -> None:
    """Bug: a zero/negative interval would either spin-loop or never tick."""
    with pytest.raises(ValueError, match="interval_s must be > 0"):
        SweeperLoop(
            store=object(),
            ledger=_SpyLedger(),
            registry_get_provider=lambda name: (lambda: None),
            thresholds={},
            interval_s=0,
            host="h",
            policy=Policy(act_verdicts=frozenset()),
        )
    with pytest.raises(ValueError, match="interval_s must be > 0"):
        SweeperLoop(
            store=object(),
            ledger=_SpyLedger(),
            registry_get_provider=lambda name: (lambda: None),
            thresholds={},
            interval_s=-1.5,
            host="h",
            policy=Policy(act_verdicts=frozenset()),
        )


def test_eager_first_tick_fires_before_first_sleep() -> None:
    """Mirror HeartbeatLoop: tick(0) before wait(interval_s).

    Bug guard: if the loop slept first, a one-shot daemon would never sweep
    before SIGTERM tore it down.
    """
    sweep_calls = threading.Event()

    def sweep_fn(*args, **kwargs):
        sweep_calls.set()
        return _make_report()

    loop, _, _ = _spawn_loop(sweep_fn=sweep_fn, interval_s=60.0)
    loop.start()
    try:
        # Eager first tick should fire well before the 60s interval.
        assert sweep_calls.wait(timeout=2.0), "first tick did not fire eagerly"
    finally:
        loop.stop()


def test_stop_set_wakes_event_wait_immediately() -> None:
    """stop() must return within join_timeout_s even with a huge interval.

    Bug guard: if Event.wait were replaced with time.sleep, stop would block
    until the interval elapsed.
    """

    def sweep_fn(*args, **kwargs):
        return _make_report()

    loop, _, _ = _spawn_loop(sweep_fn=sweep_fn, interval_s=600.0, join_timeout_s=1.0)
    loop.start()
    # Let the eager first tick run.
    time.sleep(0.1)
    start = time.monotonic()
    loop.stop()
    elapsed = time.monotonic() - start
    assert elapsed < 1.5, f"stop() blocked for {elapsed:.2f}s (expected <1.5s)"


def test_bad_classify_does_not_kill_loop() -> None:
    """A sweep() exception is caught; the loop survives to tick again.

    Bug guard: if try/except were lifted, one bad provider would silently
    stop the daemon — operators would never know.
    """
    tick_count = {"n": 0}

    def sweep_fn(*args, **kwargs):
        tick_count["n"] += 1
        if tick_count["n"] == 1:
            raise RuntimeError("provider exploded")
        return _make_report()

    loop, _, stats = _spawn_loop(sweep_fn=sweep_fn, interval_s=0.05)
    loop.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if tick_count["n"] >= 2:
                break
            time.sleep(0.05)
        assert tick_count["n"] >= 2, "loop did not survive the first exception"
        assert stats.errors_total >= 1
    finally:
        loop.stop()


def test_tick_writes_sweeper_ledger_entry() -> None:
    """Each successful tick touches sweeper:<host> with monotonic tick ts.

    Bug guard: if last_heartbeat were omitted, Layer V classify would see
    hb=None and read substrate-missing for the synthetic entry.
    """
    fake_clock = FakeClock(start=1000.0)

    def sweep_fn(*args, **kwargs):
        return _make_report()

    loop, ledger, _ = _spawn_loop(
        sweep_fn=sweep_fn,
        clock=fake_clock,
        interval_s=0.05,
        host="myhost",
    )
    loop.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if len(ledger.touches) >= 1:
                break
            time.sleep(0.05)
        assert ledger.touches, "no touch recorded"
        eid, record = ledger.touches[0]
        assert eid == "sweeper:myhost"
        assert record["heartbeat_thread_tick"] == 1000.0
        assert record["last_heartbeat"] == 1000.0
        # snapshot_for_ledger keys present
        assert "sweeps_total" in record
        assert "destroys_total" in record
    finally:
        loop.stop()


def test_stats_fold_counts_destroys() -> None:
    """destroyed_and_forgot → destroys_total += 1; failed → errors_total += 1.

    Bug guard: a stats-fold off-by-one would mis-report sweeper effectiveness
    on the dashboard.
    """
    stats = _SweeperStats()
    report = _make_report(
        actions=[
            ActionResult(
                instance_id="i-1",
                snapshot_verdict=Verdict.IDLE_REAP,
                applied_verdict=Verdict.IDLE_REAP,
                action="destroyed_and_forgot",
            ),
            ActionResult(
                instance_id="i-2",
                snapshot_verdict=Verdict.IDLE_REAP,
                applied_verdict=Verdict.IDLE_REAP,
                action="destroyed_and_forgot",
            ),
            ActionResult(
                instance_id="i-3",
                snapshot_verdict=Verdict.IDLE_REAP,
                applied_verdict=Verdict.IDLE_REAP,
                action="failed",
                reason="TeardownError boom",
            ),
        ]
    )
    stats.fold(report, now=2000.0)
    assert stats.destroys_total == 2
    assert stats.errors_total == 1
    assert stats.sweeps_total == 1
    assert stats.last_sweep_ts == 2000.0


def test_stats_fold_counts_deferred_session_claim(caplog: pytest.LogCaptureFixture) -> None:
    """deferred-session-claim increments counter AND emits INFO with reason.

    Bug guard: silently dropping the reason on the floor would lose B7
    holder-pid diagnostic that operators rely on to debug stuck claims.
    """
    stats = _SweeperStats()
    report = _make_report(
        actions=[
            ActionResult(
                instance_id="i-9",
                snapshot_verdict=Verdict.IDLE_REAP,
                applied_verdict=Verdict.IDLE_REAP,
                action="deferred-session-claim",
                reason="held by pid 12345; orchestrator mid-session-claim",
            ),
        ]
    )
    with caplog.at_level(logging.INFO, logger="kinoforge.core.sweeper"):
        stats.fold(report, now=2000.0)
    assert stats.deferred.session_claim == 1
    assert any(
        "i-9" in rec.message and "12345" in rec.message
        for rec in caplog.records
    ), "deferred INFO log missing instance_id and holder pid"


def test_stats_fold_counts_substrate_missing_from_snapshot() -> None:
    """HEARTBEAT_SUBSTRATE_MISSING surfaces via snapshot, not actions.

    Bug guard: act_on_verdict returns no_op on this verdict, so the only
    place to count it is the snapshot — looking at actions would always
    report zero.
    """
    stats = _SweeperStats()
    snapshot = {
        "i-sky-1": ({"id": "i-sky-1"}, Verdict.HEARTBEAT_SUBSTRATE_MISSING),
        "i-unk-1": ({"id": "i-unk-1"}, Verdict.HEARTBEAT_UNKNOWN),
        "i-live": ({"id": "i-live"}, Verdict.LIVE),
    }
    report = _make_report(snapshot=snapshot)
    stats.fold(report, now=2000.0)
    assert stats.deferred.heartbeat_substrate_missing == 1
    assert stats.deferred.heartbeat_unknown_skipped == 1


def test_reload_swaps_policy_under_lock() -> None:
    """reload() must produce non-torn reads under concurrency.

    Bug guard: a non-locked reload could swap policy mid-tick and produce
    sweeps whose act_verdicts came from one Policy and thresholds from
    another, causing inconsistent verdicts.
    """
    sweep_args: list[tuple[Mapping[str, Any], Policy]] = []

    def sweep_fn(store, ledger, get_provider, thresholds, clock, *, policy):
        sweep_args.append((dict(thresholds), policy))
        return _make_report()

    initial_policy = Policy(act_verdicts=frozenset({Verdict.IDLE_REAP}))
    new_policy = Policy(act_verdicts=frozenset({Verdict.OVERAGE_REAP}))
    loop, _, _ = _spawn_loop(
        sweep_fn=sweep_fn, interval_s=0.05, policy=initial_policy
    )
    loop.start()
    try:
        time.sleep(0.15)
        loop.reload(policy=new_policy)
        time.sleep(0.15)
    finally:
        loop.stop()
    saw_initial = any(p == initial_policy for _, p in sweep_args)
    saw_new = any(p == new_policy for _, p in sweep_args)
    assert saw_initial and saw_new, (
        f"expected ticks under both policies; got {sweep_args}"
    )


def test_reload_wakes_sleep_immediately() -> None:
    """reload() must wake Event.wait so the new interval takes effect now.

    Bug guard: if reload only stored fields, a long old interval would
    delay the new cadence indefinitely.
    """
    tick_times: list[float] = []

    def sweep_fn(*args, **kwargs):
        tick_times.append(time.monotonic())
        return _make_report()

    loop, _, _ = _spawn_loop(sweep_fn=sweep_fn, interval_s=10.0)
    loop.start()
    try:
        # Wait for the eager tick to land.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not tick_times:
            time.sleep(0.05)
        assert tick_times, "eager tick missing"
        start_reload = time.monotonic()
        loop.reload(interval_s=0.05)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(tick_times) < 2:
            time.sleep(0.05)
        assert len(tick_times) >= 2, "second tick did not fire after reload"
        gap = tick_times[1] - start_reload
        assert gap < 1.5, f"reload did not wake sleep promptly (gap={gap:.2f}s)"
    finally:
        loop.stop()


def test_join_timeout_bounds_stop_call() -> None:
    """A wedged sweep_fn must not block stop() beyond join_timeout_s.

    Bug guard: a daemon thread that wedges in provider I/O must not block
    process exit — daemon=True + bounded join is the contract.
    """
    wedge = threading.Event()

    def sweep_fn(*args, **kwargs):
        wedge.wait(timeout=30.0)
        return _make_report()

    loop, _, _ = _spawn_loop(
        sweep_fn=sweep_fn, interval_s=10.0, join_timeout_s=0.5
    )
    loop.start()
    time.sleep(0.1)  # let the tick land in the wedge
    start = time.monotonic()
    loop.stop()
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"stop blocked {elapsed:.2f}s past join_timeout_s"
    wedge.set()


def test_reload_rejects_non_positive_interval() -> None:
    """reload(interval_s=0) raises ValueError.

    Bug guard: same invariant as __init__; mid-flight zero interval would
    spin-loop the daemon.
    """
    def sweep_fn(*args, **kwargs):
        return _make_report()

    loop, _, _ = _spawn_loop(sweep_fn=sweep_fn, interval_s=0.05)
    loop.start()
    try:
        with pytest.raises(ValueError, match="interval_s must be > 0"):
            loop.reload(interval_s=0)
    finally:
        loop.stop()
```

- [ ] **Step 2: Run RED**

```
pixi run pytest tests/core/test_sweeper.py -v
```
Expected: collection errors / `ModuleNotFoundError: kinoforge.core.sweeper`.

- [ ] **Step 3: GREEN — write `src/kinoforge/core/sweeper.py`**

```python
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
from kinoforge.core.reaper import Policy, Verdict
from kinoforge.core.reaper_actor import SweepReport, sweep

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
    sweeper-tick entry carries a snapshot of every counter so `status`
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
        counted from `report.snapshot` because act_on_verdict returns
        no_op for these (no entry in `report.actions`).
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
        """Return the ledger.touch **extra kwargs for this tick."""
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

    Mirrors HeartbeatLoop (core/heartbeat_loop.py:81-175) byte-for-byte on
    lifecycle: eager first tick, Event.wait sleep, daemon=True thread,
    bounded join, broad try/except in _tick_once.

    Args:
        store: ArtifactStore for sweep()'s cross-process lock.
        ledger: Ledger to read entries from and touch the sweeper:<host>
            liveness entry on.
        registry_get_provider: Usually kinoforge.core.registry.get_provider.
        thresholds: Mapping forwarded to classify() (idle_timeout_s,
            max_lifetime_s, heartbeat_interval_s, grace_after_session_s).
        clock: Wall-clock source. Defaults to RealClock.
        interval_s: Seconds between successive ticks. Must be > 0.
        host: Identifier baked into the synthetic ledger key
            'sweeper:<host>'. Usually socket.gethostname() at CLI level.
        policy: Verdict-action policy. DEFAULT_APPLY_POLICY or its opt-in
            extensions built via sweeper_policy_from_cfg.
        stats: Inject when the caller wants pre-existing counters
            (e.g. to survive a reload). Default fresh _SweeperStats.
        logger_: Optional logger override.
        join_timeout_s: Bound on stop()'s join(). Default 5.0s — absorbs
            worst-case act_on_verdict cloud round-trip.
        _sweep_fn: Test-only injection seam for the sweep callable. Defaults
            to kinoforge.core.reaper_actor.sweep.

    Raises:
        ValueError: when interval_s <= 0 at __init__ or reload().
    """

    def __init__(
        self,
        *,
        store: Any,
        ledger: Any,
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
        fields. Wakes _stop.wait(...) so a new interval takes effect on
        the next tick rather than after the old interval elapses.

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
        # Wake the in-flight sleep without latching the stop flag.
        self._stop.set()
        self._stop.clear()

    # ------------------------------------------------------------------
    # Internal — thread body
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Eager first tick, then sleep-and-tick until stopped."""
        while not self._stop.is_set():
            self._tick_once()
            self._stop.wait(self._interval_s)

    def _tick_once(self) -> None:
        """One sweep + persistence cycle, wrapped in broad try/except.

        Any exception from sweep, fold, or ledger.touch is logged via
        logger.exception (full stack) and swallowed. The loop is the
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
        except Exception:  # noqa: BLE001 — single bad tick must not kill the loop
            self._stats.errors_total += 1
            self._logger.exception(
                "sweep tick failed on host=%s", self._host
            )
```

- [ ] **Step 4: Run GREEN**

```
pixi run pytest tests/core/test_sweeper.py -v
```
Expected: 12 passed.

- [ ] **Step 5: Stage + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/sweeper.py \
    tests/core/test_sweeper.py
git add src/kinoforge/core/sweeper.py tests/core/test_sweeper.py
git commit -m "$(cat <<'EOF'
feat(b1): SweeperLoop substrate + _SweeperStats fold

Layer W background-thread daemon mirrors HeartbeatLoop shape (eager first
tick, Event.wait sleep, daemon=True + bounded join, broad try/except per
iter). _SweeperStats.fold counts destroys/errors/deferred-session-claim
from actions and HEARTBEAT_SUBSTRATE_MISSING/HEARTBEAT_UNKNOWN from
snapshot. reload() swaps policy/thresholds/interval atomically under
_reload_lock and wakes the sleep so new cadence applies immediately.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task b: `sweep()` filter for `sweeper:*` synthetic ids

**Goal:** Prevent the daemon's own liveness entry from being classified or reaped by adding a one-line prefix filter at the top of `sweep()`'s per-entry loop.

**Files:**
- Modify: `src/kinoforge/core/reaper_actor.py` (one-line insert at the top of the for-entry loop in `sweep`)
- Test: `tests/core/test_reaper_sweep.py` (+1 test)

**Acceptance Criteria:**
- [ ] A ledger containing 2 real pod entries + 1 `sweeper:host` entry → `sweep()` returns a `SweepReport` whose `snapshot` has exactly 2 keys; `sweeper:host` is absent.
- [ ] `provider_for` is never called with a `_sweeper` provider name (verified via spy `registry_get_provider`).
- [ ] Existing `test_reaper_sweep.py` cases still pass.

**Verify:** `pixi run pytest tests/core/test_reaper_sweep.py -v` → all green, including the new case.

**Steps:**

- [ ] **Step 1: RED — add the new test to `tests/core/test_reaper_sweep.py`**

Append:

```python
def test_sweep_filters_sweeper_prefix_synthetic_ids(tmp_path) -> None:
    """Layer W: sweep() must skip entries whose id starts with 'sweeper:'.

    Bug guard: the daemon writes its own liveness entry as
    'sweeper:<host>' with provider='_sweeper'. Without this filter,
    sweep() would resolve the provider via the registry (which has no
    '_sweeper' factory), demote the entry to UNROUTABLE, and operators
    running --force-forget would nuke the daemon's own heartbeat — a
    self-destruct race.
    """
    from kinoforge.core.clock import FakeClock
    from kinoforge.core.interfaces import Instance
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.core.reaper_actor import sweep
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=1000.0)
    # Two real entries + one synthetic.
    for entry_id in ("i-real-1", "i-real-2"):
        ledger.record(
            Instance(
                id=entry_id,
                provider="local",
                status="ready",
                created_at=clock.now(),
                cost_rate_usd_per_hr=0.10,
            )
        )
    ledger.record(
        Instance(
            id="sweeper:test-host",
            provider="_sweeper",
            status="ready",
            created_at=clock.now(),
            cost_rate_usd_per_hr=0.0,
        )
    )

    factory_calls: list[str] = []

    def registry_get_provider(name: str):  # noqa: ANN202
        factory_calls.append(name)
        raise KeyError(name)  # would force UNROUTABLE if reached

    report = sweep(
        store=store,
        ledger=ledger,
        registry_get_provider=registry_get_provider,
        thresholds={
            "idle_timeout_s": 7200.0,
            "max_lifetime_s": 28800.0,
            "heartbeat_interval_s": 30.0,
            "grace_after_session_s": 300.0,
        },
        clock=clock,
    )

    assert set(report.snapshot.keys()) == {"i-real-1", "i-real-2"}
    assert "_sweeper" not in factory_calls
```

- [ ] **Step 2: Run RED**

```
pixi run pytest tests/core/test_reaper_sweep.py::test_sweep_filters_sweeper_prefix_synthetic_ids -v
```
Expected: FAIL — `sweeper:test-host` appears in the snapshot keys; `factory_calls` contains `"_sweeper"`.

- [ ] **Step 3: GREEN — edit `src/kinoforge/core/reaper_actor.py:334`**

Find the per-entry loop (currently around `:334`):

```python
    for entry in entries:
        eid = str(entry["id"])
        provider = provider_for(entry, registry_get_provider, provider_cache)
```

Insert the filter immediately after `eid = str(entry["id"])`:

```python
    for entry in entries:
        eid = str(entry["id"])
        # Layer W: synthetic daemon-liveness entry written by SweeperLoop;
        # not a reapable pod. Reserved prefix at substrate level — joins
        # `_lifecycle` (run_id) and `_cost_cache` as the third reserved
        # kinoforge namespace. See B1 spec §4.4.
        if eid.startswith("sweeper:"):
            continue
        provider = provider_for(entry, registry_get_provider, provider_cache)
```

- [ ] **Step 4: Run GREEN**

```
pixi run pytest tests/core/test_reaper_sweep.py -v
```
Expected: all green including new case.

- [ ] **Step 5: Stage + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/reaper_actor.py \
    tests/core/test_reaper_sweep.py
git add src/kinoforge/core/reaper_actor.py tests/core/test_reaper_sweep.py
git commit -m "$(cat <<'EOF'
feat(b1): filter sweeper:* synthetic ids in sweep()

Reserved prefix at substrate level: joins _lifecycle (run_id) and
_cost_cache as third reserved kinoforge namespace. Prevents daemon's own
liveness entry from being demoted to UNROUTABLE and force-forgotten by
operators running `kinoforge reap --apply --force-forget`.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task c: `SweeperConfig` pydantic model + `Config.sweeper` + policy bridge

**Goal:** Add the YAML surface and the policy-bridge function the CLI will use to compose `DEFAULT_APPLY_POLICY ∪ opt-ins`.

**Files:**
- Modify: `src/kinoforge/core/config.py` (+~30 LOC)
- Test: `tests/core/test_config.py` (+3 tests)

**Acceptance Criteria:**
- [ ] `Config` loaded from a YAML without a `sweeper:` block has `cfg.sweeper.interval_s == 60.0`, `include_orphans == False`, `force_forget == False`, `host is None`.
- [ ] YAML `sweeper.interval_s: -1` → pydantic `ValidationError` at load time.
- [ ] `sweeper_policy_from_cfg(cfg)` with both flags off returns `DEFAULT_APPLY_POLICY`. With `include_orphans=True` adds `ORPHAN_REAP`. With `force_forget=True` adds `UNROUTABLE`.

**Verify:** `pixi run pytest tests/core/test_config.py -v -k sweeper` → 3 passed.

**Steps:**

- [ ] **Step 1: RED — append to `tests/core/test_config.py`**

```python
def test_sweeper_config_defaults_load(tmp_path) -> None:
    """No `sweeper:` block in YAML → SweeperConfig defaults apply.

    Bug guard: a missing default would make every existing YAML break on
    upgrade. Field(default_factory=SweeperConfig) is mandatory.
    """
    from kinoforge.core.config import load_config

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        "compute:\n"
        "  provider: local\n"
        "  image: dummy\n"
        "engine:\n"
        "  kind: fake\n"
        "models: []\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.sweeper.interval_s == 60.0
    assert cfg.sweeper.include_orphans is False
    assert cfg.sweeper.force_forget is False
    assert cfg.sweeper.host is None


def test_sweeper_interval_negative_rejected(tmp_path) -> None:
    """sweeper.interval_s <= 0 must be rejected at load time.

    Bug guard: a zero/negative interval at YAML level would either spin-
    loop the daemon or never tick — SweeperLoop.__init__ already raises,
    but the YAML must fail fast before the daemon binary even starts.
    """
    import pydantic
    from kinoforge.core.config import load_config

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        "compute:\n"
        "  provider: local\n"
        "  image: dummy\n"
        "engine:\n"
        "  kind: fake\n"
        "models: []\n"
        "sweeper:\n"
        "  interval_s: -1\n"
    )
    with pytest.raises(pydantic.ValidationError, match="interval_s must be > 0"):
        load_config(cfg_path)


def test_sweeper_policy_bridge_composes_correctly() -> None:
    """sweeper_policy_from_cfg ∪'s DEFAULT_APPLY_POLICY with the two opt-ins.

    Bug guard: a misnamed verdict in the bridge would silently fail to
    add ORPHAN_REAP / UNROUTABLE — the daemon would run dry-run forever
    despite the operator's YAML.
    """
    from kinoforge.core.config import (
        Config,
        ComputeConfig,
        EngineConfig,
        SweeperConfig,
        sweeper_policy_from_cfg,
    )
    from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict

    base = Config(
        compute=ComputeConfig(provider="local", image="x"),
        engine=EngineConfig(kind="fake"),
        models=[],
        sweeper=SweeperConfig(),
    )
    assert sweeper_policy_from_cfg(base).act_verdicts == DEFAULT_APPLY_POLICY.act_verdicts

    with_orphans = base.model_copy(
        update={"sweeper": SweeperConfig(include_orphans=True)}
    )
    p = sweeper_policy_from_cfg(with_orphans)
    assert Verdict.ORPHAN_REAP in p.act_verdicts
    assert DEFAULT_APPLY_POLICY.act_verdicts <= p.act_verdicts

    with_force = base.model_copy(
        update={"sweeper": SweeperConfig(force_forget=True)}
    )
    p = sweeper_policy_from_cfg(with_force)
    assert Verdict.UNROUTABLE in p.act_verdicts
```

- [ ] **Step 2: Run RED**

```
pixi run pytest tests/core/test_config.py -v -k sweeper
```
Expected: collection errors / ImportError on `SweeperConfig` and `sweeper_policy_from_cfg`.

- [ ] **Step 3: GREEN — edit `src/kinoforge/core/config.py`**

Locate the existing `Config` class and adjacent imports. Add the new model + bridge near the bottom of the file (after `Config`). Import additions go at the top.

Top-of-file imports — ensure these are present (add only the missing ones):

```python
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Policy, Verdict
```

After existing nested-model classes (e.g. after `EngineConfig`), add:

```python
class SweeperConfig(BaseModel):
    """YAML surface for the Layer W sweeper daemon.

    Default sleeps at 60s — gentle on RunPod GraphQL (B5a smoke measured
    P50=460ms, P99=583ms; ~100x headroom at 60s). Two opt-in policy
    flags extend DEFAULT_APPLY_POLICY:
      - include_orphans → adds ORPHAN_REAP
      - force_forget → adds UNROUTABLE

    Host defaults to socket.gethostname() at CLI level when None.
    """

    interval_s: float = 60.0
    include_orphans: bool = False
    force_forget: bool = False
    host: str | None = None

    @field_validator("interval_s")
    @classmethod
    def _validate_interval(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"sweeper.interval_s must be > 0; got {v}")
        return v
```

In `Config`, add the field (in alphabetical/logical order with peers):

```python
class Config(BaseModel):
    # ... existing fields ...
    sweeper: SweeperConfig = Field(default_factory=SweeperConfig)
```

After `Config`, add the bridge function:

```python
def sweeper_policy_from_cfg(cfg: Config) -> Policy:
    """Build the Layer W daemon's Policy from cfg.sweeper.

    Starts with Layer V DEFAULT_APPLY_POLICY (IDLE_REAP, OVERAGE_REAP,
    STALE_LEDGER) and unions the two opt-in verdicts based on YAML flags.

    Args:
        cfg: Loaded Config; cfg.sweeper is consulted.

    Returns:
        Policy with the resulting frozenset.
    """
    act = set(DEFAULT_APPLY_POLICY.act_verdicts)
    if cfg.sweeper.include_orphans:
        act.add(Verdict.ORPHAN_REAP)
    if cfg.sweeper.force_forget:
        act.add(Verdict.UNROUTABLE)
    return Policy(act_verdicts=frozenset(act))
```

- [ ] **Step 4: Run GREEN**

```
pixi run pytest tests/core/test_config.py -v -k sweeper
```
Expected: 3 passed.

Also run the full config test suite to confirm no regression:

```
pixi run pytest tests/core/test_config.py -v
```

- [ ] **Step 5: Stage + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/config.py \
    tests/core/test_config.py
git add src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "$(cat <<'EOF'
feat(b1): SweeperConfig pydantic model + policy bridge

Adds cfg.sweeper YAML block (interval_s default 60s; include_orphans +
force_forget opt-ins; host=None → socket.gethostname() at CLI level).
sweeper_policy_from_cfg(cfg) composes DEFAULT_APPLY_POLICY ∪ opt-ins.
Existing YAMLs load unchanged via Field(default_factory=SweeperConfig).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task d: `sweeper_metrics.py` — human / JSON / Prom renderers

**Goal:** Pure renderer module for the three output modes of `sweeper status` + `sweeper metrics`. No I/O; takes a ledger-entry-shaped dict + `interval_s` + `host` and returns text/dict.

**Files:**
- Create: `src/kinoforge/core/sweeper_metrics.py`
- Test: `tests/core/test_sweeper_metrics.py`

**Acceptance Criteria:**
- [ ] `render_metrics_prom(entry, *, host, interval_s)` emits all 6 series: `kinoforge_sweeper_last_sweep_ts`, `kinoforge_sweeper_sweeps_total`, `kinoforge_sweeper_destroys_total`, `kinoforge_sweeper_deferred_total` (3 reason labels: `session-claim`, `heartbeat-unknown-skipped`, `heartbeat-substrate-missing`), `kinoforge_sweeper_errors_total`, `kinoforge_sweeper_interval_s`. Every metric carries `host="<host>"`. UTF-8, LF-only line endings.
- [ ] `render_metrics_prom` with `entry=None` (no daemon running) omits `kinoforge_sweeper_last_sweep_ts` but emits zero counters on every other series with the host label.
- [ ] `render_status_json(entry, *, host, interval_s, now)` emits the §4.6.3 schema (keys: host, pid, running, last_sweep_ts, last_sweep_age_s, interval_s, stale, sweeps_total, destroys_total, deferred_total{}, errors_total).
- [ ] `stale = last_sweep_age_s > 3 * interval_s` — boundary verified (eq is not stale).
- [ ] `render_status_human(entry, *, host, interval_s, now)` emits `key=value` lines sibling to `kinoforge status` style.
- [ ] `running == False` when `entry` is `None` OR `entry["pid"]` is missing/zero.

**Verify:** `pixi run pytest tests/core/test_sweeper_metrics.py -v` → 6 passed.

**Steps:**

- [ ] **Step 1: RED — write `tests/core/test_sweeper_metrics.py`**

```python
"""Layer W: sweeper_metrics renderers (human / JSON / Prom).

Pure functions; no fixtures beyond constructed dicts.
"""

from __future__ import annotations

import json

from kinoforge.core.sweeper_metrics import (
    render_metrics_prom,
    render_status_human,
    render_status_json,
)


def _live_entry(*, now: float = 2000.0) -> dict:
    return {
        "id": "sweeper:hostname.local",
        "provider": "_sweeper",
        "pid": 12345,
        "last_heartbeat": now - 8.0,
        "heartbeat_thread_tick": now - 8.0,
        "sweeps_total": 1421,
        "destroys_total": 17,
        "errors_total": 0,
        "deferred_session_claim": 3,
        "deferred_heartbeat_unknown_skipped": 0,
        "deferred_heartbeat_substrate_missing": 0,
    }


def test_prom_format_emits_all_required_series() -> None:
    """All six required metric series emit, each with host label and
    HELP+TYPE lines. UTF-8 + LF endings."""
    entry = _live_entry(now=2000.0)
    out = render_metrics_prom(entry, host="hostname.local", interval_s=60.0)
    # LF-only.
    assert "\r\n" not in out and "\r" not in out
    # Series names.
    for name in (
        "kinoforge_sweeper_last_sweep_ts",
        "kinoforge_sweeper_sweeps_total",
        "kinoforge_sweeper_destroys_total",
        "kinoforge_sweeper_deferred_total",
        "kinoforge_sweeper_errors_total",
        "kinoforge_sweeper_interval_s",
    ):
        assert f"# HELP {name}" in out
        assert f"# TYPE {name}" in out
    # All three deferred reasons emit even at zero.
    for reason in (
        "session-claim",
        "heartbeat-unknown-skipped",
        "heartbeat-substrate-missing",
    ):
        assert f'reason="{reason}"' in out
    # Host label on every series.
    assert out.count('host="hostname.local"') >= 6


def test_prom_omits_last_sweep_ts_when_no_entry() -> None:
    """No ledger entry → no last_sweep_ts series; zero counters elsewhere."""
    out = render_metrics_prom(None, host="hostname.local", interval_s=60.0)
    assert "kinoforge_sweeper_last_sweep_ts" not in out
    assert 'kinoforge_sweeper_sweeps_total{host="hostname.local"} 0' in out
    assert 'kinoforge_sweeper_errors_total{host="hostname.local"} 0' in out
    # All three deferred reasons still emit.
    assert (
        'kinoforge_sweeper_deferred_total{host="hostname.local",reason="session-claim"} 0'
        in out
    )


def test_json_shape_lock_matches_status_spec() -> None:
    """Stable shape per spec §4.6.3."""
    entry = _live_entry(now=2000.0)
    out_str = render_status_json(
        entry, host="hostname.local", interval_s=60.0, now=2000.0
    )
    out = json.loads(out_str)
    assert out["host"] == "hostname.local"
    assert out["pid"] == 12345
    assert out["running"] is True
    assert out["last_sweep_age_s"] == 8
    assert out["interval_s"] == 60
    assert out["stale"] is False
    assert out["sweeps_total"] == 1421
    assert out["destroys_total"] == 17
    assert out["errors_total"] == 0
    assert out["deferred_total"] == {
        "session-claim": 3,
        "heartbeat-unknown-skipped": 0,
        "heartbeat-substrate-missing": 0,
    }


def test_json_stale_flag_computed_correctly() -> None:
    """stale iff last_sweep_age_s > 3 * interval_s. Boundary check."""
    interval = 60.0
    entry = _live_entry(now=2000.0)
    # exactly 3x — not stale (strict >)
    entry["heartbeat_thread_tick"] = 2000.0 - 180.0
    out = json.loads(
        render_status_json(entry, host="h", interval_s=interval, now=2000.0)
    )
    assert out["stale"] is False
    # just over 3x — stale
    entry["heartbeat_thread_tick"] = 2000.0 - 180.5
    out = json.loads(
        render_status_json(entry, host="h", interval_s=interval, now=2000.0)
    )
    assert out["stale"] is True


def test_human_render_key_value_style() -> None:
    """Renders sibling-of-`kinoforge status` key=value lines."""
    entry = _live_entry(now=2000.0)
    out = render_status_human(
        entry, host="hostname.local", interval_s=60.0, now=2000.0
    )
    # No JSON braces, no Prom HELP lines.
    assert "{" not in out and "# HELP" not in out
    for line in (
        "host=hostname.local",
        "running=true",
        "pid=12345",
        "interval_s=60",
        "stale=false",
        "sweeps_total=1421",
        "destroys_total=17",
        "errors_total=0",
        "deferred_session_claim=3",
    ):
        assert line in out


def test_running_false_when_pid_missing() -> None:
    """running=false when entry present but pid missing/zero, or entry None."""
    entry = _live_entry(now=2000.0)
    entry.pop("pid")
    out = json.loads(
        render_status_json(entry, host="h", interval_s=60.0, now=2000.0)
    )
    assert out["running"] is False
    # Also when entry is None.
    out = json.loads(
        render_status_json(None, host="h", interval_s=60.0, now=2000.0)
    )
    assert out["running"] is False
    assert out["pid"] is None
```

- [ ] **Step 2: Run RED**

```
pixi run pytest tests/core/test_sweeper_metrics.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: GREEN — write `src/kinoforge/core/sweeper_metrics.py`**

```python
"""Layer W: pure renderers for `kinoforge sweeper status` and `metrics`.

Three output shapes share a single input (entry dict from
ledger.read('sweeper:<host>') + cfg.sweeper.interval_s + clock.now()):

  - render_status_human(entry, *, host, interval_s, now) -> str
  - render_status_json(entry, *, host, interval_s, now) -> str
  - render_metrics_prom(entry, *, host, interval_s) -> str

Pure: no I/O, no threading, no global state. The CLI does the ledger
read; this module folds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

_DEFERRED_REASONS: tuple[tuple[str, str], ...] = (
    ("session-claim", "deferred_session_claim"),
    ("heartbeat-unknown-skipped", "deferred_heartbeat_unknown_skipped"),
    ("heartbeat-substrate-missing", "deferred_heartbeat_substrate_missing"),
)


def _running(entry: Mapping[str, Any] | None) -> bool:
    """A daemon is running when an entry exists and carries a non-zero pid."""
    if entry is None:
        return False
    pid = entry.get("pid")
    try:
        return bool(int(pid)) if pid is not None else False
    except (TypeError, ValueError):
        return False


def _stats_view(
    entry: Mapping[str, Any] | None,
    *,
    interval_s: float,
    now: float,
) -> dict[str, Any]:
    """Project the ledger entry into the dict consumed by all three renderers."""
    if entry is None:
        return {
            "running": False,
            "pid": None,
            "last_sweep_ts": None,
            "last_sweep_age_s": None,
            "stale": False,
            "sweeps_total": 0,
            "destroys_total": 0,
            "errors_total": 0,
            "deferred_total": {label: 0 for label, _ in _DEFERRED_REASONS},
        }
    pid_raw = entry.get("pid")
    try:
        pid = int(pid_raw) if pid_raw is not None else None
    except (TypeError, ValueError):
        pid = None
    last_tick = entry.get("heartbeat_thread_tick")
    if last_tick is None:
        return {
            "running": False,
            "pid": pid,
            "last_sweep_ts": None,
            "last_sweep_age_s": None,
            "stale": False,
            "sweeps_total": int(entry.get("sweeps_total", 0)),
            "destroys_total": int(entry.get("destroys_total", 0)),
            "errors_total": int(entry.get("errors_total", 0)),
            "deferred_total": {
                label: int(entry.get(key, 0)) for label, key in _DEFERRED_REASONS
            },
        }
    last_tick_f = float(last_tick)
    age = int(now - last_tick_f)
    stale = (now - last_tick_f) > 3.0 * interval_s
    return {
        "running": _running(entry),
        "pid": pid,
        "last_sweep_ts": last_tick_f,
        "last_sweep_age_s": age,
        "stale": stale,
        "sweeps_total": int(entry.get("sweeps_total", 0)),
        "destroys_total": int(entry.get("destroys_total", 0)),
        "errors_total": int(entry.get("errors_total", 0)),
        "deferred_total": {
            label: int(entry.get(key, 0)) for label, key in _DEFERRED_REASONS
        },
    }


def render_status_json(
    entry: Mapping[str, Any] | None,
    *,
    host: str,
    interval_s: float,
    now: float,
) -> str:
    """Render the §4.6.3 stable JSON schema."""
    view = _stats_view(entry, interval_s=interval_s, now=now)
    out: dict[str, Any] = {
        "host": host,
        "pid": view["pid"],
        "running": view["running"],
        "last_sweep_ts": (
            datetime.fromtimestamp(view["last_sweep_ts"]).astimezone().isoformat()
            if view["last_sweep_ts"] is not None
            else None
        ),
        "last_sweep_age_s": view["last_sweep_age_s"],
        "interval_s": int(interval_s) if interval_s == int(interval_s) else interval_s,
        "stale": view["stale"],
        "sweeps_total": view["sweeps_total"],
        "destroys_total": view["destroys_total"],
        "deferred_total": view["deferred_total"],
        "errors_total": view["errors_total"],
    }
    return json.dumps(out, sort_keys=False)


def render_status_human(
    entry: Mapping[str, Any] | None,
    *,
    host: str,
    interval_s: float,
    now: float,
) -> str:
    """Render sibling-of-`kinoforge status` key=value lines."""
    view = _stats_view(entry, interval_s=interval_s, now=now)
    iv = int(interval_s) if interval_s == int(interval_s) else interval_s
    lines = [
        f"host={host}",
        f"running={str(view['running']).lower()}",
        f"pid={view['pid'] if view['pid'] is not None else 'none'}",
        (
            "last_sweep_ts="
            f"{datetime.fromtimestamp(view['last_sweep_ts']).astimezone().isoformat()}"
            if view["last_sweep_ts"] is not None
            else "last_sweep_ts=none"
        ),
        (
            f"last_sweep_age_s={view['last_sweep_age_s']}"
            if view["last_sweep_age_s"] is not None
            else "last_sweep_age_s=none"
        ),
        f"interval_s={iv}",
        f"stale={str(view['stale']).lower()}",
        f"sweeps_total={view['sweeps_total']}",
        f"destroys_total={view['destroys_total']}",
    ]
    for label, _ in _DEFERRED_REASONS:
        key = "deferred_" + label.replace("-", "_")
        lines.append(f"{key}={view['deferred_total'][label]}")
    lines.append(f"errors_total={view['errors_total']}")
    return "\n".join(lines) + "\n"


def render_metrics_prom(
    entry: Mapping[str, Any] | None,
    *,
    host: str,
    interval_s: float,
) -> str:
    """Render Prometheus text exposition (textfile-collector cron target).

    Sibling of B2 `kinoforge cost --prom` prefix (`kinoforge_*`). LF
    line endings; UTF-8.
    """
    view = _stats_view(entry, interval_s=interval_s, now=0.0)
    parts: list[str] = []

    if view["last_sweep_ts"] is not None:
        parts.extend(
            [
                "# HELP kinoforge_sweeper_last_sweep_ts Unix timestamp of most recent successful sweep.",
                "# TYPE kinoforge_sweeper_last_sweep_ts gauge",
                f'kinoforge_sweeper_last_sweep_ts{{host="{host}"}} {int(view["last_sweep_ts"])}',
                "",
            ]
        )
    parts.extend(
        [
            "# HELP kinoforge_sweeper_sweeps_total Cumulative sweeps since daemon start.",
            "# TYPE kinoforge_sweeper_sweeps_total counter",
            f'kinoforge_sweeper_sweeps_total{{host="{host}"}} {view["sweeps_total"]}',
            "",
            "# HELP kinoforge_sweeper_destroys_total Cumulative pods destroyed since daemon start.",
            "# TYPE kinoforge_sweeper_destroys_total counter",
            f'kinoforge_sweeper_destroys_total{{host="{host}"}} {view["destroys_total"]}',
            "",
            "# HELP kinoforge_sweeper_deferred_total Sweeps that skipped a pod for a known reason.",
            "# TYPE kinoforge_sweeper_deferred_total counter",
        ]
    )
    for label, _ in _DEFERRED_REASONS:
        parts.append(
            f'kinoforge_sweeper_deferred_total{{host="{host}",reason="{label}"}} '
            f"{view['deferred_total'][label]}"
        )
    parts.extend(
        [
            "",
            "# HELP kinoforge_sweeper_errors_total Per-tick exceptions caught by the loop body.",
            "# TYPE kinoforge_sweeper_errors_total counter",
            f'kinoforge_sweeper_errors_total{{host="{host}"}} {view["errors_total"]}',
            "",
            "# HELP kinoforge_sweeper_interval_s Configured sweep cadence.",
            "# TYPE kinoforge_sweeper_interval_s gauge",
            (
                f'kinoforge_sweeper_interval_s{{host="{host}"}} '
                f"{int(interval_s) if interval_s == int(interval_s) else interval_s}"
            ),
            "",
        ]
    )
    return "\n".join(parts)
```

- [ ] **Step 4: Run GREEN**

```
pixi run pytest tests/core/test_sweeper_metrics.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Stage + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/sweeper_metrics.py \
    tests/core/test_sweeper_metrics.py
git add src/kinoforge/core/sweeper_metrics.py tests/core/test_sweeper_metrics.py
git commit -m "$(cat <<'EOF'
feat(b1): sweeper_metrics pure renderers (human / JSON / Prom)

Three output modes share one input (ledger entry + interval + clock.now()).
Prom exposition siblings B2 prefix; six series including labelled
deferred_total{reason=...} for session-claim, heartbeat-unknown-skipped,
heartbeat-substrate-missing. JSON schema is stable per spec §4.6.3.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task e: CLI `sweeper start | stop | status | metrics` subcommand family

**Goal:** Wire the daemon and renderers into the CLI: argparse subparser; four `_cmd_sweeper_*` handlers; signal handlers; synthetic `Instance` materialised via `Ledger.record` at startup.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (+~180 LOC)
- Modify: `src/kinoforge/cli/_main.py` (+~15 LOC)
- Test: `tests/cli/test_cmd_sweeper.py` (CREATE)

**Acceptance Criteria:**
- [ ] `kinoforge sweeper status` with no entry → prints `running=false`, exit 0.
- [ ] `kinoforge sweeper status --json` matches the §4.6.3 schema (parses, contains all required keys).
- [ ] `kinoforge sweeper metrics --prom` matches the §4.6.4 series list (all 6 series; UTF-8 + LF).
- [ ] `kinoforge sweeper stop` with no entry → prints `no sweeper running on host=<host>` to stderr, exit 1.
- [ ] `kinoforge sweeper status` with an injected stale entry (heartbeat_thread_tick > 3*interval_s in the past) → `stale=true`.
- [ ] `kinoforge sweeper start` (with the loop start mocked out) emits the §4.7 banner at INFO before signal-handler installation.
- [ ] `kinoforge sweeper start` (with the loop start mocked out) calls `ledger.record(Instance(id='sweeper:<host>', provider='_sweeper', tags={}, created_at=<now>, cost_rate_usd_per_hr=0.0))` once, then `ledger.touch('sweeper:<host>', pid=os.getpid())` once.

**Verify:** `pixi run pytest tests/cli/test_cmd_sweeper.py -v` → 7 passed.

**Steps:**

- [ ] **Step 1: RED — write `tests/cli/test_cmd_sweeper.py`**

```python
"""Layer W: offline CLI tests for `kinoforge sweeper`.

All paths exercised against LocalArtifactStore + LocalProvider on tmp_path.
SweeperLoop.start is patched to no-op so `start` exits without spawning
the background thread (xprocess tests cover the live spawn path).
"""

from __future__ import annotations

import io
import json
import logging
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import pytest

from kinoforge._adapters import register_all_builtin_adapters  # noqa: F401  side-effect
from kinoforge.cli._commands import (
    _cmd_sweeper_metrics,
    _cmd_sweeper_start,
    _cmd_sweeper_status,
    _cmd_sweeper_stop,
)


def _make_ctx(tmp_path, *, sweeper_block: str = ""):
    """Build a SessionContext on LocalArtifactStore + LocalProvider."""
    from kinoforge.cli._commands import SessionContext
    from kinoforge.core.config import load_config

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        "compute:\n"
        "  provider: local\n"
        "  image: dummy\n"
        "engine:\n"
        "  kind: fake\n"
        "models: []\n"
        + sweeper_block
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cfg = load_config(cfg_path)
    ctx = SessionContext(
        cfg=cfg,
        cfg_path=str(cfg_path),
        run_id="r",
        state_dir=str(state_dir),
    )
    return ctx, cfg_path


# ---------------------------------------------------------------------------
# status / metrics
# ---------------------------------------------------------------------------


def test_cmd_sweeper_status_no_entry(tmp_path) -> None:
    """No sweeper running → running=false, exit 0."""
    ctx, _ = _make_ctx(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = _cmd_sweeper_status(_args(json=False), ctx)
    assert rc == 0
    assert "running=false" in out.getvalue()


def test_cmd_sweeper_status_json_shape(tmp_path) -> None:
    """`--json` parses; every required key present."""
    ctx, _ = _make_ctx(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = _cmd_sweeper_status(_args(json=True), ctx)
    assert rc == 0
    body = json.loads(out.getvalue())
    for k in (
        "host",
        "pid",
        "running",
        "last_sweep_ts",
        "last_sweep_age_s",
        "interval_s",
        "stale",
        "sweeps_total",
        "destroys_total",
        "deferred_total",
        "errors_total",
    ):
        assert k in body, f"missing key {k!r}"


def test_cmd_sweeper_metrics_prom_format(tmp_path) -> None:
    """--prom output contains all required series + LF-only line endings."""
    ctx, _ = _make_ctx(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = _cmd_sweeper_metrics(_args(prom=True), ctx)
    assert rc == 0
    body = out.getvalue()
    assert "\r" not in body
    for series in (
        "kinoforge_sweeper_sweeps_total",
        "kinoforge_sweeper_destroys_total",
        "kinoforge_sweeper_deferred_total",
        "kinoforge_sweeper_errors_total",
        "kinoforge_sweeper_interval_s",
    ):
        assert series in body


def test_cmd_sweeper_stop_no_entry(tmp_path) -> None:
    """No sweeper running → stderr message + exit 1."""
    ctx, _ = _make_ctx(tmp_path)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = _cmd_sweeper_stop(_args(), ctx)
    assert rc == 1
    assert "no sweeper running" in err.getvalue()


def test_cmd_sweeper_status_stale_flag(tmp_path) -> None:
    """An entry whose heartbeat_thread_tick is older than 3*interval → stale=true."""
    import time as _t

    from kinoforge.core.interfaces import Instance

    ctx, _ = _make_ctx(tmp_path, sweeper_block="sweeper:\n  interval_s: 1\n")
    ledger = ctx.ledger()
    host = "test-host"
    # Inject a synthetic entry whose tick is far in the past.
    ledger.record(
        Instance(
            id=f"sweeper:{host}",
            provider="_sweeper",
            status="ready",
            created_at=_t.time() - 100.0,
            cost_rate_usd_per_hr=0.0,
        )
    )
    ledger.touch(
        f"sweeper:{host}",
        last_heartbeat=_t.time() - 100.0,
        heartbeat_thread_tick=_t.time() - 100.0,
        pid=os.getpid(),
    )
    out = io.StringIO()
    # _cmd_sweeper_status auto-detects host via socket.gethostname; override:
    with patch("socket.gethostname", return_value=host), redirect_stdout(out):
        rc = _cmd_sweeper_status(_args(json=True), ctx)
    assert rc == 0
    body = json.loads(out.getvalue())
    assert body["stale"] is True


def test_banner_emitted_on_start_dry_run(tmp_path, caplog) -> None:
    """`start` emits the §4.7 banner at INFO before installing handlers."""
    ctx, cfg_path = _make_ctx(tmp_path)
    with (
        patch("kinoforge.core.sweeper.SweeperLoop.start", lambda self: None),
        patch("threading.Event.wait", return_value=True),  # exit block immediately
        patch("signal.signal"),  # don't install real handlers
        caplog.at_level(logging.INFO, logger="kinoforge.cli._commands"),
    ):
        rc = _cmd_sweeper_start(_args(config=str(cfg_path), interval_s=None), ctx)
    assert rc == 0
    joined = " ".join(rec.message for rec in caplog.records)
    assert "kinoforge sweeper starting" in joined
    assert "B5a heartbeat-substrate gate is ACTIVE" in joined
    assert "B7 cooperative session-claim probe is ACTIVE" in joined


def test_cmd_sweeper_start_records_synthetic_entry(tmp_path) -> None:
    """Start materialises sweeper:<host> via Ledger.record + sets pid via touch."""
    import socket

    ctx, cfg_path = _make_ctx(tmp_path)
    host = socket.gethostname()
    with (
        patch("kinoforge.core.sweeper.SweeperLoop.start", lambda self: None),
        patch("threading.Event.wait", return_value=True),
        patch("signal.signal"),
    ):
        rc = _cmd_sweeper_start(_args(config=str(cfg_path), interval_s=None), ctx)
    assert rc == 0
    entry = ctx.ledger().read(f"sweeper:{host}")
    assert entry is not None
    assert entry["provider"] == "_sweeper"
    assert int(entry["pid"]) == os.getpid()
    assert entry["cost_rate_usd_per_hr"] == 0.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _args(**overrides):
    """Build an argparse.Namespace-like with sensible defaults."""
    import argparse

    base = argparse.Namespace(
        json=False, prom=False, config=None, interval_s=None
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base
```

- [ ] **Step 2: Run RED**

```
pixi run pytest tests/cli/test_cmd_sweeper.py -v
```
Expected: ImportError on the `_cmd_sweeper_*` symbols.

- [ ] **Step 3: GREEN — add command handlers to `src/kinoforge/cli/_commands.py`**

Append below `_cmd_cost` (at end of file):

```python
def _cmd_sweeper_start(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Layer W: foreground sweeper daemon supervisor.

    Blocks until SIGTERM. Operator wraps under systemd / nohup / docker
    PID 1 / tmux. Materialises the synthetic `sweeper:<host>` ledger
    entry (§4.4 init), prints the §4.7 banner, installs SIGTERM /
    SIGHUP / SIGUSR1 handlers, then starts the SweeperLoop.
    """
    import signal
    import socket
    import threading
    from datetime import datetime

    from kinoforge.core import registry
    from kinoforge.core.config import load_config, sweeper_policy_from_cfg
    from kinoforge.core.interfaces import Instance
    from kinoforge.core.sweeper import SweeperLoop, _SweeperStats

    cfg = ctx.cfg
    cfg_path = args.config or ctx.cfg_path
    host = cfg.sweeper.host or socket.gethostname()
    interval_s = float(args.interval_s) if args.interval_s else cfg.sweeper.interval_s
    if interval_s <= 0:
        logger.error("invalid interval_s=%s", interval_s)
        return 2
    policy = sweeper_policy_from_cfg(cfg)
    lc = cfg.lifecycle()
    thresholds = {
        "idle_timeout_s": float(lc.idle_timeout_s),
        "max_lifetime_s": float(lc.max_lifetime_s),
        "heartbeat_interval_s": (
            float(lc.heartbeat_interval_s) if lc.heartbeat_interval_s else None
        ),
        "grace_after_session_s": float(lc.grace_after_session_s),
    }
    ledger = ctx.ledger()
    store = ledger._store  # SessionContext provides store via ledger

    # Banner — §4.7. Must precede signal-handler installation so operators
    # see the contract on first log line.
    pid = os.getpid()
    logger.info(
        "kinoforge sweeper starting host=%s interval_s=%s policy=%s "
        "include_orphans=%s force_forget=%s pid=%s",
        host,
        interval_s,
        sorted(v.value for v in policy.act_verdicts),
        cfg.sweeper.include_orphans,
        cfg.sweeper.force_forget,
        pid,
    )
    logger.info(
        "B5a heartbeat-substrate gate is ACTIVE: providers with no "
        "shipped HeartbeatEndpoint satisfier emit HEARTBEAT_SUBSTRATE_MISSING "
        "and are NEVER reaped. SkyPilot is the only such provider today; "
        "B5b ships the satisfier when GPU quota lands. WARN-once-per-"
        "(provider,instance_id) deduped."
    )
    logger.info(
        "B7 cooperative session-claim probe is ACTIVE: entries whose "
        "orchestrator holds provision:<id> emit "
        "action=\"deferred-session-claim\" and are skipped this pass; "
        "the next sweep re-evaluates."
    )

    # Materialise the synthetic liveness entry (§4.4).
    synthetic = Instance(
        id=f"sweeper:{host}",
        provider="_sweeper",
        status="ready",
        created_at=datetime.now().timestamp(),
        cost_rate_usd_per_hr=0.0,
    )
    ledger.record(synthetic)
    ledger.touch(f"sweeper:{host}", pid=pid)

    stats = _SweeperStats()
    loop = SweeperLoop(
        store=store,
        ledger=ledger,
        registry_get_provider=registry.get_provider,
        thresholds=thresholds,
        interval_s=interval_s,
        host=host,
        policy=policy,
        stats=stats,
    )

    exit_event = threading.Event()

    def _handle_sigterm(_signum, _frame):
        exit_event.set()

    def _handle_sighup(_signum, _frame):
        try:
            new_cfg = load_config(cfg_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIGHUP: cfg reload failed: %s", exc)
            return
        new_policy = sweeper_policy_from_cfg(new_cfg)
        new_lc = new_cfg.lifecycle()
        new_thresholds = {
            "idle_timeout_s": float(new_lc.idle_timeout_s),
            "max_lifetime_s": float(new_lc.max_lifetime_s),
            "heartbeat_interval_s": (
                float(new_lc.heartbeat_interval_s)
                if new_lc.heartbeat_interval_s
                else None
            ),
            "grace_after_session_s": float(new_lc.grace_after_session_s),
        }
        loop.reload(
            policy=new_policy,
            thresholds=new_thresholds,
            interval_s=new_cfg.sweeper.interval_s,
        )
        logger.info("SIGHUP: cfg reloaded from %s", cfg_path)

    def _handle_sigusr1(_signum, _frame):
        logger.info("sweeper stats: %s", stats.snapshot_for_log())

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGHUP, _handle_sighup)
    signal.signal(signal.SIGUSR1, _handle_sigusr1)

    loop.start()
    exit_event.wait()  # block until SIGTERM
    loop.stop()
    return 0


def _cmd_sweeper_stop(args: argparse.Namespace, ctx: SessionContext) -> int:  # noqa: ARG001
    """Layer W: send SIGTERM to the daemon owning this host's sweeper entry."""
    import signal
    import socket
    import sys
    import time

    cfg = ctx.cfg
    host = cfg.sweeper.host or socket.gethostname()
    ledger = ctx.ledger()
    entry = ledger.read(f"sweeper:{host}")
    if entry is None:
        sys.stderr.write(f"no sweeper running on host={host}\n")
        return 1
    pid = entry.get("pid")
    try:
        pid_int = int(pid) if pid is not None else 0
    except (TypeError, ValueError):
        pid_int = 0
    if not pid_int:
        sys.stderr.write(f"daemon liveness entry has no pid on host={host} (stale?)\n")
        return 1
    try:
        os.kill(pid_int, signal.SIGTERM)
    except ProcessLookupError:
        sys.stderr.write(f"pid {pid_int} no longer alive on host={host}\n")
        return 1
    # Poll until heartbeat_thread_tick stops advancing for 2 consecutive polls.
    deadline = time.monotonic() + 30.0
    last_tick = entry.get("heartbeat_thread_tick", 0.0)
    stable_polls = 0
    while time.monotonic() < deadline:
        time.sleep(1.0)
        entry = ledger.read(f"sweeper:{host}")
        if entry is None:
            return 0
        tick = entry.get("heartbeat_thread_tick", 0.0)
        if tick == last_tick:
            stable_polls += 1
            if stable_polls >= 2:
                return 0
        else:
            stable_polls = 0
            last_tick = tick
    sys.stderr.write(f"sweeper on host={host} did not stop within 30s\n")
    return 2


def _cmd_sweeper_status(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Layer W: render sweeper liveness — human (default) or --json."""
    import socket
    import sys
    from datetime import datetime

    from kinoforge.core.sweeper_metrics import (
        render_status_human,
        render_status_json,
    )

    cfg = ctx.cfg
    host = cfg.sweeper.host or socket.gethostname()
    ledger = ctx.ledger()
    entry = ledger.read(f"sweeper:{host}")
    now = datetime.now().timestamp()
    if args.json:
        sys.stdout.write(
            render_status_json(
                entry, host=host, interval_s=cfg.sweeper.interval_s, now=now
            )
            + "\n"
        )
    else:
        sys.stdout.write(
            render_status_human(
                entry, host=host, interval_s=cfg.sweeper.interval_s, now=now
            )
        )
    return 0


def _cmd_sweeper_metrics(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Layer W: render Prom textfile-collector target."""
    import socket
    import sys

    from kinoforge.core.sweeper_metrics import render_metrics_prom

    if not args.prom:
        sys.stderr.write("kinoforge sweeper metrics requires --prom\n")
        return 2
    cfg = ctx.cfg
    host = cfg.sweeper.host or socket.gethostname()
    ledger = ctx.ledger()
    entry = ledger.read(f"sweeper:{host}")
    sys.stdout.write(
        render_metrics_prom(entry, host=host, interval_s=cfg.sweeper.interval_s)
    )
    return 0
```

- [ ] **Step 4: GREEN — wire the subparser in `src/kinoforge/cli/_main.py`**

Locate where `p_reap` and `p_cost` are added; below them add:

```python
    # sweeper
    p_sweeper = sub.add_parser(
        "sweeper", help="Layer W: long-running reap daemon"
    )
    sw_sub = p_sweeper.add_subparsers(dest="sweeper_cmd", metavar="SUBCOMMAND")

    p_sweeper_start = sw_sub.add_parser(
        "start", help="run the sweeper daemon in the foreground"
    )
    p_sweeper_start.add_argument("--config", required=True, metavar="PATH")
    p_sweeper_start.add_argument(
        "--interval-s",
        type=float,
        default=None,
        metavar="N",
        help="override cfg.sweeper.interval_s for this run",
    )

    p_sweeper_stop = sw_sub.add_parser(
        "stop", help="SIGTERM the daemon owning sweeper:<host>"
    )
    p_sweeper_stop.add_argument("--config", required=True, metavar="PATH")

    p_sweeper_status = sw_sub.add_parser("status", help="read sweeper liveness")
    p_sweeper_status.add_argument("--config", required=True, metavar="PATH")
    p_sweeper_status.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )

    p_sweeper_metrics = sw_sub.add_parser(
        "metrics", help="Prometheus textfile-collector target"
    )
    p_sweeper_metrics.add_argument("--config", required=True, metavar="PATH")
    p_sweeper_metrics.add_argument(
        "--prom", action="store_true", required=True,
        help="emit Prom text exposition",
    )
```

And in the dispatcher (the if/elif chain mapping `args.cmd` to handlers), add the sweeper branch:

```python
    elif args.cmd == "sweeper":
        from kinoforge.cli._commands import (
            _cmd_sweeper_metrics,
            _cmd_sweeper_start,
            _cmd_sweeper_status,
            _cmd_sweeper_stop,
        )
        if args.sweeper_cmd == "start":
            return _cmd_sweeper_start(args, ctx)
        if args.sweeper_cmd == "stop":
            return _cmd_sweeper_stop(args, ctx)
        if args.sweeper_cmd == "status":
            return _cmd_sweeper_status(args, ctx)
        if args.sweeper_cmd == "metrics":
            return _cmd_sweeper_metrics(args, ctx)
        p_sweeper.print_help()
        return 2
```

(Match the surrounding style of the existing dispatcher; if dispatch uses a dict, register the four `_cmd_sweeper_*` callables instead.)

- [ ] **Step 5: Run GREEN**

```
pixi run pytest tests/cli/test_cmd_sweeper.py -v
```
Expected: 7 passed.

Run the broader CLI suite to confirm no regression:

```
pixi run pytest tests/cli/ -v
```

- [ ] **Step 6: Stage + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/cli/_commands.py \
    src/kinoforge/cli/_main.py \
    tests/cli/test_cmd_sweeper.py
git add src/kinoforge/cli/_commands.py src/kinoforge/cli/_main.py tests/cli/test_cmd_sweeper.py
git commit -m "$(cat <<'EOF'
feat(b1): kinoforge sweeper start/stop/status/metrics CLI

Foreground supervisor (start blocks under systemd/nohup/docker PID 1).
Materialises sweeper:<host> via Ledger.record + Ledger.touch(pid=...).
SIGTERM drains in-flight act_on_verdict; SIGHUP re-reads cfg via
loop.reload(); SIGUSR1 dumps stats. status renders human + --json
sibling of `kinoforge status`; metrics --prom is the textfile-collector
target. Banner advertises B5a substrate gate + B7 session-claim probe.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task f: Cross-process subprocess tests (start→SIGTERM→stop; SIGHUP reload)

**Goal:** End-to-end coverage that spawning `kinoforge sweeper start` in a real subprocess writes the ledger entry, that SIGTERM drains cleanly, and that SIGHUP swaps `interval_s` mid-flight.

**Files:**
- Create: `tests/cli/test_cmd_sweeper_xprocess.py`

**Acceptance Criteria:**
- [ ] Subprocess `python -m kinoforge sweeper start -c <fake-config>` with LocalProvider records the synthetic ledger entry within 10s; SIGTERM produces exit 0 within an additional 10s; the ledger entry persists for `status` to read post-stop.
- [ ] After stop, `kinoforge sweeper status` reports `last_sweep_age_s` > 0.
- [ ] Start a daemon at `interval_s=5`; modify the YAML to `interval_s=1`; send SIGHUP; observe `heartbeat_thread_tick` deltas in the ledger averaging ≤2s over three subsequent ticks (sample via `ledger.read` polls in the parent).

**Verify:** `pixi run pytest tests/cli/test_cmd_sweeper_xprocess.py -v` → 3 passed.

**Steps:**

- [ ] **Step 1: RED — write `tests/cli/test_cmd_sweeper_xprocess.py`**

```python
"""Layer W: cross-process xprocess tests for `kinoforge sweeper`.

Mirrors tests/core/test_orchestrator_session_claim_xprocess.py shape.
Uses subprocess.Popen against the installed `kinoforge` console script
(via `pixi run python -m kinoforge`).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _yaml(interval_s: float, *, host: str | None = None) -> str:
    host_line = f"  host: {host}\n" if host else ""
    return (
        "compute:\n"
        "  provider: local\n"
        "  image: dummy\n"
        "engine:\n"
        "  kind: fake\n"
        "models: []\n"
        "sweeper:\n"
        f"  interval_s: {interval_s}\n"
        f"{host_line}"
    )


def _wait_for_entry(
    ledger_path: Path, host: str, *, timeout_s: float = 10.0
) -> dict | None:
    """Poll the on-disk ledger.json until sweeper:<host> appears."""
    deadline = time.monotonic() + timeout_s
    key = f"sweeper:{host}"
    while time.monotonic() < deadline:
        if ledger_path.exists():
            try:
                data = json.loads(ledger_path.read_text())
            except json.JSONDecodeError:
                time.sleep(0.1)
                continue
            for e in data.get("entries", []):
                if e.get("id") == key:
                    return e
        time.sleep(0.1)
    return None


def _spawn(cfg_path: Path, state_dir: Path):
    """Spawn `kinoforge sweeper start` against the given cfg and state dir."""
    env = os.environ.copy()
    env["KINOFORGE_STATE_DIR"] = str(state_dir)
    return subprocess.Popen(
        [sys.executable, "-m", "kinoforge", "sweeper", "start", "-c", str(cfg_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.mark.timeout(60)
def test_start_then_sigterm(tmp_path: Path) -> None:
    """Subprocess: start daemon; verify entry; SIGTERM; verify exit 0."""
    host = "xprocess-host-a"
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(_yaml(interval_s=1.0, host=host))
    state = tmp_path / "state"
    state.mkdir()

    proc = _spawn(cfg, state)
    try:
        ledger_path = state / "_lifecycle" / "ledger.json"
        entry = _wait_for_entry(ledger_path, host, timeout_s=10.0)
        assert entry is not None, "sweeper liveness entry never appeared"
        assert entry["provider"] == "_sweeper"
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("daemon did not exit within 10s of SIGTERM")
        assert rc == 0, f"non-zero exit {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.timeout(60)
def test_status_after_clean_stop(tmp_path: Path) -> None:
    """After SIGTERM, `sweeper status` reports last_sweep_age_s > 0."""
    host = "xprocess-host-b"
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(_yaml(interval_s=1.0, host=host))
    state = tmp_path / "state"
    state.mkdir()
    env = os.environ.copy()
    env["KINOFORGE_STATE_DIR"] = str(state)

    proc = _spawn(cfg, state)
    try:
        ledger_path = state / "_lifecycle" / "ledger.json"
        assert _wait_for_entry(ledger_path, host, timeout_s=10.0) is not None
        time.sleep(2.0)  # let a couple of ticks land
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10.0)
        assert rc == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    # Now read status.
    status = subprocess.run(
        [
            sys.executable, "-m", "kinoforge",
            "sweeper", "status", "-c", str(cfg), "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert status.returncode == 0, status.stderr
    body = json.loads(status.stdout)
    assert body["last_sweep_age_s"] is not None
    assert body["last_sweep_age_s"] > 0


@pytest.mark.timeout(60)
def test_sighup_reloads_interval(tmp_path: Path) -> None:
    """Daemon started at 5s; SIGHUP after YAML edit to 1s → next ticks land ~1s apart."""
    host = "xprocess-host-c"
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(_yaml(interval_s=5.0, host=host))
    state = tmp_path / "state"
    state.mkdir()

    proc = _spawn(cfg, state)
    try:
        ledger_path = state / "_lifecycle" / "ledger.json"
        first = _wait_for_entry(ledger_path, host, timeout_s=10.0)
        assert first is not None
        # Edit YAML and SIGHUP.
        cfg.write_text(_yaml(interval_s=1.0, host=host))
        proc.send_signal(signal.SIGHUP)
        # Sample three subsequent ticks via ledger reads.
        ticks: list[float] = []
        deadline = time.monotonic() + 15.0
        last_tick = float(first.get("heartbeat_thread_tick", 0.0))
        while time.monotonic() < deadline and len(ticks) < 3:
            data = json.loads(ledger_path.read_text())
            for e in data.get("entries", []):
                if e.get("id") == f"sweeper:{host}":
                    t = float(e.get("heartbeat_thread_tick", 0.0))
                    if t > last_tick:
                        ticks.append(t)
                        last_tick = t
                    break
            time.sleep(0.2)
        assert len(ticks) >= 3, f"only saw {len(ticks)} post-SIGHUP ticks"
        deltas = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
        avg = sum(deltas) / len(deltas)
        assert avg <= 2.0, f"avg gap {avg:.2f}s — SIGHUP did not shorten interval"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
```

- [ ] **Step 2: Run RED**

```
pixi run pytest tests/cli/test_cmd_sweeper_xprocess.py -v
```
Expected: at least one failure / timeout (depending on subprocess behaviour pre-fix). Capture the actual stderr; if `kinoforge sweeper` subcommand wiring works correctly from Task e, this may already pass — record the result.

- [ ] **Step 3: GREEN — fix any issues uncovered**

Common issues to check:
- `KINOFORGE_STATE_DIR` may need wiring in `cli/_main.py` `SessionContext` construction. Inspect with `rg -n "state_dir" src/kinoforge/cli/_main.py` — if the env var is not honoured today, do NOT add it as a side-effect; instead, replace the `_spawn` helper to use `--state-dir` if the CLI supports it, or to chdir into `tmp_path` and accept the default `.kinoforge/` layout. Adjust the test, not the production code, when the production code already has an established state-dir pattern.

If subprocess can't find `kinoforge` module, run via `pixi run python -m kinoforge` instead of bare `python -m kinoforge`. Update the test's `subprocess.Popen` `executable` argument to the pixi-env python (`shutil.which("python")` inside the pixi env, or wire via pytest fixture).

- [ ] **Step 4: Verify GREEN**

```
pixi run pytest tests/cli/test_cmd_sweeper_xprocess.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Stage + commit**

```bash
pixi run pre-commit run --files tests/cli/test_cmd_sweeper_xprocess.py
git add tests/cli/test_cmd_sweeper_xprocess.py
git commit -m "$(cat <<'EOF'
test(b1): xprocess start→SIGTERM→stop + SIGHUP reload

Spawns `python -m kinoforge sweeper start` against LocalProvider on
tmp_path; verifies sweeper:<host> ledger entry materialises, SIGTERM
drains within 10s, status shows last_sweep_age_s post-stop, and
SIGHUP after YAML edit shortens the cadence to ~1s within 15s.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task g: Examples + README + invariant scan + closeout

**Goal:** Operator-facing surface (example YAML + README section) + the `sweeper_metrics.py` purity invariant + closeout strikes in PROGRESS.md and warm-reuse-tasks.txt. Final commit references the merge sha (do this after the prior six tasks land on main).

**Files:**
- Create: `examples/configs/sweeper.yaml`
- Modify: `README.md`
- Modify: `tests/test_core_invariant.py` (+1 test)
- Modify: `PROGRESS.md` (B1 closeout strike with merge sha)
- Modify: `warm-reuse-tasks.txt` (replace lines 523–555 with closeout)

**Acceptance Criteria:**
- [ ] `examples/configs/sweeper.yaml` parses via `load_config`; existing `tests/test_examples.py` automatically picks it up via its glob fixture.
- [ ] README has a new "Sweeper daemon" section documenting the four subcommands, the `sweeper:` YAML block, the textfile-collector cron pattern, and the systemd `Type=simple` posture.
- [ ] `test_core_sweeper_metrics_is_pure` asserts `core/sweeper_metrics.py` imports no `threading` / `subprocess` / `time` / `pathlib` / `kinoforge.core.lifecycle` / `kinoforge.providers` / `kinoforge.engines` / `kinoforge.sources`.
- [ ] `PROGRESS.md §B B1 entry` strikes with `~~B1. Layer W — ...~~ — CLOSED by commit <sha>`.
- [ ] `warm-reuse-tasks.txt:523-555` block replaced with a 1-2 line closeout pointing at the spec + plan paths + closing commit sha.

**Verify:** `pixi run pytest tests/test_examples.py tests/test_core_invariant.py -v` → all green; spot-check `git log --oneline -10`.

**Steps:**

- [ ] **Step 1: Write `examples/configs/sweeper.yaml`**

```yaml
# kinoforge sweeper daemon example config.
#
# Run with:
#   kinoforge sweeper start -c examples/configs/sweeper.yaml
#
# Typical systemd posture (Type=simple + Restart=on-failure):
#   ExecStart=/usr/local/bin/kinoforge sweeper start -c /etc/kinoforge.yaml
#   Restart=on-failure
#
# Textfile-collector cron for Prometheus:
#   */30 * * * * kinoforge sweeper metrics --prom -c /etc/kinoforge.yaml \
#                  > /var/lib/node_exporter/textfile/kinoforge_sweeper.prom

compute:
  provider: runpod
  image: runpod/base:ubuntu22.04
  lifecycle:
    idle_timeout_s: 7200      # 2h idle → IDLE_REAP
    max_lifetime_s: 28800     # 8h hard ceiling → OVERAGE_REAP
    heartbeat_interval_s: 30  # Layer U heartbeat cadence; substrate gate
    grace_after_session_s: 300  # 5min post-session warm-reuse window

engine:
  kind: fake                  # replace with your real engine kind

models: []

sweeper:
  interval_s: 60              # cadence; default 60s — gentle on RunPod GraphQL
  include_orphans: false      # opt-in: extend act_verdicts with ORPHAN_REAP
  force_forget: false         # opt-in: extend act_verdicts with UNROUTABLE
  host: null                  # null → socket.gethostname() at CLI level
```

- [ ] **Step 2: Update `README.md`**

Locate the operator section (where `kinoforge cost` and `kinoforge reap` are documented). Add a new subsection:

```markdown
### Sweeper daemon (Layer W)

The sweeper is a long-running foreground daemon that calls
`kinoforge reap`'s underlying `sweep()` on a configurable cadence
(default 60s). It closes the idle-pod cost-leak window between
manual operator sweeps.

Subcommands:

```
kinoforge sweeper start    # foreground; blocks until SIGTERM
kinoforge sweeper stop     # SIGTERM the daemon owning sweeper:<host>
kinoforge sweeper status   # human or --json output
kinoforge sweeper metrics  # --prom textfile-collector target
```

YAML block (additive to existing config; defaults are safe):

```yaml
sweeper:
  interval_s: 60
  include_orphans: false   # extend default policy with ORPHAN_REAP
  force_forget: false      # extend default policy with UNROUTABLE
  host: null               # null → socket.gethostname()
```

Operator postures:

- **systemd:** `Type=simple` + `Restart=on-failure`. `ExecStart=/usr/local/bin/kinoforge sweeper start -c /etc/kinoforge.yaml`.
- **docker:** Run as PID 1; the daemon handles SIGTERM cleanly.
- **textfile-collector cron:**
  ```
  */30 * * * * kinoforge sweeper metrics --prom -c /etc/kinoforge.yaml \
                 > /var/lib/node_exporter/textfile/kinoforge_sweeper.prom
  ```

Signals:

- `SIGTERM` → drain the in-flight sweep then exit 0.
- `SIGHUP` → re-read the config file and swap policy / thresholds /
  interval without restarting the thread.
- `SIGUSR1` → log cumulative stats to stdout.

The daemon's own liveness lives in a reserved synthetic ledger entry
keyed `sweeper:<host>`. `sweep()` filters this prefix so the daemon
cannot reap itself. Use `kinoforge sweeper status --json` to read the
entry programmatically.
```

(Match surrounding markdown style; preserve TOC anchor if README has one.)

- [ ] **Step 3: Add the purity invariant — append to `tests/test_core_invariant.py`**

```python
# ---------------------------------------------------------------------------
# AC 19: core/sweeper_metrics.py purity contract (Layer W / B1)
# ---------------------------------------------------------------------------

_SWEEPER_METRICS_FORBIDDEN_IMPORTS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(import|from)\s+threading\b"),
    re.compile(r"^\s*(import|from)\s+subprocess\b"),
    re.compile(r"^\s*(import|from)\s+time\b"),
    re.compile(r"^\s*(import|from)\s+pathlib\b"),
    re.compile(r"^\s*(import|from)\s+urllib\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.core\.lifecycle\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.providers\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.sources\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.engines\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.stores\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.cli\b"),
]


def test_core_sweeper_metrics_module_is_pure() -> None:
    """Layer W: core/sweeper_metrics.py is pure — no I/O, no ledger import.

    The three renderers (human / JSON / Prom) take their input as a dict
    argument; any I/O here would couple the dashboard's output format
    to a specific storage backend. Architecturally enforced so a future
    contributor cannot reach into ledger.json directly.
    """
    path = SRC_ROOT / "core" / "sweeper_metrics.py"
    violations: list[str] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        for pattern in _SWEEPER_METRICS_FORBIDDEN_IMPORTS:
            if pattern.match(line):
                violations.append(f"{path}:{lineno}: {line.strip()}")
                break
    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            f"core/sweeper_metrics.py must be pure — forbidden import(s) found:\n  {detail}"
        )
```

- [ ] **Step 4: Run the new invariant + examples tests**

```
pixi run pytest tests/test_examples.py tests/test_core_invariant.py -v
```
Expected: green.

- [ ] **Step 5: Commit examples + README + invariant**

```bash
pixi run pre-commit run --files \
    examples/configs/sweeper.yaml \
    README.md \
    tests/test_core_invariant.py
git add examples/configs/sweeper.yaml README.md tests/test_core_invariant.py
git commit -m "$(cat <<'EOF'
docs(b1): sweeper example config + README section + purity invariant

examples/configs/sweeper.yaml documents the four subcommands, the
sweeper YAML block, the systemd posture, and the textfile-collector
cron. README adds Sweeper daemon operator section.
tests/test_core_invariant adds test_core_sweeper_metrics_module_is_pure
to lock down core/sweeper_metrics.py as I/O-free.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: After all tasks land on main — closeout PROGRESS.md + warm-reuse-tasks.txt**

Capture the closing merge sha:

```bash
git log --oneline -5
```

In `PROGRESS.md`, find the B1 entry at §B (around line 147):

```markdown
- **B1. Layer W — `kinoforge sweeper` daemon.** `while True: sweep(...); sleep(interval)` consumer of the Layer V substrate. ...
```

Replace with:

```markdown
- ~~**B1. Layer W — `kinoforge sweeper` daemon.**~~ — CLOSED by commit `<SHA>`. Spec at `docs/superpowers/specs/2026-06-13-b1-sweeper-daemon-design.md`; plan at `docs/superpowers/plans/2026-06-13-b1-sweeper-daemon.md`. Foreground supervisor (`kinoforge sweeper start | stop | status | metrics`); SweeperLoop mirrors HeartbeatLoop (eager first tick, bounded shutdown, broad try/except per iter); synthetic `sweeper:<host>` ledger entry as daemon-liveness signal; one-line filter in `sweep()` prevents self-reap; `kinoforge_sweeper_*` Prom gauges as siblings of B2. Banner advertises B5a HEARTBEAT_SUBSTRATE_MISSING contract + B7 cooperative session-claim probe. Live spend: $0.
```

In `warm-reuse-tasks.txt`, replace lines 523–555 (the B1 block) with:

```text
- ~~**B1. Layer W — `kinoforge sweeper` daemon.**~~ — CLOSED by commit `<SHA>`.
  Spec: docs/superpowers/specs/2026-06-13-b1-sweeper-daemon-design.md.
  Plan: docs/superpowers/plans/2026-06-13-b1-sweeper-daemon.md.
  Surface: `kinoforge sweeper start | stop | status [--json] | metrics --prom`
  + `sweeper:` YAML block + synthetic `sweeper:<host>` ledger liveness entry.
  Daemon loop mirrors HeartbeatLoop. Live spend: $0.
```

- [ ] **Step 7: Commit closeout**

```bash
git add PROGRESS.md warm-reuse-tasks.txt
git commit -m "$(cat <<'EOF'
docs(b1): closeout — strike PROGRESS + warm-reuse-tasks B1 with merge sha

Layer W shipped. Spec + plan paths pinned; surface area summary
preserved for future grep.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review (notes during execution)

When closing the plan, re-verify against the spec:
- §3 D1–D7 → respectively Task e (foreground supervisor), Task e (Ledger.record init), Task e (signal triplet), Task c (interval_s default 60s), Task d (Prom siblings), automatic via existing reaper/<id> TTL (no code), Task d + Task e (status vs metrics split).
- §4 module map → fully covered by Tasks a + b + c + d + e + g.
- §6 test plan → §6.1 covered by Task a; §6.2 by Task d; §6.3 by Task b; §6.4 by Task e; §6.5 by Task f; §6.6 by Task c; §6.7 by Task g.
- §7 AC1–AC20 → covered by Task a (AC1–9), b (AC9 again from a different angle), c (AC10–12), d (AC13–15 partial), e (AC13–17), f (AC17–18), g (AC19); AC20 (full suite green + pre-commit) runs at every commit.

No spec sections without a task. No tasks reference symbols not defined in earlier tasks.
