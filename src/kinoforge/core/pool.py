"""BackendPool implementations.

The orchestrator depends only on the BackendPool ABC + Stage + ModelProfile,
never on a concrete engine. SequentialPool is the trivial impl: it runs jobs
through the single registered backend inline and wraps the result in an
already-resolved Future so the contract is identical to what a future concurrent
pool exposes.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from dataclasses import dataclass, field

from kinoforge.core.cancel import CancelToken
from kinoforge.core.interfaces import (
    Artifact,
    BackendPool,
    GenerationBackend,
    GenerationJob,
)

_log = logging.getLogger(__name__)


class _DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor that marks every worker daemon=True at creation.

    Plan A's L1 thread-leak harvest (commit 1d83d1d) showed the
    ConcurrentPool's ThreadPoolExecutor workers leak non-daemon across
    1845 distinct test nodeids -- concurrent.futures defaults workers to
    daemon=False on Python 3.13, so any test that submits a job and does
    not also call ``pool.close()`` in teardown leaks the worker past
    process exit (blocking pytest's interpreter shutdown).

    The obvious-looking fix -- passing an ``initializer`` callback that
    sets ``threading.current_thread().daemon = True`` -- does NOT work:
    the initializer runs inside the worker AFTER it has started, and
    ``Thread.daemon`` setter raises ``RuntimeError("cannot set daemon
    status of active thread")`` on a live thread. The daemon flag must
    be set BEFORE ``Thread.start()``, which is what this subclass does
    by overriding the private ``_adjust_thread_count`` hook.

    Graceful shutdown is unchanged because ``executor.shutdown(wait=True)``
    still joins each worker; on ungraceful exit the workers now die with
    the process instead of blocking pytest's interpreter shutdown.
    """

    def _adjust_thread_count(self) -> None:  # noqa: D401, D102
        # Stdlib's _adjust_thread_count spawns a single Thread per call when
        # the pool has spare capacity. We rebuild the same logic here so we
        # can flip daemon=True before start() -- the only effective moment.
        import weakref
        from concurrent.futures.thread import (
            _threads_queues,
            _worker,
        )

        def _weakref_cb(_: object, q: object = self._work_queue) -> None:
            q.put(None)  # type: ignore[attr-defined]

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = f"{self._thread_name_prefix or self}_{num_threads}"
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, _weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            t.start()
            self._threads.add(t)  # type: ignore[attr-defined]
            _threads_queues[t] = self._work_queue  # type: ignore[index]


