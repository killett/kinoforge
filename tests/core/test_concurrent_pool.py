"""Tests for ConcurrentPool — Layer G core dispatch (no map; map covered in Task 3)."""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from kinoforge.core.interfaces import GenerationJob
from kinoforge.core.pool import ConcurrentPool

from .conftest import BlockingFakeBackend


def _job(prompt: str = "test") -> GenerationJob:
    """Build a minimal GenerationJob for tests that don't inspect content."""
    from kinoforge.core.interfaces import Segment

    return GenerationJob(
        spec={},
        params={},
        segments=[Segment(prompt=prompt)],
    )


# --- AC 1: submit returns a Future that resolves to the Artifact ----------


def test_submit_returns_future_that_resolves_after_release():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=1)
        fut = pool.submit(_job())
        assert isinstance(fut, concurrent.futures.Future)
        # Must NOT be resolved until release.
        assert not fut.done()
        backend.release("blk-1")
        art = fut.result(timeout=2.0)
        assert art.meta["jid"] == "blk-1"


# --- AC 2: empty pool submit raises ---------------------------------------


def test_empty_pool_submit_raises_runtimeerror():
    pool = ConcurrentPool()
    try:
        with pytest.raises(
            RuntimeError, match="ConcurrentPool has no registered backend"
        ):
            pool.submit(_job())
    finally:
        pool.close()


# --- AC 3: closed pool submit raises --------------------------------------


def test_closed_pool_submit_raises_runtimeerror():
    backend = BlockingFakeBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)
    pool.close()
    with pytest.raises(RuntimeError, match="pool closed"):
        pool.submit(_job())


# --- AC 4: add(max_in_flight=N) honoured ----------------------------------


def test_add_max_in_flight_honoured_n_plus_one_jobs():
    """Submit N+1 jobs without release; exactly N reach backend.submit."""
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=2)
        futures = [pool.submit(_job(f"j{i}")) for i in range(3)]
        # Give workers a brief moment to pick up; assert exactly 2 in flight.
        for _ in range(50):
            if len(backend.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert len(backend.submit_log) == 2
        # Now release the first 2; the third should pick up.
        backend.release("blk-1")
        backend.release("blk-2")
        for _ in range(50):
            if len(backend.submit_log) >= 3:
                break
            time.sleep(0.01)
        assert len(backend.submit_log) == 3
        backend.release("blk-3")
        for f in futures:
            f.result(timeout=2.0)


# --- AC 4b: add(max_in_flight < 1) raises ValueError ----------------------


def test_add_max_in_flight_less_than_one_raises_valueerror():
    """add(backend, max_in_flight=0) raises ValueError."""
    backend = BlockingFakeBackend()
    pool = ConcurrentPool()
    try:
        with pytest.raises(ValueError, match="max_in_flight must be >= 1"):
            pool.add(backend, max_in_flight=0)
    finally:
        pool.close()


# --- AC 5: cap=1 serialises -----------------------------------------------


def test_cap_one_serialises_jobs():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=1)
        futures = [pool.submit(_job(f"j{i}")) for i in range(3)]
        for _ in range(50):
            if len(backend.submit_log) >= 1:
                break
            time.sleep(0.01)
        assert backend.submit_log == ["blk-1"]
        backend.release("blk-1")
        for _ in range(50):
            if len(backend.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert backend.submit_log == ["blk-1", "blk-2"]
        backend.release("blk-2")
        # Wait for blk-3 to be submitted before releasing it.
        for _ in range(50):
            if len(backend.submit_log) >= 3:
                break
            time.sleep(0.01)
        backend.release("blk-3")
        for f in futures:
            f.result(timeout=2.0)


# --- AC 6: cap=4 allows 4 in-flight ---------------------------------------


def test_cap_four_allows_four_in_flight():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=4)
        futures = [pool.submit(_job(f"j{i}")) for i in range(4)]
        for _ in range(50):
            if len(backend.submit_log) >= 4:
                break
            time.sleep(0.01)
        assert len(backend.submit_log) == 4
        for jid in backend.submit_log:
            backend.release(jid)
        for f in futures:
            f.result(timeout=2.0)


# --- AC 7: two backends caps [1,1] distribute -----------------------------


