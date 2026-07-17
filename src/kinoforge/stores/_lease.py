"""Shared lease-lock policy for the artifact-store lock backends.

The blocking-acquire retry loop, deadline/timeout handling, the
:class:`~kinoforge.core.errors.LockTimeout` message, and the
context-manager protocol are ONE policy decision shared by the local,
S3, and GCS lock adapters; only the CAS primitive (how a lease is
taken and released against the backing store) is backend-specific.

:class:`_LeaseLockBase` captures the shared policy as a template
method; concrete backends implement :meth:`_LeaseLockBase._try_take`
and :meth:`_LeaseLockBase.release`.
"""

from __future__ import annotations

import abc
import time as _time
from collections.abc import Callable

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.errors import LockTimeout
from kinoforge.core.locks import LockToken


class _LeaseLockBase(abc.ABC):
    """Template-method base class for TTL-lease locks.

    Owns the blocking-acquire poll loop, timeout handling, the
    ``LockTimeout`` message, and the ``__enter__``/``__exit__``
    context-manager protocol.  Backends supply the CAS primitives via
    :meth:`_try_take` and :meth:`release`.

    Args:
        key: Logical lock key.
        ttl_s: Lease duration in seconds recorded in the lock payload.
        clock: Wall-clock source; defaults to :class:`RealClock`.
        sleep: Injectable sleep callable for blocking poll loops.
        poll_interval_s: Seconds between blocking-acquire polls.
    """

    # Message for the defensive (unreachable) branch in ``__enter__`` where a
    # blocking ``acquire()`` returned ``None``.  Overridable per backend so
    # each keeps its historical wording byte-for-byte.
    _blocking_none_message: str = "acquire() returned None in blocking context manager"

    def __init__(
        self,
        *,
        key: str,
        ttl_s: float,
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_interval_s: float = 0.05,
    ) -> None:
        """Initialise shared lease state; see class docstring for parameter docs."""
        self._key = key
        self._ttl_s = ttl_s
        self._clock: Clock = clock or RealClock()
        self._sleep: Callable[[float], None] = sleep or _time.sleep
        self._poll_interval_s = poll_interval_s
        self._held_token: LockToken | None = None

    # ------------------------------------------------------------------
    # Backend-specific CAS primitives
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def _try_take(self) -> LockToken | None:
        """One-shot non-blocking acquire attempt against the backing store.

        Returns:
            A :class:`LockToken` on success, or ``None`` on contention.
        """

    @abc.abstractmethod
    def release(self, token: LockToken) -> None:
        """Release the lease when *token* matches the currently held token.

        Args:
            token: The :class:`LockToken` returned by :meth:`acquire`.
        """

    # ------------------------------------------------------------------
    # Shared lease policy
    # ------------------------------------------------------------------

    def acquire(
        self,
        *,
        blocking: bool = True,
        timeout_s: float | None = None,
    ) -> LockToken | None:
        """Acquire the lock, optionally blocking until success or timeout.

        Args:
            blocking: When ``False``, returns ``None`` immediately on
                contention.
            timeout_s: Maximum seconds to block; raises :class:`LockTimeout`
                when elapsed.  ``None`` means block indefinitely.

        Returns:
            A :class:`LockToken` on success, or ``None`` when
            ``blocking=False`` and the lock is held.

        Raises:
            LockTimeout: When ``blocking=True``, ``timeout_s`` is set, and the
                deadline elapses without acquiring the lock.
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

    def __enter__(self) -> LockToken:
        """Acquire the lock (blocking) and return the token.

        Returns:
            The :class:`~kinoforge.core.locks.LockToken` for this lease.
        """
        token = self.acquire()
        if token is None:  # pragma: no cover — blocking acquire never returns None
            raise RuntimeError(self._blocking_none_message)
        return token

    def __exit__(self, *exc: object) -> None:
        """Release the lock on context-manager exit.

        Args:
            *exc: Exception info (ignored).
        """
        if self._held_token is not None:
            self.release(self._held_token)