class SequentialPool(BackendPool):
    """Trivial single-backend pool that runs jobs inline.

    Jobs submitted to this pool are processed synchronously by the first
    registered backend. The returned ``Future`` is already resolved when
    ``submit`` returns, so callers can call ``.result()`` without blocking.

    This is the reference implementation of :class:`BackendPool`; concurrent
    and multi-backend variants are DEFERRED.

    Attributes:
        _backends: Ordered list of registered backends. Submission always
            uses ``_backends[0]``.
    """

    def __init__(self, backend: GenerationBackend | None = None) -> None:
        """Initialise, optionally pre-registering one backend.

        Args:
            backend: Optional backend to register immediately. When ``None``
                the pool starts empty and a backend must be added via
                :meth:`add` before any submission.
        """
        self._backends: list[GenerationBackend] = []
        if backend is not None:
            self._backends.append(backend)

    def add(
        self,
        backend: GenerationBackend,
        *,
        max_in_flight: int = 1,
    ) -> None:
        """Append *backend* to the internal backend list.

        Args:
            backend: The :class:`~kinoforge.core.interfaces.GenerationBackend`
                to register.
            max_in_flight: Accepted for :class:`BackendPool` ABC parity with
                :class:`ConcurrentPool`; ignored by ``SequentialPool`` because
                only ``_backends[0]`` is ever used.
        """
        self._backends.append(backend)

    def submit(
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> concurrent.futures.Future[Artifact]:
        """Run *job* inline through the first registered backend.

        The returned ``Future`` is already in the ``done`` state.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` to execute.
            cancel_token: Optional :class:`CancelToken`. Forwarded
                verbatim to ``backend.submit`` and ``backend.result``;
                ``None`` preserves today's library-caller behavior.

        Returns:
            An already-resolved ``Future[Artifact]`` containing the result.

        Raises:
            RuntimeError: No backend has been registered.
        """
        if not self._backends:
            raise RuntimeError("SequentialPool has no registered backend")
        backend = self._backends[0]
        job_id = backend.submit(job, cancel_token=cancel_token)
        artifact = backend.result(job_id, cancel_token=cancel_token)
        fut: concurrent.futures.Future[Artifact] = concurrent.futures.Future()
        fut.set_result(artifact)
        return fut

    def map(self, jobs: list[GenerationJob]) -> list[Artifact]:
        """Execute all *jobs* in order and return their results.

        Results are returned in the same order as the input jobs (not as-completed),
        because this pool has no concurrency.

        Args:
            jobs: Ordered list of :class:`~kinoforge.core.interfaces.GenerationJob`
                to execute. An empty list returns ``[]``.

        Returns:
            List of :class:`~kinoforge.core.interfaces.Artifact` in input order.
        """
        return [self.submit(j).result() for j in jobs]

    def close(
        self,
        *,
        cancel_pending: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Release any resources held by this pool.

        ``SequentialPool`` owns no threads or open handles; this is a no-op
        provided for :class:`BackendPool` ABC parity with concurrent pools
        that must drain worker threads.

        Args:
            cancel_pending: Accepted for ABC parity with
                :class:`ConcurrentPool`; ignored — there is no executor
                with queued futures to cancel.
            timeout: Accepted for ABC parity; ignored.
        """
        del cancel_pending, timeout
        return None


@dataclass
class _Slot:
    """Per-backend bookkeeping inside :class:`ConcurrentPool`.

    Attributes:
        backend: The registered :class:`GenerationBackend`.
        executor: The dedicated :class:`concurrent.futures.ThreadPoolExecutor`,
            sized to ``cap``.
        cap: The per-backend ``max_in_flight`` cap.
        in_flight: Live count of jobs in this slot's executor (queued or
            running). Mutated only under :attr:`ConcurrentPool._lock`.
    """

    backend: GenerationBackend
    executor: concurrent.futures.ThreadPoolExecutor
    cap: int
    in_flight: int = field(default=0)


class ConcurrentPool(BackendPool):
    """Bounded-concurrency pool across one or more backend replicas.

    Each registered backend owns one
    :class:`concurrent.futures.ThreadPoolExecutor` sized to its
    ``max_in_flight`` cap.  :meth:`submit` picks the least-loaded backend by
    ``in_flight / cap`` utilization (ties broken by registration order) and
    forwards the call to that backend's executor.  Returned ``Future`` may
    be pending if all workers are busy; the executor's internal queue holds
    overflow — the caller never blocks.

    :meth:`map` dispatches every job eagerly, then resolves futures in input
    order.  On the first exception it cancels still-queued futures, drains
    in-flight ones (results discarded), and re-raises the captured exception.

    Use as a context manager for deterministic shutdown::

        with ConcurrentPool() as pool:
            pool.add(backend, max_in_flight=4)
            results = pool.map(jobs)

    Attributes:
        _slots: Per-backend state; appended to by :meth:`add`.
        _lock: Guards :attr:`_Slot.in_flight` and the ``_closed`` flag.
        _closed: Set by :meth:`close`; subsequent :meth:`submit` calls raise.
    """

    def __init__(self) -> None:
        """Construct an empty pool. Use :meth:`add` to register backends."""
        self._slots: list[_Slot] = []
        self._lock = threading.Lock()
        self._closed = False

    def add(
        self,
        backend: GenerationBackend,
        *,
        max_in_flight: int = 1,
    ) -> None:
        """Register *backend* with its per-replica concurrency cap.

        Args:
            backend: The :class:`GenerationBackend` to dispatch through.
            max_in_flight: Maximum concurrent in-flight calls to this
                backend.  Defaults to 1 (equivalent to sequential).  Must be
                a positive integer.

        Raises:
            ValueError: If ``max_in_flight`` is not a positive integer.
        """
        if max_in_flight < 1:
            raise ValueError(f"max_in_flight must be >= 1, got {max_in_flight}")
        executor = _DaemonThreadPoolExecutor(
            max_workers=max_in_flight,
            thread_name_prefix=f"kinoforge-pool-{len(self._slots)}",
        )
        self._slots.append(_Slot(backend=backend, executor=executor, cap=max_in_flight))

    def submit(
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> concurrent.futures.Future[Artifact]:
        """Dispatch *job* to the least-loaded backend.

        Args:
            job: The :class:`GenerationJob` to execute.
            cancel_token: Optional :class:`CancelToken` forwarded to the
                chosen backend's ``submit`` and ``result`` calls. ``None``
                preserves today's library-caller behavior.

        Returns:
            A :class:`concurrent.futures.Future` resolving to the Artifact.
            May be pending when all workers are busy; the chosen executor's
            internal queue holds overflow.

        Raises:
            RuntimeError: The pool has been closed, OR no backend has been
                added yet.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("pool closed")
            if not self._slots:
                raise RuntimeError("ConcurrentPool has no registered backend")
        slot = self._pick()
        try:
            return slot.executor.submit(self._run_one, slot, job, cancel_token)
        except BaseException:
            self._release(slot)
            raise

    def close(
        self,
        *,
        cancel_pending: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Shut down every per-backend executor.

        Two-phase: flip the ``_closed`` flag under the lock so new
        :meth:`submit` calls reject immediately; then shut down each slot
        outside the lock so long-running shutdowns do not serialise.

        Args:
            cancel_pending: When ``True``, queued-but-not-started futures
                are cancelled via ``cancel_futures=True``. Running workers
                still finish their current poll tick (cooperative
                cancellation happens via the :class:`CancelToken` passed
                through :meth:`submit`).
            timeout: Per-slot wait cap in seconds. When set, the shutdown
                joins each slot in a watchdog thread and logs ``WARN
                "worker still running after %.1fs; abandoning slot"`` if
                the join exceeds the cap. ``None`` preserves the
                unconditional-wait behavior expected by existing callers.

        Idempotent — second call is a no-op.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            slots = list(self._slots)
        for slot in slots:
            _shutdown_slot(slot, cancel_pending=cancel_pending, timeout=timeout)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _pick(self) -> _Slot:
        """Return the least-loaded slot, bumping its in_flight counter.

        Selection is by ``in_flight / cap`` ratio.  Ties broken by
        registration order via CPython ``min(iter, key=...)`` returning the
        first occurrence of the minimum key (documented behaviour).

        Returns:
            The chosen :class:`_Slot` with ``in_flight`` already incremented.
        """
        with self._lock:
            best = min(self._slots, key=lambda s: s.in_flight / s.cap)
            best.in_flight += 1
            return best

    def _release(self, slot: _Slot) -> None:
        """Decrement *slot*'s in_flight counter under the lock.

        Args:
            slot: The :class:`_Slot` whose worker has finished a job.
        """
        with self._lock:
            slot.in_flight -= 1

    def _run_one(
        self,
        slot: _Slot,
        job: GenerationJob,
        cancel_token: CancelToken | None,
    ) -> Artifact:
        """Run *job* through *slot*'s backend, ensuring counter release.

        Args:
            slot: The :class:`_Slot` chosen by :meth:`_pick`.
            job: The :class:`GenerationJob`.
            cancel_token: Token forwarded verbatim to ``backend.submit`` and
                ``backend.result``; ``None`` is forwarded unchanged so
                backends that do not honor cancellation observe today's
                behavior.

        Returns:
            The :class:`Artifact` produced by the backend.

        Raises:
            Any exception raised by ``backend.submit`` or ``backend.result``
            is re-raised after the slot counter is released.
        """
        try:
            job_id = slot.backend.submit(job, cancel_token=cancel_token)
            return slot.backend.result(job_id, cancel_token=cancel_token)
        finally:
            self._release(slot)

    def map(self, jobs: list[GenerationJob]) -> list[Artifact]:
        """Dispatch every job and return results in input order; fail-fast.

        All jobs are submitted eagerly; results are awaited in input order.
        On first exception, every still-queued future is cancelled (in-flight
        jobs continue and their outcomes are discarded), and the first
        exception is re-raised.

        Args:
            jobs: Ordered list of :class:`GenerationJob`.  Empty list returns
                ``[]`` with no backend calls.

        Returns:
            List of :class:`Artifact` in the same order as *jobs*.

        Raises:
            BaseException: The first exception raised by any backend; queued
                futures are cancelled before the re-raise.  In-flight futures
                are allowed to complete; their results (success or further
                exceptions) are silently discarded.
        """
        if not jobs:
            return []
        futures = [self.submit(j) for j in jobs]
        results: list[Artifact | None] = [None] * len(jobs)
        first_exc: BaseException | None = None
        for i, fut in enumerate(futures):
            if first_exc is not None:
                fut.cancel()
                continue
            try:
                results[i] = fut.result()
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                first_exc = exc
        if first_exc is not None:
            raise first_exc
        # All slots filled; cast is safe because no None remains.
        return [r for r in results if r is not None]


def _shutdown_slot(
    slot: _Slot,
    *,
    cancel_pending: bool,
    timeout: float | None,
) -> None:
    """Best-effort bounded shutdown of one slot's executor.

    When ``timeout is None`` the call delegates to
    ``executor.shutdown(wait=True, cancel_futures=cancel_pending)`` and
    blocks until every worker exits — today's behavior, preserved for
    callers that don't opt in to the watchdog.

    When ``timeout`` is set, the executor shutdown runs in a daemon
    watchdog thread and the main thread waits on a
    :class:`threading.Event` for up to ``timeout`` seconds. On expiry,
    a single WARN is logged and control returns — the worker thread is
    leaked (daemon, dies with the process). This is the load-bearing
    primitive that ends the "second Ctrl-C required" UX: even if a
    backend never honors the cancel token, pool shutdown returns within
    ``timeout`` seconds.

    Args:
        slot: The slot whose executor to shut down.
        cancel_pending: Forwarded to ``executor.shutdown(cancel_futures=)``
            so queued-but-not-started futures are cancelled. Running
            workers are NOT interrupted by this flag — cancellation of
            running workers happens cooperatively via the
            :class:`CancelToken` plumbed through ``submit``.
        timeout: Per-slot wait cap in seconds, or ``None`` to wait
            unconditionally.
    """
    if timeout is None:
        slot.executor.shutdown(wait=True, cancel_futures=cancel_pending)
        return
    done = threading.Event()

    def _do_shutdown() -> None:
        slot.executor.shutdown(wait=True, cancel_futures=cancel_pending)
        done.set()

    watchdog = threading.Thread(
        target=_do_shutdown,
        daemon=True,
        name=f"kinoforge-pool-shutdown-{id(slot)}",
    )
    watchdog.start()
    if not done.wait(timeout):
        _log.warning(
            "worker still running after %.1fs; abandoning slot",
            timeout,
        )