def test_two_backends_cap_one_each_distribute_one_each():
    b1 = BlockingFakeBackend(name="A")
    b2 = BlockingFakeBackend(name="B")
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=1)
        pool.add(b2, max_in_flight=1)
        futures = [pool.submit(_job(f"j{i}")) for i in range(2)]
        for _ in range(50):
            if len(b1.submit_log) >= 1 and len(b2.submit_log) >= 1:
                break
            time.sleep(0.01)
        # Registration order tie-break: first job → b1, second → b2.
        assert b1.submit_log == ["A-1"]
        assert b2.submit_log == ["B-1"]
        b1.release("A-1")
        b2.release("B-1")
        for f in futures:
            f.result(timeout=2.0)


# --- AC 8: caps [1,4] weight-distribute -----------------------------------


def test_caps_one_and_four_distribute_by_utilization():
    b1 = BlockingFakeBackend(name="A")  # cap 1
    b4 = BlockingFakeBackend(name="B")  # cap 4
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=1)
        pool.add(b4, max_in_flight=4)
        # First submit: utilization 0/1 == 0/4 → tie → b1 (registration order).
        # Subsequent submits: b1 is at 1/1=1.0 vs b4 at 0/4=0.0 → b4 every time.
        futures = [pool.submit(_job(f"j{i}")) for i in range(5)]
        for _ in range(50):
            if len(b1.submit_log) + len(b4.submit_log) >= 5:
                break
            time.sleep(0.01)
        assert len(b1.submit_log) == 1, b1.submit_log
        assert len(b4.submit_log) == 4, b4.submit_log
        for jid in b1.submit_log:
            b1.release(jid)
        for jid in b4.submit_log:
            b4.release(jid)
        for f in futures:
            f.result(timeout=2.0)


# --- AC 9: pre-occupied backend skipped -----------------------------------


def test_pre_occupied_backend_skipped_in_favour_of_lower_utilization():
    """caps [1,2]; b1 pre-occupied at 1/1=1.0; next 2 submits both go to b2.

    With b1 at cap=1 and in_flight=1 (utilization=1.0) and b2 at cap=2 and
    in_flight=0 (utilization=0.0), the next submit goes to b2 (0.0 < 1.0).
    After j1, b2 is at 1/2=0.5 vs b1's 1.0, so j2 also goes to b2.
    """
    b1 = BlockingFakeBackend(name="A")
    b2 = BlockingFakeBackend(name="B")
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=1)
        pool.add(b2, max_in_flight=2)
        # Pre-occupy b1 with one in-flight job (fully saturated: 1/1 = 1.0).
        f0 = pool.submit(_job("seed"))
        for _ in range(50):
            if len(b1.submit_log) >= 1:
                break
            time.sleep(0.01)
        assert b1.submit_log == ["A-1"]
        # b1 utilization: 1/1 = 1.0; b2 utilization: 0/2 = 0.0 → j1 → b2.
        # After j1: b1=1.0, b2=0.5 → j2 also → b2.
        f1 = pool.submit(_job("j1"))
        f2 = pool.submit(_job("j2"))
        for _ in range(50):
            if len(b2.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert len(b1.submit_log) == 1  # unchanged — b1 was skipped
        assert len(b2.submit_log) == 2
        b1.release("A-1")
        b2.release("B-1")
        b2.release("B-2")
        for f in [f0, f1, f2]:
            f.result(timeout=2.0)


# --- AC 10: after release, lowest-utilization backend wins again ---------


def test_after_release_lowest_utilization_wins():
    b1 = BlockingFakeBackend(name="A")
    b2 = BlockingFakeBackend(name="B")
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=1)
        pool.add(b2, max_in_flight=1)
        f0 = pool.submit(_job("j0"))  # → b1
        f1 = pool.submit(_job("j1"))  # → b2
        for _ in range(50):
            if b1.submit_log and b2.submit_log:
                break
            time.sleep(0.01)
        # Release b1's job; now b1 = 0/1, b2 = 1/1 → next submit goes to b1.
        b1.release("A-1")
        f0.result(timeout=2.0)
        f2 = pool.submit(_job("j2"))
        for _ in range(50):
            if len(b1.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert b1.submit_log == ["A-1", "A-2"]
        b1.release("A-2")
        b2.release("B-1")
        f1.result(timeout=2.0)
        f2.result(timeout=2.0)


# --- AC 16: close() waits for in-flight ----------------------------------


def test_close_blocks_until_inflight_completes():
    backend = BlockingFakeBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)
    fut = pool.submit(_job("j"))
    for _ in range(50):
        if backend.submit_log:
            break
        time.sleep(0.01)

    closed_event = threading.Event()

    def _close() -> None:
        pool.close()
        closed_event.set()

    closer = threading.Thread(target=_close, daemon=True)
    closer.start()
    # close() should NOT have returned yet — the job is still in-flight.
    time.sleep(0.05)
    assert not closed_event.is_set()
    backend.release("blk-1")
    # Now close() can complete.
    assert closed_event.wait(timeout=2.0)
    fut.result(timeout=2.0)


