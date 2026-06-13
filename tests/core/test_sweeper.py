"""Layer W: SweeperLoop substrate tests.

Mirrors the offline-only style of tests/core/test_heartbeat_loop.py.
FakeClock + spy ledger + manual SweepReport fabrication; no real
ArtifactStore, no real provider.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest

from kinoforge.core.clock import Clock, FakeClock
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import Policy, Verdict
from kinoforge.core.reaper_actor import ActionResult, SweepReport
from kinoforge.core.sweeper import (
    SweeperLoop,
    _SweeperStats,
)
from kinoforge.stores.base import ArtifactStore

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
    sweep_fn: Callable[..., SweepReport],
    ledger: _SpyLedger | None = None,
    clock: Clock | None = None,
    interval_s: float = 0.05,
    policy: Policy | None = None,
    host: str = "test-host",
    stats: _SweeperStats | None = None,
    join_timeout_s: float = 1.0,
) -> tuple[SweeperLoop, _SpyLedger, _SweeperStats]:
    led = ledger or _SpyLedger()
    st = stats or _SweeperStats()
    loop = SweeperLoop(
        store=cast(ArtifactStore, object()),  # _sweep_fn injection bypasses store
        ledger=cast(Ledger, led),
        registry_get_provider=lambda name: lambda: None,  # noqa: ARG005
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
        _sweep_fn=sweep_fn,
    )
    return loop, led, st


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_interval_s_must_be_positive() -> None:
    """Bug: a zero/negative interval would either spin-loop or never tick."""
    with pytest.raises(ValueError, match="interval_s must be > 0"):
        SweeperLoop(
            store=cast(ArtifactStore, object()),
            ledger=cast(Ledger, _SpyLedger()),
            registry_get_provider=lambda name: lambda: None,  # noqa: ARG005
            thresholds={},
            interval_s=0,
            host="h",
            policy=Policy(act_verdicts=frozenset()),
        )
    with pytest.raises(ValueError, match="interval_s must be > 0"):
        SweeperLoop(
            store=cast(ArtifactStore, object()),
            ledger=cast(Ledger, _SpyLedger()),
            registry_get_provider=lambda name: lambda: None,  # noqa: ARG005
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


def test_stats_fold_counts_deferred_session_claim(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
        "i-9" in rec.message and "12345" in rec.message for rec in caplog.records
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
    loop, _, _ = _spawn_loop(sweep_fn=sweep_fn, interval_s=0.05, policy=initial_policy)
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

    loop, _, _ = _spawn_loop(sweep_fn=sweep_fn, interval_s=10.0, join_timeout_s=0.5)
    loop.start()
    time.sleep(0.1)
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
