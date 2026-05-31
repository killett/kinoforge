"""Lease-based mutex primitive used for cross-process coordination.

The ``Lock`` Protocol is implemented by ``InMemoryLock`` (test primitive),
``FileLock`` (LocalArtifactStore), ``S3CloudLock`` (S3ArtifactStore), and
``GCSCloudLock`` (GCSArtifactStore).  All adapters share the same external
contract — best-effort lease semantics: an acquirer holds the lock until it
either releases or the lease TTL expires, at which point another acquirer
may steal.

Failure model is documented in
``docs/superpowers/specs/2026-05-30-layer-h-cross-process-discovery-lock-design.md``
§7.  This module assumes best-effort cooperative locking; no fencing tokens,
no Paxos/Raft consensus.
"""

from __future__ import annotations

import time as _time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.errors import LockTimeout


def _sanitize_key(key: str) -> str:
    """Replace forward slashes with double underscores for filesystem safety.

    Forward slashes in lock keys (``"profiles/abc"``) would otherwise be
    interpreted as path separators by ``FileLock`` and as nested object
    keys by ``S3CloudLock``/``GCSCloudLock``.  Sanitizing once at the
    primitive boundary keeps every adapter's storage layout flat.

    Args:
        key: A logical lock key, e.g. ``"profiles/abc"``.

    Returns:
        The key with every ``"/"`` replaced by ``"__"``.
    """
    return key.replace("/", "__")


@dataclass(frozen=True)
class LockToken:
    """Opaque handle returned by ``Lock.acquire``; passed back to ``Lock.release``.

    Attributes:
        key: The logical lock key this token represents.
        nonce: A uuid4 hex string identifying this specific lease so the
            lock can detect stolen-after-TTL releases.
    """

    key: str
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)


@runtime_checkable
class Lock(Protocol):
    """Lease-based mutex acquired via ``ArtifactStore.acquire_lock``.

    Implementations MUST honour:

    * ``acquire(blocking=False)`` returns ``None`` immediately on contention.
    * ``acquire(blocking=True, timeout_s=X)`` raises ``LockTimeout`` after X
      seconds.
    * ``release(token)`` is silent when the lock was already stolen after
      its TTL expired.
    * ``__enter__`` / ``__exit__`` provide context-manager semantics; the
      token returned from ``__enter__`` matches ``acquire``.
    """

    def acquire(
        self,
        *,
        blocking: bool = True,
        timeout_s: float | None = None,
    ) -> LockToken | None:
        """Acquire the lock; return a token on success, or ``None`` on contention when non-blocking."""
        ...

    def release(self, token: LockToken) -> None:
        """Release the lock identified by ``token``; silent if the lease was stolen."""
        ...

    def __enter__(self) -> LockToken:
        """Acquire the lock and return its token for use as a context manager."""
        ...

    def __exit__(self, *exc: object) -> None:
        """Release the lock on context exit."""
        ...


class InMemoryLock:
    """Dict-backed Lock implementation for tests.

    Multiple ``InMemoryLock`` instances pointed at the same ``registry`` dict
    behave as if they were separate processes sharing the lock.  This is the
    in-process simulation of cross-process contention used throughout the
    test suite.

    Attributes:
        key: Logical lock key.
        ttl_s: Lease duration in seconds.
        clock: Wall-clock source.
        registry: Shared ``{key: {"nonce": str, "expires_at": float}}`` dict.
        sleep: Injectable sleep callable used by blocking acquires.
        poll_interval_s: Seconds between poll attempts when blocking.
    """

    def __init__(
        self,
        *,
        key: str,
        ttl_s: float,
        registry: dict[str, dict[str, float | str]],
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_interval_s: float = 0.05,
    ) -> None:
        """Initialise the in-memory lock.

        Args:
            key: Logical lock key.
            ttl_s: Lease duration in seconds.
            registry: Shared ``{key: {"nonce": str, "expires_at": float}}`` dict.
            clock: Wall-clock source; defaults to :class:`RealClock`.
            sleep: Injectable sleep callable; defaults to :func:`time.sleep`.
            poll_interval_s: Seconds between blocking-acquire poll attempts.
        """
        self._key = key
        self._ttl_s = ttl_s
        self._registry = registry
        self._clock: Clock = clock or RealClock()
        self._sleep: Callable[[float], None] = sleep or _time.sleep
        self._poll_interval_s = poll_interval_s
        self._held_token: LockToken | None = None

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def _try_take(self) -> LockToken | None:
        """One-shot, non-blocking attempt; returns token on success, else None."""
        existing = self._registry.get(self._key)
        now = self._clock.now()
        if existing is not None and float(existing["expires_at"]) > now:
            return None
        token = LockToken(key=self._key)
        self._registry[self._key] = {
            "nonce": token.nonce,
            "expires_at": now + self._ttl_s,
        }
        self._held_token = token
        return token

    def acquire(
        self,
        *,
        blocking: bool = True,
        timeout_s: float | None = None,
    ) -> LockToken | None:
        """Attempt to acquire the lock.

        Args:
            blocking: If ``False``, return ``None`` immediately on contention.
                If ``True``, poll until the lock is free or ``timeout_s`` elapses.
            timeout_s: Maximum seconds to wait when ``blocking=True``.  ``None``
                means wait indefinitely.

        Returns:
            A :class:`LockToken` on success, or ``None`` when
            ``blocking=False`` and the lock is held.

        Raises:
            LockTimeout: When ``blocking=True`` and ``timeout_s`` elapses
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
        """Release the lock if we still own it; silent when stolen after TTL.

        Args:
            token: The :class:`LockToken` returned by a prior ``acquire`` call.
        """
        existing = self._registry.get(self._key)
        if existing is None or existing.get("nonce") != token.nonce:
            self._held_token = None
            return
        del self._registry[self._key]
        self._held_token = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> LockToken:
        """Acquire the lock and return its token.

        Returns:
            The :class:`LockToken` for this lease.
        """
        token = self.acquire()
        if token is None:  # pragma: no cover — acquire() blocks until success or raises
            raise RuntimeError("InMemoryLock.acquire() returned None in blocking mode")
        return token

    def __exit__(self, *exc: object) -> None:
        """Release the lock on context exit.

        Args:
            *exc: Exception info (ignored; lock is always released).
        """
        if self._held_token is not None:
            self.release(self._held_token)
