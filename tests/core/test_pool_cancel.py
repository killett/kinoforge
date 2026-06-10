"""Bounded-shutdown semantics for ConcurrentPool.close(cancel_pending=...).

These tests pin the load-bearing behavior of Task 1 in the
graceful-interrupt plan: the pool must accept the new ``cancel_token``
kwarg on ``submit`` and the new ``cancel_pending`` / ``timeout`` kwargs
on ``close`` without changing the behavior of any caller that does not
opt in.

Bug story: today ``ConcurrentPool.close`` calls
``executor.shutdown(wait=True)`` with no upper bound. A worker thread
parked inside ``backend.result``'s blocking poll loop blocks shutdown
indefinitely — the operator must hit Ctrl-C twice to escape because the
first press only escapes ``pool.submit(...).result()`` and the second
escapes ``shutdown``. The watchdog added in this task is what ends that
UX.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.interfaces import (
    Artifact,
    GenerationBackend,
    GenerationJob,
    ModelProfile,
    Segment,
)
from kinoforge.core.pool import ConcurrentPool


@dataclass
class _SlowBackend(GenerationBackend):
    """Backend whose submit() parks the worker in ``time.sleep(60)``.

    Used to prove that ``ConcurrentPool.close(cancel_pending=True,
    timeout=...)`` no longer blocks for the full sleep duration. The
    sleep is deliberately NOT interruptible — backends that honor
    cancellation are exercised by separate tests in Task 2 / Task 3.
    Here we only need the slot to be "wedged" from the pool's POV.
    """

    name: str = "slow"
    started: threading.Event = field(default_factory=threading.Event)

    def submit(
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> str:
        del cancel_token
        self.started.set()
        time.sleep(60.0)
        return "irrelevant"

    def result(
        self,
        job_id: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Artifact:
        del job_id, cancel_token
        raise AssertionError("result() should never be reached")

    def capabilities(self) -> ModelProfile:
        return _PROBE

    def inspect_capabilities(self) -> ModelProfile:
        return _PROBE

    def endpoints(self) -> dict[str, str]:
        return {}


_PROBE = ModelProfile(
    name="slow",
    max_frames=16,
    fps=8,
    supported_modes={"t2v"},
    max_resolution=(512, 512),
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _make_job() -> GenerationJob:
    """Minimal GenerationJob — content unused by ``_SlowBackend``."""
    return GenerationJob(spec={}, segments=[Segment(prompt="x")], params={})


def test_close_returns_within_timeout_when_worker_wedged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``close(cancel_pending=True, timeout=0.5)`` returns even if worker stuck.

    Bug: ``ConcurrentPool.close`` currently calls
    ``executor.shutdown(wait=True)`` with no timeout. A worker parked in
    a forever-poll blocks shutdown indefinitely — the reason
    ``kinoforge generate`` requires two Ctrl-C presses to escape.

    What would fail this test if the bug returned: removing the
    watchdog thread + ``Event.wait(timeout)`` from
    ``_shutdown_slot`` would make ``close()`` block for the full 60s
    sleep, blowing the 1.5s assert.
    """
    backend = _SlowBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)

    fut = pool.submit(_make_job())
    assert backend.started.wait(2.0), "worker did not start"

    caplog.set_level(logging.WARNING, logger="kinoforge.core.pool")
    start = time.monotonic()
    pool.close(cancel_pending=True, timeout=0.5)
    elapsed = time.monotonic() - start

    assert elapsed < 1.5, f"close() took {elapsed:.2f}s; should have bailed at ~0.5s"
    assert any("abandoning slot" in rec.message for rec in caplog.records), (
        f"expected WARN about abandoned slot; got: {[r.message for r in caplog.records]}"
    )
    fut.cancel()


def test_close_no_kwargs_preserves_existing_behavior() -> None:
    """``close()`` without kwargs still blocks until workers finish.

    Bug guard: a sloppy refactor that silently routes the no-kwarg
    path through the watchdog would change behavior for every existing
    caller. Verify the watchdog path is opt-in: with no kwargs,
    ``close()`` must NOT return inside 0.5s while a worker is wedged
    in ``time.sleep(60)``.
    """
    backend = _SlowBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)

    fut = pool.submit(_make_job())
    assert backend.started.wait(2.0)

    closed = threading.Event()

    def _close() -> None:
        pool.close()
        closed.set()

    threading.Thread(target=_close, daemon=True).start()
    assert closed.wait(0.5) is False, (
        "close() with no kwargs returned in <0.5s — watchdog path must be opt-in"
    )
    # NOTE: we intentionally leak the wedged worker thread; the pytest
    # process exit kills the daemon. Trying to "clean up" by waiting
    # 60s would defeat the purpose of the test.
    fut.cancel()


def test_submit_accepts_cancel_token_kwarg() -> None:
    """``ConcurrentPool.submit(job, cancel_token=...)`` is accepted.

    Bug guard: catches the regression where ``cancel_token`` was
    plumbed through the ABC but not through ``ConcurrentPool.submit``,
    making the kwarg a TypeError at call time.
    """
    backend = _SlowBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)
    token = CancelToken()
    try:
        fut = pool.submit(_make_job(), cancel_token=token)
        assert backend.started.wait(2.0)
        fut.cancel()
    finally:
        pool.close(cancel_pending=True, timeout=0.5)


def test_sequential_pool_close_accepts_kwargs() -> None:
    """``SequentialPool.close(cancel_pending=..., timeout=...)`` is a no-op.

    Bug guard: the ABC-parity signature change on ``SequentialPool.close``
    must not raise — there is no executor to cancel, but callers that
    pass the new kwargs (orchestrator ``deploy_session.__exit__`` does)
    must not crash.
    """
    from kinoforge.core.pool import SequentialPool

    pool = SequentialPool()
    pool.close(cancel_pending=True, timeout=0.5)
    pool.close()  # idempotent / no kwargs
