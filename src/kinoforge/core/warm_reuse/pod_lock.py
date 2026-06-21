"""PodLockRegistry — per-pod_id threading.Lock serialization.

In-process only. Multi-process kinoforge instances on the same machine
will NOT see each other's locks (documented limitation; tracked under
Layer H deferred follow-up).

Holds the lock for the duration of (POST /lora/set_stack + POST
/generate + result()) so two concurrent generate jobs cannot fight
over the same pod's LoRA state. The pod is the unit of serialization.

CPython's threading.Lock is implemented over a non-robust pthread
mutex on POSIX, so a thread that acquires then dies without releasing
leaves the lock permanently held. To satisfy the "thread death
releases lock" contract the registry tracks each lock's owner thread
ident and force-releases on acquire when the owner has exited
(zombie-reap pattern). This keeps recursive-acquire blocked (the
serialization semantic we actually need) while preventing permanent
dead-thread starvation.
"""

from __future__ import annotations

import threading
import time


class PodLockRegistry:
    """Per-pod_id Lock registry with zombie-thread reaping."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._registry_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._owners: dict[str, int] = {}

    def _get_or_create(self, pod_id: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(pod_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[pod_id] = lock
            return lock

    def _maybe_reap_zombie(self, pod_id: str) -> bool:
        """If the recorded owner thread has exited, force-release the lock.

        Returns:
            True if a zombie was reaped (caller should retry acquire).
        """
        with self._registry_lock:
            owner = self._owners.get(pod_id)
            if owner is None:
                return False
            alive_idents = {t.ident for t in threading.enumerate() if t.is_alive()}
            if owner in alive_idents:
                return False
            lock = self._locks.get(pod_id)
            if lock is None:
                return False
            try:
                lock.release()
            except RuntimeError:
                pass
            self._owners.pop(pod_id, None)
            return True

    def acquire(
        self, pod_id: str, *, blocking: bool = False, timeout: float | None = None
    ) -> bool:
        """Acquire the per-pod lock.

        Args:
            pod_id: Pod identifier.
            blocking: When False (default), return immediately.
            timeout: When blocking=True, wait at most this many seconds.

        Returns:
            True iff the lock was acquired (or stolen from a dead owner).
        """
        lock = self._get_or_create(pod_id)
        if not blocking:
            got = lock.acquire(blocking=False)
            if not got and self._maybe_reap_zombie(pod_id):
                got = lock.acquire(blocking=False)
            if got:
                self._owners[pod_id] = threading.get_ident()
            return got
        deadline: float | None = None if timeout is None else time.monotonic() + timeout
        poll_step = 0.02
        while True:
            got = lock.acquire(blocking=False)
            if got:
                self._owners[pod_id] = threading.get_ident()
                return True
            if self._maybe_reap_zombie(pod_id):
                continue
            if deadline is None:
                if lock.acquire(blocking=True):
                    self._owners[pod_id] = threading.get_ident()
                    return True
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll_step, remaining))

    def release(self, pod_id: str) -> None:
        """Release the per-pod lock. Caller must currently hold it."""
        lock = self._get_or_create(pod_id)
        with self._registry_lock:
            self._owners.pop(pod_id, None)
        lock.release()

    def __contains__(self, pod_id: str) -> bool:
        """Return True iff the per-pod lock is currently held."""
        lock = self._locks.get(pod_id)
        if lock is None:
            return False
        return lock.locked()
