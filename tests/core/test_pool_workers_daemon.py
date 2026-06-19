"""Unit tests for ConcurrentPool worker thread daemon-flag contract.

The L1 thread-leak policy (Plan A spec, Plan B Task 2 enforcement)
requires every pool worker thread to be `daemon=True`. Without this,
any test that constructs a ConcurrentPool and submits a job leaks a
non-daemon worker named `kinoforge-pool-N_M` past test teardown --
which Plan A's harvest captured across 1845 distinct test nodeids.

These tests pin the fix at the construction site.
"""

from __future__ import annotations

import threading

from kinoforge.core.pool import ConcurrentPool

from .conftest import BlockingFakeBackend


def _job() -> object:
    """Build a minimal GenerationJob for tests that don't inspect content."""
    from kinoforge.core.interfaces import GenerationJob, Segment

    return GenerationJob(
        spec={},
        params={},
        segments=[Segment(prompt="ignored")],
    )


def test_pool_workers_are_daemon_after_submit() -> None:
    """Every alive thread named `kinoforge-pool-*` must be daemon=True.

    Catches: dropping the `initializer=_mark_thread_daemon` arg from the
    ThreadPoolExecutor construction in pool.py, or accidentally setting
    daemon=False inside the initializer.
    """
    backend = BlockingFakeBackend()
    pool = ConcurrentPool()
    try:
        pool.add(backend, max_in_flight=2)
        fut = pool.submit(_job())  # type: ignore[arg-type]
        # Release immediately so the worker reaches a steady state but
        # the executor still holds it as an idle worker.
        backend.release("blk-1")
        art = fut.result(timeout=5.0)
        assert art.meta["jid"] == "blk-1"

        pool_workers = [
            t
            for t in threading.enumerate()
            if t.name.startswith("kinoforge-pool-") and t.is_alive()
        ]
        assert pool_workers, (
            "no pool workers found after submit; pool may have shut down prematurely"
        )
        non_daemon = [t for t in pool_workers if not t.daemon]
        assert not non_daemon, (
            f"non-daemon pool workers found: {[(t.name, t.ident) for t in non_daemon]}"
        )
    finally:
        pool.close(timeout=2.0)


def test_worker_observes_daemon_true_while_running_work() -> None:
    """A callable running on a pool worker sees current_thread().daemon=True.

    Catches: setting daemon on the wrong thread (e.g. the constructor's
    caller thread), or regressing the executor back to the stdlib
    `ThreadPoolExecutor` whose workers default to daemon=False on
    Python 3.13.
    """
    backend = BlockingFakeBackend()
    pool = ConcurrentPool()
    try:
        pool.add(backend, max_in_flight=1)

        observed_daemon: list[bool] = []
        observed_event = threading.Event()

        def _probe_target() -> None:
            observed_daemon.append(threading.current_thread().daemon)
            observed_event.set()

        fut = pool._slots[0].executor.submit(_probe_target)
        fut.result(timeout=5.0)
        assert observed_event.is_set(), "probe target was never invoked"
        assert observed_daemon == [True], (
            f"worker thread saw daemon={observed_daemon!r}; expected [True]"
        )
    finally:
        pool.close(timeout=2.0)
