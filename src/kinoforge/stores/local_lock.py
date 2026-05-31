"""POSIX file lock backed by ``fcntl.flock``.

Layout: ``<store_root>/_locks/<sanitized_key>.lock``.  Each lock file is a
JSON sidecar containing ``{nonce, holder_pid, expires_at}``.  The OS owns
mutual exclusion via ``fcntl.flock(LOCK_EX | LOCK_NB)``; the JSON payload
is informational and used to identify the holder for diagnostic logs.

Best-effort lease semantics:

* ``fcntl`` releases automatically when the holding process dies.
* TTL is recorded but not enforced on the same host — the OS guarantee
  is stronger than any TTL check we could do.
* Cross-host TTL stealing is the cloud adapters' concern (Tasks 4–5).
"""

from __future__ import annotations

import fcntl
import json
import os
import time as _time
from collections.abc import Callable
from pathlib import Path

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.errors import LockTimeout
from kinoforge.core.locks import LockToken


class FileLock:
    """fcntl-backed Lock implementation for LocalArtifactStore.

    Args:
        path: Sidecar path (``<root>/_locks/<sanitized_key>.lock``).
        key: Logical lock key (unsanitized) used to populate ``LockToken.key``.
        ttl_s: Lease duration recorded in the sidecar JSON.
        clock: Wall-clock source; defaults to :class:`RealClock`.
        flock_fn: Injectable replacement for :func:`fcntl.flock`; tests pass
            a spy.
        sleep: Injectable sleep callable for blocking poll loops.
        poll_interval_s: Seconds between blocking-acquire polls.
    """

    def __init__(
        self,
        *,
        path: Path,
        key: str,
        ttl_s: float,
        clock: Clock | None = None,
        flock_fn: Callable[[int, int], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_interval_s: float = 0.05,
    ) -> None:
        """Initialise a FileLock for *key* backed by the file at *path*.

        Args:
            path: Sidecar path (``<root>/_locks/<sanitized_key>.lock``).
            key: Logical lock key (unsanitized).
            ttl_s: Lease duration recorded in the sidecar JSON.
            clock: Wall-clock source; defaults to :class:`RealClock`.
            flock_fn: Injectable replacement for :func:`fcntl.flock`.
            sleep: Injectable sleep callable for blocking poll loops.
            poll_interval_s: Seconds between blocking-acquire polls.
        """
        self._path = path
        self._key = key
        self._ttl_s = ttl_s
        self._clock: Clock = clock or RealClock()
        self._flock: Callable[[int, int], None] = flock_fn or fcntl.flock
        self._sleep: Callable[[float], None] = sleep or _time.sleep
        self._poll_interval_s = poll_interval_s
        self._fd: int | None = None
        self._held_token: LockToken | None = None

    def _try_take(self) -> LockToken | None:
        """One-shot non-blocking acquire; returns token on success else None."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        token = LockToken(key=self._key)
        payload = {
            "nonce": token.nonce,
            "holder_pid": os.getpid(),
            "expires_at": self._clock.now() + self._ttl_s,
        }
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(payload).encode("utf-8"))
        self._fd = fd
        self._held_token = token
        return token

    def acquire(
        self,
        *,
        blocking: bool = True,
        timeout_s: float | None = None,
    ) -> LockToken | None:
        """Acquire the file lock.

        Args:
            blocking: If ``False``, return ``None`` immediately on contention.
                If ``True``, poll until the lock is obtained or *timeout_s*
                elapses.
            timeout_s: Maximum seconds to wait when *blocking* is ``True``.
                ``None`` means wait indefinitely.

        Returns:
            A :class:`~kinoforge.core.locks.LockToken` on success, or ``None``
            when *blocking* is ``False`` and the lock is held.

        Raises:
            LockTimeout: When *blocking* is ``True`` and *timeout_s* elapses
                without obtaining the lock.
        """
        if not blocking:
            return self._try_take()
        deadline = self._clock.now() + timeout_s if timeout_s is not None else None
        while True:
            token = self._try_take()
            if token is not None:
                return token
            if deadline is not None and self._clock.now() >= deadline:
                raise LockTimeout(
                    f"failed to acquire lock {self._key!r} within {timeout_s}s"
                )
            self._sleep(self._poll_interval_s)

    def release(self, token: LockToken) -> None:
        """Release the OS lock and remove the sidecar.

        Silent when the token does not match the currently held token (e.g.
        after a TTL steal by another process).

        Args:
            token: The :class:`~kinoforge.core.locks.LockToken` returned by
                :meth:`acquire`.
        """
        if self._held_token is None or self._held_token.nonce != token.nonce:
            return
        if self._fd is not None:
            try:
                self._flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        self._held_token = None

    def __enter__(self) -> LockToken:
        """Acquire the lock and return the token.

        Returns:
            The :class:`~kinoforge.core.locks.LockToken` for this lease.
        """
        token = self.acquire()
        if token is None:  # pragma: no cover — blocking acquire never returns None
            raise RuntimeError("acquire() returned None in blocking context manager")
        return token

    def __exit__(self, *exc: object) -> None:
        """Release the lock on context-manager exit.

        Args:
            *exc: Exception info (ignored).
        """
        if self._held_token is not None:
            self.release(self._held_token)
