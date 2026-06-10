"""Two-press SIGINT handler — first sets token, second re-raises.

Phase 50 Task 5 — these tests pin the CLI signal-handler contract:

1. First ``SIGINT`` while the handler is installed sets the shared
   ``CancelToken`` and does NOT raise — the orchestrator + backends
   unwind cooperatively.
2. Second ``SIGINT`` restores the default handler and re-raises
   ``KeyboardInterrupt`` so the operator can always force-exit.

The tests save the prior SIGINT handler in a ``try/finally`` so a
failure here can never corrupt the global signal state for subsequent
tests in the same pytest process.
"""

from __future__ import annotations

import signal

import pytest

from kinoforge.cli._main import _install_sigint_handler
from kinoforge.core import CancelToken


def test_first_signal_sets_token_no_raise() -> None:
    """First Ctrl-C sets the shared CancelToken and does NOT raise.

    Bug: today the CLI installs no SIGINT handler, so Ctrl-C surfaces as
    a raw KeyboardInterrupt mid-orchestration — the orchestrator's
    backend poll loop never observes the operator's intent and the pod
    leaks because the cleanup path runs only on graceful exit.
    """
    token = CancelToken()
    prior = signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        _install_sigint_handler(token)
        # First press: handler intercepts, flips the token, no raise.
        signal.raise_signal(signal.SIGINT)
        assert token.is_set() is True
    finally:
        signal.signal(signal.SIGINT, prior)


def test_second_signal_reraises_and_restores_default() -> None:
    """Second Ctrl-C restores SIG_DFL and re-raises KeyboardInterrupt.

    Bug: today there is no escape hatch — a wedged backend with no
    cancellation honoring could trap the operator in an unkillable
    process. The two-press contract guarantees a force-exit is always
    one Ctrl-C away once the cooperative drain begins.
    """
    token = CancelToken()
    prior = signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        _install_sigint_handler(token)
        # First press: sets token, no raise.
        signal.raise_signal(signal.SIGINT)
        # Second press: restores default handler, raises KeyboardInterrupt.
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
        # After re-raise, the default handler is back in place so a third
        # press would terminate the process the usual way (no token check).
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGINT, prior)
