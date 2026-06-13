"""B7 T2: hold_until_first_tick context manager unit tests.

Pure offline coverage of the lock+poll+release contract. Cross-process
integration lives at tests/core/test_orchestrator_session_claim_xprocess.py.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.session_claim import FirstTickTimeout, hold_until_first_tick
from kinoforge.stores.local import LocalArtifactStore


def _make_instance(instance_id: str = "i-claim") -> Instance:
    return Instance(
        id=instance_id,
        provider="local",
        status="ready",
        created_at=0.0,
        cost_rate_usd_per_hr=0.0,
        tags={},
    )


def _record_and_set_tick(ledger: Ledger, instance_id: str, tick: float) -> None:
    """Helper: record instance + touch with the given heartbeat_thread_tick."""
    ledger.record(_make_instance(instance_id))
    ledger.touch(instance_id, heartbeat_thread_tick=tick)


class _CountingSleep:
    """Replace time.sleep in the helper to count poll iterations."""

    def __init__(self, real_sleep: float = 0.0) -> None:
        self.calls: list[float] = []
        self._real = real_sleep

    def __call__(self, s: float) -> None:
        self.calls.append(s)
        if self._real > 0:
            time.sleep(self._real)


def test_acquires_yields_and_releases_on_first_tick(tmp_path: Path) -> None:
    """Happy path: acquire → yield → poll → release when tick >= start."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-happy"))

    with hold_until_first_tick(
        store=store,
        instance_id="i-happy",
        ledger=ledger,
        ttl_s=60.0,
        timeout_s=60.0,
        poll_interval_s=0.01,
        clock=clock,
    ):
        ledger.touch("i-happy", heartbeat_thread_tick=100.5)

    assert store.acquire_lock("provision:i-happy", ttl_s=1.0)


def test_first_tick_timeout_raises_when_no_tick(tmp_path: Path) -> None:
    """Polling exhausts timeout_s with tick never written -> FirstTickTimeout."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-timeout"))

    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        clock.advance(s)

    with pytest.raises(FirstTickTimeout):
        with hold_until_first_tick(
            store=store,
            instance_id="i-timeout",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=0.5,
            poll_interval_s=0.1,
            clock=clock,
            sleep=fake_sleep,
        ):
            pass

    assert len(sleeps) >= 1


def test_first_tick_timeout_when_ledger_read_none(tmp_path: Path) -> None:
    """Helper given an instance_id that was never recorded -> timeout."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)

    def fake_sleep(s: float) -> None:
        clock.advance(s)

    with pytest.raises(FirstTickTimeout):
        with hold_until_first_tick(
            store=store,
            instance_id="i-never-recorded",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=0.5,
            poll_interval_s=0.1,
            clock=clock,
            sleep=fake_sleep,
        ):
            pass


def test_yielded_block_exception_propagates_and_releases_lock(tmp_path: Path) -> None:
    """Caller raises -> exception re-raised; polling skipped; lock released."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-raise"))

    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)

    class _CallerError(RuntimeError):
        pass

    with pytest.raises(_CallerError):
        with hold_until_first_tick(
            store=store,
            instance_id="i-raise",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=60.0,
            poll_interval_s=0.1,
            clock=clock,
            sleep=fake_sleep,
        ):
            raise _CallerError("boom")

    assert sleeps == []
    assert store.acquire_lock("provision:i-raise", ttl_s=1.0)


def test_blocking_acquire_serializes_concurrent_calls(tmp_path: Path) -> None:
    """Second concurrent hold_until_first_tick blocks until first releases."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-serial"))

    sequence: list[str] = []
    first_started = threading.Event()
    first_release = threading.Event()

    def first() -> None:
        with hold_until_first_tick(
            store=store,
            instance_id="i-serial",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=60.0,
            poll_interval_s=0.01,
            clock=clock,
        ):
            sequence.append("first-entered")
            first_started.set()
            first_release.wait(timeout=5.0)
            ledger.touch("i-serial", heartbeat_thread_tick=100.5)
        sequence.append("first-released")

    def second() -> None:
        first_started.wait(timeout=5.0)
        ledger.touch("i-serial", heartbeat_thread_tick=200.5)
        clock_local = FakeClock(start=200.0)
        with hold_until_first_tick(
            store=store,
            instance_id="i-serial",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=60.0,
            poll_interval_s=0.01,
            clock=clock_local,
        ):
            sequence.append("second-entered")
        sequence.append("second-released")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    first_started.wait(timeout=5.0)
    assert sequence == ["first-entered"]
    first_release.set()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)
    assert sequence == [
        "first-entered",
        "first-released",
        "second-entered",
        "second-released",
    ]


def test_clock_injection_used_for_start_time(tmp_path: Path) -> None:
    """start = clock.now() — not time.time()."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=12345.0)
    ledger.record(_make_instance("i-clock"))

    with hold_until_first_tick(
        store=store,
        instance_id="i-clock",
        ledger=ledger,
        ttl_s=60.0,
        timeout_s=60.0,
        poll_interval_s=0.01,
        clock=clock,
    ):
        ledger.touch("i-clock", heartbeat_thread_tick=12345.0)


def test_poll_uses_injected_interval(tmp_path: Path) -> None:
    """poll_interval_s drives sleep cadence between polls."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-poll"))

    sleeps: list[float] = []
    tick_after_n = 3

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == tick_after_n:
            ledger.touch("i-poll", heartbeat_thread_tick=100.5)

    with hold_until_first_tick(
        store=store,
        instance_id="i-poll",
        ledger=ledger,
        ttl_s=60.0,
        timeout_s=60.0,
        poll_interval_s=0.07,
        clock=clock,
        sleep=fake_sleep,
    ):
        pass

    assert all(s == 0.07 for s in sleeps), sleeps
    assert len(sleeps) >= tick_after_n
