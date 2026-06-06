"""Layer U T2: HeartbeatLoop threaded poll + crash-safe try/except + sentinel.

Tests are red-first against the missing `kinoforge.core.heartbeat_loop` module.

Design constraints (Layer U spec §3.3 / §3.4):
- Inner try/except per tick: a single bad tick cannot kill the loop.
- Sentinel `heartbeat_thread_tick` advances on every successful tick.
- `daemon=True` thread + bounded `join(timeout=...)` on stop: a wedged
  thread cannot block process exit.

AC mapping (Layer U):
- AC6: tick semantics + sentinel monotonic.
- AC7: provider / ledger exceptions caught + logged.
- AC8: stop() bounded join from mid-sleep AND on wedged thread.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.heartbeat_loop import HeartbeatLoop
from kinoforge.core.lifecycle import Ledger
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# ---------------------------------------------------------------------------
# Test helpers — providers, ledgers, spies
# ---------------------------------------------------------------------------


def _seed_local_provider_and_ledger(
    tmp_path: Path,
    *,
    instance_id: str = "i-loop",
    run_id: str = "_hbloop",
) -> tuple[LocalProvider, Ledger, FakeClock]:
    """Build a LocalProvider with one ready instance and a fresh Ledger."""
    clock = FakeClock(start=100.0)
    provider = LocalProvider(clock=clock)
    # LocalProvider uses an internal id generator; we bypass it by injecting.
    from kinoforge.core.interfaces import Instance

    inst = Instance(
        id=instance_id,
        provider="local",
        status="ready",
        created_at=clock.now(),
        cost_rate_usd_per_hr=0.0,
    )
    provider._instances[instance_id] = inst  # noqa: SLF001 — test seam
    ledger = Ledger(store=LocalArtifactStore(tmp_path), run_id=run_id)
    ledger.record(inst)
    return provider, ledger, clock


class _CountingProvider:
    """Minimal ComputeProvider stub: counts heartbeat() calls.

    Implements only the two methods HeartbeatLoop touches (heartbeat,
    last_heartbeat) — the loop never reaches into create_instance et al.,
    so a duck-typed stand-in is safe and keeps the test surface small.
    """

    def __init__(
        self,
        *,
        clock: FakeClock,
        raise_first_n: int = 0,
        block_event: threading.Event | None = None,
    ) -> None:
        self._clock = clock
        self._raise_first_n = raise_first_n
        self._block_event = block_event
        self.calls: list[str] = []
        self.call_event = threading.Event()
        self._target = 0

    def expect(self, n: int) -> None:
        """Reset the wait-event so the next ``n`` successful calls fire it."""
        self._target = n
        self.call_event.clear()

    def heartbeat(self, instance_id: str) -> None:
        """Record the call; optionally raise; optionally block on an event."""
        if self._block_event is not None:
            self._block_event.wait(timeout=30.0)
        self.calls.append(instance_id)
        if len(self.calls) <= self._raise_first_n:
            raise RuntimeError(f"injected fault on call {len(self.calls)}")
        if self._target > 0 and len(self.calls) >= self._target:
            self.call_event.set()

    def last_heartbeat(self, instance_id: str) -> float | None:
        """Return the clock time of the most recent successful call."""
        return self._clock.now()


class _RaisingLedger:
    """Ledger stub whose touch() raises N times then succeeds.

    Structurally satisfies kinoforge.core.heartbeat_loop._TouchableLedger.
    """

    def __init__(self, *, raise_first_n: int = 0) -> None:
        self._raise_first_n = raise_first_n
        self.touch_calls: list[tuple[str, dict[str, float | int | str | None]]] = []
        self.call_event = threading.Event()
        self._target = 0

    def expect(self, n: int) -> None:
        """Reset the wait-event so the next ``n`` successful touch calls fire it."""
        self._target = n
        self.call_event.clear()

    def touch(
        self,
        instance_id: str,
        *,
        last_heartbeat: float | None = None,
        **extra: float | int | str | None,
    ) -> bool:
        """Record + optionally raise; return True so loop treats as a write."""
        payload: dict[str, float | int | str | None] = {
            "last_heartbeat": last_heartbeat
        }
        payload.update(extra)
        self.touch_calls.append((instance_id, payload))
        if len(self.touch_calls) <= self._raise_first_n:
            raise RuntimeError(f"injected ledger fault on call {len(self.touch_calls)}")
        if self._target > 0 and len(self.touch_calls) >= self._target:
            self.call_event.set()
        return True


# ---------------------------------------------------------------------------
# AC6 — tick semantics
# ---------------------------------------------------------------------------


def test_loop_ticks_provider_heartbeat_and_ledger_touch_each_interval(
    tmp_path: Path,
) -> None:
    """Each interval, the loop calls provider.heartbeat then ledger.touch once.

    Bug this would catch: a future refactor that swaps the call order
    or only calls one of the two seams. We assert both spies advance in
    lockstep over 3 ticks.
    """
    provider, ledger, clock = _seed_local_provider_and_ledger(tmp_path)
    spy = _CountingProvider(clock=clock)
    spy.expect(3)
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=spy,
        instance_id="i-loop",
        interval_s=0.01,
        clock=clock,
    )
    try:
        loop.start()
        assert spy.call_event.wait(timeout=5.0), (
            f"loop only ticked {len(spy.calls)} times in 5s"
        )
    finally:
        loop.stop()

    assert len(spy.calls) >= 3
    # ledger.touch was called at least 3 times — surfaced by the entry now
    # having last_heartbeat + heartbeat_thread_tick populated.
    [entry] = ledger.entries()
    assert "last_heartbeat" in entry
    assert "heartbeat_thread_tick" in entry


def test_loop_eager_first_tick_writes_before_any_sleep(tmp_path: Path) -> None:
    """The first tick fires before _stop.wait(interval_s) runs.

    Without this, short-lived deploy_session contexts (sub-interval
    sessions) would never write a heartbeat — operator-visible "did my
    session start?" would be a no-op. We use a long interval (5.0s) and
    assert the first tick is observed in well under that window.
    """
    provider, ledger, clock = _seed_local_provider_and_ledger(tmp_path)
    spy = _CountingProvider(clock=clock)
    spy.expect(1)
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=spy,
        instance_id="i-loop",
        interval_s=5.0,
        clock=clock,
    )
    try:
        t0 = time.monotonic()
        loop.start()
        assert spy.call_event.wait(timeout=1.0), (
            "first tick did not fire within 1s under a 5.0s interval — sleep is not eager-first"
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"first tick took {elapsed:.2f}s — not eager"
    finally:
        loop.stop()


# ---------------------------------------------------------------------------
# AC7 — exception isolation
# ---------------------------------------------------------------------------


def test_loop_provider_heartbeat_raises_loop_continues_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Provider exception is caught + logged at ERROR; the loop keeps ticking.

    Bug this would catch: a future refactor that lifts the try/except
    outside the loop body. After the first raise, the thread would die
    and the heartbeat would silently stop — exactly the failure mode
    we are designing around.
    """
    provider, ledger, clock = _seed_local_provider_and_ledger(tmp_path)
    spy = _CountingProvider(clock=clock, raise_first_n=2)
    spy.expect(4)  # 2 failures + 2 successes
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=spy,
        instance_id="i-loop",
        interval_s=0.01,
        clock=clock,
    )
    caplog.set_level(logging.ERROR, logger="kinoforge.core.heartbeat_loop")
    try:
        loop.start()
        assert spy.call_event.wait(timeout=5.0), (
            f"loop died after exception — only {len(spy.calls)} calls observed"
        )
    finally:
        loop.stop()

    assert len(spy.calls) >= 4  # 2 raised + 2+ successful
    # caplog must have at least one ERROR from the swallowed exceptions
    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "heartbeat tick failed" in r.getMessage()
    ]
    assert len(error_records) >= 2, (
        f"expected >=2 ERROR records, got {len(error_records)}: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


def test_loop_ledger_touch_raises_loop_continues_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ledger exception is caught + logged at ERROR; the loop keeps ticking.

    Symmetric to the provider-exception test. Both seams must be
    independently resilient.
    """
    _, _, clock = _seed_local_provider_and_ledger(tmp_path)
    spy = _CountingProvider(clock=clock)
    raising_ledger = _RaisingLedger(raise_first_n=2)
    raising_ledger.expect(4)
    spy.expect(4)
    loop = HeartbeatLoop(
        ledger=raising_ledger,
        provider=spy,
        instance_id="i-loop",
        interval_s=0.01,
        clock=clock,
    )
    caplog.set_level(logging.ERROR, logger="kinoforge.core.heartbeat_loop")
    try:
        loop.start()
        assert raising_ledger.call_event.wait(timeout=5.0), (
            f"loop died after ledger exception — only {len(raising_ledger.touch_calls)} touches"
        )
    finally:
        loop.stop()

    assert len(raising_ledger.touch_calls) >= 4
    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "heartbeat tick failed" in r.getMessage()
    ]
    assert len(error_records) >= 2, (
        f"expected >=2 ERROR records, got {len(error_records)}"
    )


# ---------------------------------------------------------------------------
# Sentinel monotonic
# ---------------------------------------------------------------------------


def test_loop_sentinel_thread_tick_advances_monotonically(tmp_path: Path) -> None:
    """heartbeat_thread_tick written to the ledger advances over successive ticks.

    The sentinel is the load-bearing crash-safety signal (Layer U §3.4).
    A future reaper consults it to distinguish fresh-but-quiet from
    silent-crashed. If the sentinel ever regressed (e.g., writes a
    fixed value), the reaper would misclassify and could destroy a
    healthy pod. This test pins down the monotonic contract.
    """
    provider, ledger, _ = _seed_local_provider_and_ledger(tmp_path)
    clock = FakeClock(start=200.0)
    spy = _CountingProvider(clock=clock)
    spy.expect(3)
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=spy,
        instance_id="i-loop",
        interval_s=0.01,
        clock=clock,
    )

    observed_ticks: list[float] = []

    class _RecordingLedger(Ledger):
        def touch(
            self,
            instance_id: str,
            *,
            last_heartbeat: float | None = None,
            **extra: float | int | str | None,
        ) -> bool:
            tick = extra.get("heartbeat_thread_tick")
            if isinstance(tick, (int, float)):
                observed_ticks.append(float(tick))
                # advance clock between ticks so the next sentinel differs
                clock.advance(1.0)
            return super().touch(instance_id, last_heartbeat=last_heartbeat, **extra)

    recording = _RecordingLedger(
        store=LocalArtifactStore(tmp_path / "rec"), run_id="_rec"
    )
    recording.record(provider._instances["i-loop"])  # noqa: SLF001
    loop = HeartbeatLoop(
        ledger=recording,
        provider=spy,
        instance_id="i-loop",
        interval_s=0.01,
        clock=clock,
    )
    try:
        loop.start()
        assert spy.call_event.wait(timeout=5.0)
    finally:
        loop.stop()

    assert len(observed_ticks) >= 3
    assert observed_ticks == sorted(observed_ticks), (
        f"sentinel not monotonic: {observed_ticks}"
    )
    assert observed_ticks[0] < observed_ticks[-1], "sentinel did not advance"


# ---------------------------------------------------------------------------
# AC8 — bounded stop()
# ---------------------------------------------------------------------------


def test_stop_joins_within_timeout_when_thread_mid_sleep(tmp_path: Path) -> None:
    """stop() during the inter-tick sleep returns within join_timeout_s.

    Uses a 10s interval so the thread is guaranteed mid-sleep when we
    call stop(). _stop.wait() must wake on event-set and let the loop
    exit cleanly. If a future refactor used time.sleep() instead of
    _stop.wait(), this test would hang the full 10s.
    """
    provider, ledger, clock = _seed_local_provider_and_ledger(tmp_path)
    spy = _CountingProvider(clock=clock)
    spy.expect(1)
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=spy,
        instance_id="i-loop",
        interval_s=10.0,
        clock=clock,
        join_timeout_s=5.0,
    )
    loop.start()
    assert spy.call_event.wait(timeout=2.0), "first tick failed"

    t0 = time.monotonic()
    loop.stop()
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, (
        f"stop() took {elapsed:.2f}s — _stop.wait() not honoring event"
    )
    assert not loop._thread.is_alive(), "thread still alive after stop"  # noqa: SLF001


def test_stop_does_not_hang_when_thread_wedged(tmp_path: Path) -> None:
    """stop() on a wedged thread returns within join_timeout_s (daemon).

    Provider.heartbeat blocks on an event; loop is stuck inside the
    blocking call when stop() arrives. Because the thread is daemon
    and join uses a timeout, stop() returns in bounded time even
    though the thread itself never exits. This is the Layer 3 defense
    documented in Layer U §3.4.
    """
    _, ledger, clock = _seed_local_provider_and_ledger(tmp_path)
    block = threading.Event()
    spy = _CountingProvider(clock=clock, block_event=block)
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=spy,
        instance_id="i-loop",
        interval_s=0.01,
        clock=clock,
        join_timeout_s=1.0,
    )
    loop.start()
    # Give the thread a moment to enter the blocking heartbeat call.
    time.sleep(0.1)

    t0 = time.monotonic()
    loop.stop()
    elapsed = time.monotonic() - t0
    # stop() must return within join_timeout_s + generous slack.
    assert elapsed < 2.5, (
        f"stop() took {elapsed:.2f}s on wedged thread — join_timeout_s ignored"
    )
    # Release the wedged thread so the test process can shut down cleanly.
    block.set()


# ---------------------------------------------------------------------------
# Two-loop isolation
# ---------------------------------------------------------------------------


def test_two_concurrent_loops_on_different_instances_do_not_collide(
    tmp_path: Path,
) -> None:
    """Two loops on different ids tick independently; their touches land on separate entries.

    Bug this would catch: a HeartbeatLoop that captured the instance_id
    via mutable shared state, causing both loops to touch the same
    entry. Layer U's lock contract already serializes the writes — this
    test pins down that the SEMANTIC isolation is preserved (each loop
    writes its own id).
    """
    clock = FakeClock(start=300.0)
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_two_loops")
    from kinoforge.core.interfaces import Instance

    for inst_id in ("i-a", "i-b"):
        ledger.record(
            Instance(
                id=inst_id,
                provider="local",
                status="ready",
                created_at=clock.now(),
                cost_rate_usd_per_hr=0.0,
            )
        )

    spy_a = _CountingProvider(clock=clock)
    spy_b = _CountingProvider(clock=clock)
    spy_a.expect(2)
    spy_b.expect(2)

    loop_a = HeartbeatLoop(
        ledger=ledger,
        provider=spy_a,
        instance_id="i-a",
        interval_s=0.01,
        clock=clock,
    )
    loop_b = HeartbeatLoop(
        ledger=ledger,
        provider=spy_b,
        instance_id="i-b",
        interval_s=0.01,
        clock=clock,
    )
    try:
        loop_a.start()
        loop_b.start()
        assert spy_a.call_event.wait(timeout=5.0)
        assert spy_b.call_event.wait(timeout=5.0)
    finally:
        loop_a.stop()
        loop_b.stop()

    entries = {e["id"]: e for e in ledger.entries()}
    assert set(entries.keys()) == {"i-a", "i-b"}
    assert entries["i-a"].get("last_heartbeat") is not None
    assert entries["i-b"].get("last_heartbeat") is not None
    # Each spy only saw its own id.
    assert set(spy_a.calls) == {"i-a"}
    assert set(spy_b.calls) == {"i-b"}
