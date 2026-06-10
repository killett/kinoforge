"""Cooperative-cancellation primitive used across orchestrator + backends."""

from __future__ import annotations

import threading

from kinoforge.core.errors import Cancelled


class CancelToken:
    """Thin :class:`threading.Event` wrapper used to request cancellation.

    The class deliberately exposes a narrow surface so backends never grab
    the underlying :class:`~threading.Event`. Backends should treat the
    token as opaque: call :meth:`raise_if_set` before any blocking I/O and
    use :meth:`wait` in place of :func:`time.sleep` so an inter-poll sleep
    can be interrupted promptly.

    Thread-safe by virtue of :class:`threading.Event`.
    """

    def __init__(self) -> None:
        """Initialise an unset token wrapping a fresh :class:`threading.Event`."""
        self._event = threading.Event()

    def set(self) -> None:
        """Request cancellation.

        Safe to call from any thread (signal handler, sibling worker, etc.).
        """
        self._event.set()

    def is_set(self) -> bool:
        """Return True if cancellation has been requested."""
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Block for up to ``timeout`` seconds.

        Returns ``True`` if the token was set during the wait, ``False`` if
        the timeout expired first. Drop-in replacement for ``time.sleep``
        in poll loops that need to honor cancellation.

        Args:
            timeout: Maximum wait in seconds.

        Returns:
            ``True`` when the token is (or becomes) set, ``False`` on
            timeout.
        """
        return self._event.wait(timeout)

    def raise_if_set(self) -> None:
        """Raise :class:`Cancelled` if the token has been set.

        Cheap polling primitive — call at the top of every poll iteration
        before any blocking I/O.

        Raises:
            Cancelled: When :meth:`is_set` is ``True``.
        """
        if self._event.is_set():
            raise Cancelled("cancellation requested by operator")


_NULL_TOKEN: CancelToken = CancelToken()
"""Sentinel token that is never set.

Used as the default value for ``cancel_token`` kwargs throughout the
codebase so library + test callers that pass no token get unchanged
behavior. Do **not** call :meth:`CancelToken.set` on this instance.
"""
