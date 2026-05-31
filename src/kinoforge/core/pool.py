"""BackendPool implementations.

The orchestrator depends only on the BackendPool ABC + Stage + ModelProfile,
never on a concrete engine. SequentialPool is the trivial impl: it runs jobs
through the single registered backend inline and wraps the result in an
already-resolved Future so the contract is identical to what a future concurrent
pool exposes.
"""

from __future__ import annotations

import concurrent.futures

from kinoforge.core.interfaces import (
    Artifact,
    BackendPool,
    GenerationBackend,
    GenerationJob,
)


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

    def submit(self, job: GenerationJob) -> concurrent.futures.Future[Artifact]:
        """Run *job* inline through the first registered backend.

        The returned ``Future`` is already in the ``done`` state.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` to execute.

        Returns:
            An already-resolved ``Future[Artifact]`` containing the result.

        Raises:
            RuntimeError: No backend has been registered.
        """
        if not self._backends:
            raise RuntimeError("SequentialPool has no registered backend")
        backend = self._backends[0]
        job_id = backend.submit(job)
        artifact = backend.result(job_id)
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

    def close(self) -> None:
        """Release any resources held by this pool.

        ``SequentialPool`` owns no threads or open handles; this is a no-op
        provided for :class:`BackendPool` ABC parity with concurrent pools
        that must drain worker threads.
        """
        return None
