"""Unit tests for CancelToken + Cancelled."""

from __future__ import annotations

import threading
import time

import pytest

from kinoforge.core import Cancelled, CancelToken
from kinoforge.core.cancel import _NULL_TOKEN


def test_initial_state_not_set() -> None:
    """A fresh CancelToken reports is_set() == False."""
    token = CancelToken()
    assert token.is_set() is False


def test_set_flips_is_set() -> None:
    """set() makes is_set() return True."""
    token = CancelToken()
    token.set()
    assert token.is_set() is True


def test_wait_returns_false_on_timeout() -> None:
    """wait(timeout) on an unset token returns False after the timeout."""
    token = CancelToken()
    start = time.monotonic()
    result = token.wait(0.05)
    elapsed = time.monotonic() - start
    assert result is False
    assert elapsed >= 0.04


def test_wait_returns_true_when_set_mid_wait() -> None:
    """wait() unblocks promptly when another thread calls set()."""
    token = CancelToken()

    def _setter() -> None:
        time.sleep(0.05)
        token.set()

    threading.Thread(target=_setter, daemon=True).start()
    start = time.monotonic()
    result = token.wait(1.0)
    elapsed = time.monotonic() - start
    assert result is True
    assert elapsed < 0.5, f"wait() took {elapsed:.2f}s — should have returned promptly"


def test_raise_if_set_noop_when_unset() -> None:
    """raise_if_set on an unset token does nothing."""
    token = CancelToken()
    token.raise_if_set()  # must not raise


def test_raise_if_set_raises_when_set() -> None:
    """raise_if_set on a set token raises Cancelled."""
    token = CancelToken()
    token.set()
    with pytest.raises(Cancelled):
        token.raise_if_set()


def test_null_token_is_never_set() -> None:
    """The module-level _NULL_TOKEN sentinel is never set, even after import."""
    assert _NULL_TOKEN.is_set() is False
