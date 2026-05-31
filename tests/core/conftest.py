"""Shared fixtures for kinoforge.core tests.

Provides :class:`BlockingFakeBackend`: an Event-gated GenerationBackend whose
``result()`` blocks until the test explicitly calls ``release(job_id)``.
Use it to assert deterministic dispatch ordering in concurrent-pool tests
without sleeps.
"""

from __future__ import annotations

import threading

from kinoforge.core.interfaces import (
    Artifact,
    GenerationBackend,
    GenerationJob,
    ModelProfile,
)


class BlockingFakeBackend(GenerationBackend):
    """GenerationBackend whose result() blocks on a per-job threading.Event.

    Tests control completion order by calling :meth:`release` with the job id
    returned by :meth:`submit`. A 5-second safety timeout in :meth:`result`
    prevents test hangs from masking ordering bugs.

    Attributes:
        submit_log: Job IDs in the order ``submit()`` was called. Read by tests
            to assert which backend received which job.
    """

    def __init__(self, name: str = "blk") -> None:
        """Initialise with empty state.

        Args:
            name: Optional name prefix used in generated job IDs; useful for
                disambiguating jobs across multiple backends in a test.
        """
        self._name = name
        self._gates: dict[str, threading.Event] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self._raise_for: set[str] = set()
        self.submit_log: list[str] = []

    def capabilities(self) -> ModelProfile:
        """Return a minimal ModelProfile stub for ABC compliance.

        Returns:
            A :class:`ModelProfile` with placeholder values; unused in tests.
        """
        return ModelProfile(
            name="blocking-fake",
            max_frames=16,
            fps=8,
            supported_modes={"t2v"},
            max_resolution=(512, 512),
            supports_native_extension=False,
            supports_joint_audio=False,
        )

    def inspect_capabilities(self) -> ModelProfile:
        """Return a minimal ModelProfile stub for ABC compliance.

        Returns:
            A :class:`ModelProfile` with placeholder values; unused in tests.
        """
        return self.capabilities()

    def endpoints(self) -> dict[str, str]:
        """Return an empty endpoints dict.

        Returns:
            Empty dict for ABC compliance; unused in tests.
        """
        return {}

    def submit(self, job: GenerationJob) -> str:
        """Register a new job id, return it. Does not block.

        Args:
            job: The :class:`GenerationJob`; only used to advance the counter.

        Returns:
            A unique job id of the form ``"<name>-<counter>"``.
        """
        with self._lock:
            self._counter += 1
            jid = f"{self._name}-{self._counter}"
            self._gates[jid] = threading.Event()
            self.submit_log.append(jid)
        return jid

    def result(self, job_id: str) -> Artifact:
        """Block until ``release(job_id)`` is called; then return an Artifact.

        Args:
            job_id: The id returned by :meth:`submit`.

        Returns:
            An :class:`Artifact` whose filename and meta encode the job id.

        Raises:
            TimeoutError: The Event was not set within 5 seconds — typically
                indicates a test forgot to call :meth:`release`.
            RuntimeError: ``job_id`` was added to ``_raise_for`` via
                :meth:`fail_for` — used to test failure paths.
        """
        if not self._gates[job_id].wait(timeout=5.0):
            raise TimeoutError(f"{job_id} never released")
        if job_id in self._raise_for:
            raise RuntimeError(f"backend deliberately failed {job_id}")
        return Artifact(filename=f"{job_id}.mp4", meta={"jid": job_id})

    def release(self, job_id: str) -> None:
        """Unblock a pending :meth:`result` call.

        Args:
            job_id: The id returned by :meth:`submit`.
        """
        self._gates[job_id].set()

    def fail_for(self, job_id: str) -> None:
        """Mark *job_id* so that :meth:`result` raises ``RuntimeError`` on release.

        Args:
            job_id: The id returned by :meth:`submit`.
        """
        self._raise_for.add(job_id)