# --- AC 17: close() is idempotent -----------------------------------------


def test_close_is_idempotent():
    backend = BlockingFakeBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)
    pool.close()
    pool.close()  # must not raise


# --- AC 18: context-manager calls close even on exception ----------------


def test_context_manager_calls_close_on_exception():
    backend = BlockingFakeBackend()
    pool_ref: list[ConcurrentPool] = []

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with ConcurrentPool() as pool:
            pool_ref.append(pool)
            pool.add(backend, max_in_flight=1)
            raise _Boom("test")

    # Pool should be closed — submitting now must reject.
    with pytest.raises(RuntimeError, match="pool closed"):
        pool_ref[0].submit(_job())


# --- AC 19: stress test — invariants under parallel submit/release -------


def test_stress_in_flight_invariant_under_parallel_load():
    """8 threads each submit+release one job; in_flight must stay non-negative
    and return to 0 when all done; peak concurrency must be ≥ 2."""
    backend = BlockingFakeBackend()
    observed_max = [0]
    observed_lock = threading.Lock()
    invariant_violated = [False]

    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=4)
        # Monkey-observe the slot.
        slot = pool._slots[0]

        def _check_invariant() -> None:
            with pool._lock:
                with observed_lock:
                    if slot.in_flight > observed_max[0]:
                        observed_max[0] = slot.in_flight
                # in_flight must never go negative.
                if slot.in_flight < 0:
                    invariant_violated[0] = True

        def _worker(i: int) -> None:
            fut = pool.submit(_job(f"j{i}"))
            _check_invariant()
            # Release after a tiny delay so contention happens.
            time.sleep(0.005)
            # We can't reliably know which jid was ours; release all known gates
            # so all in-flight workers can complete.
            for known_jid in list(backend._gates.keys()):
                backend.release(known_jid)
            fut.result(timeout=5.0)
            _check_invariant()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()

        # Final invariant: counter back to 0 and never went negative.
        assert slot.in_flight == 0
        assert not invariant_violated[0], "in_flight went negative during stress test"
        # We exercised concurrency: observed at least 2 in-flight at peak.
        assert observed_max[0] >= 2, (
            f"stress test never observed concurrency; observed_max={observed_max[0]}"
        )


# --- AC 20: backend exception frees the slot -----------------------------


def test_backend_exception_releases_slot_in_finally():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=1)
        # Submit a job and mark it to fail on release.
        fut1 = pool.submit(_job("fail"))
        for _ in range(50):
            if backend.submit_log:
                break
            time.sleep(0.01)
        backend.fail_for("blk-1")
        backend.release("blk-1")
        with pytest.raises(RuntimeError, match="deliberately failed"):
            fut1.result(timeout=2.0)
        # Slot must be free again — next submit should succeed.
        fut2 = pool.submit(_job("ok"))
        for _ in range(50):
            if len(backend.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert backend.submit_log == ["blk-1", "blk-2"]
        backend.release("blk-2")
        fut2.result(timeout=2.0)


# --- counter-leak fix: executor.submit raises before _run_one starts ------


def test_submit_releases_slot_when_executor_raises():
    """If slot.executor.submit raises, the in_flight counter must be released."""
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=1)
        slot = pool._slots[0]
        # Force the next executor.submit call to raise.
        original_submit = slot.executor.submit

        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("cannot schedule")

        slot.executor.submit = _boom  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="cannot schedule"):
                pool.submit(_job("doomed"))
            # The slot counter MUST be back to 0.
            assert slot.in_flight == 0
        finally:
            slot.executor.submit = original_submit  # type: ignore[method-assign]


# --- AC 11: map([]) returns [] ; no submits -------------------------------


def test_map_empty_list_returns_empty_and_makes_no_calls():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=2)
        results = pool.map([])
        assert results == []
        assert backend.submit_log == []


# --- AC 12: map preserves input order despite reverse release order ------


def test_map_preserves_input_order_with_reverse_release():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=3)
        jobs = [_job(f"j{i}") for i in range(3)]

        # Release in reverse on a separate thread once all 3 are picked up.
        def _releaser() -> None:
            for _ in range(50):
                if len(backend.submit_log) >= 3:
                    break
                time.sleep(0.01)
            for jid in reversed(list(backend._gates.keys())):
                backend.release(jid)

        threading.Thread(target=_releaser, daemon=True).start()
        results = pool.map(jobs)
        # Results must be in INPUT order (j0, j1, j2) despite reverse release.
        assert [r.meta["jid"] for r in results] == ["blk-1", "blk-2", "blk-3"]


# --- AC 13: map fail-fast raises first exception ------------------------


def test_map_failfast_raises_first_exception_from_middle_job():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=3)
        jobs = [_job(f"j{i}") for i in range(3)]

        def _releaser() -> None:
            for _ in range(50):
                if len(backend.submit_log) >= 3:
                    break
                time.sleep(0.01)
            # Fail the middle one; release in order so j0 returns first.
            backend.release("blk-1")
            backend.fail_for("blk-2")
            backend.release("blk-2")
            backend.release("blk-3")

        threading.Thread(target=_releaser, daemon=True).start()
        with pytest.raises(RuntimeError, match="deliberately failed blk-2"):
            pool.map(jobs)


# --- AC 14: cap=1, 4 jobs, job 0 raises → queued jobs cancelled ---------


def test_map_failfast_cancels_queued_futures():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=1)
        jobs = [_job(f"j{i}") for i in range(4)]

        def _releaser() -> None:
            for _ in range(50):
                if backend.submit_log:
                    break
                time.sleep(0.01)
            backend.fail_for("blk-1")
            backend.release("blk-1")

        threading.Thread(target=_releaser, daemon=True).start()
        with pytest.raises(RuntimeError, match="deliberately failed blk-1"):
            pool.map(jobs)
        # Job 1 must have been picked up before cancel reached it; jobs 2 and
        # 3 should still be queued and thus cancellable.  However, since map
        # raised, we only need to verify that at most 1 additional job
        # reached backend.submit (job 1 is racy; jobs 2 and 3 must NOT).
        assert len(backend.submit_log) <= 2, (
            f"too many jobs reached backend after fail-fast: {backend.submit_log}"
        )


# --- AC 15: map fail-fast drains in-flight on other backend ---------------


def test_map_failfast_drains_inflight_on_other_backend():
    """2 backends cap=1, both running; backend[0]'s job fails; backend[1]'s
    job is released after and completes; map re-raises backend[0]'s exception."""
    b1 = BlockingFakeBackend(name="A")
    b2 = BlockingFakeBackend(name="B")
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=1)
        pool.add(b2, max_in_flight=1)
        jobs = [_job(f"j{i}") for i in range(2)]
        # Spawn the releaser on a thread so map() can be called synchronously.
        b2_released = threading.Event()

        def _releaser() -> None:
            for _ in range(50):
                if b1.submit_log and b2.submit_log:
                    break
                time.sleep(0.01)
            b1.fail_for("A-1")
            b1.release("A-1")
            # Wait a moment so map captures the exception before b2 completes.
            time.sleep(0.05)
            b2.release("B-1")
            b2_released.set()

        threading.Thread(target=_releaser, daemon=True).start()
        with pytest.raises(RuntimeError, match="deliberately failed A-1"):
            pool.map(jobs)
        # Verify b2 was indeed allowed to drain (releaser fired).
        assert b2_released.wait(timeout=2.0)
