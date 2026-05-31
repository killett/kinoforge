"""Conformance tests for the Lock Protocol + InMemoryLock test primitive.

Each test names a behavior under test and a concrete failure mode that
would make it fail.
"""

from __future__ import annotations

import pytest
from pytest import MonkeyPatch

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import KinoforgeError, LockError, LockTimeout
from kinoforge.core.locks import InMemoryLock, Lock, LockToken, _sanitize_key

# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


def test_lock_error_subclasses_kinoforge_error() -> None:
    """LockError must subclass KinoforgeError so callers can catch the umbrella.

    Fails if errors.py omits the subclass relationship (callers using
    `except KinoforgeError` would miss lock failures).
    """
    assert issubclass(LockError, KinoforgeError)


def test_lock_timeout_subclasses_lock_error() -> None:
    """LockTimeout must be catchable as LockError for callers that don't care why."""
    assert issubclass(LockTimeout, LockError)


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


def test_in_memory_lock_satisfies_lock_protocol() -> None:
    """Runtime isinstance check against Lock Protocol catches signature drift.

    Fails if InMemoryLock omits any method declared on the Protocol.
    """
    lock = InMemoryLock(key="k", ttl_s=10.0, registry={}, clock=FakeClock(start=0.0))
    assert isinstance(lock, Lock)


def test_lock_token_carries_key() -> None:
    """LockToken must surface the key it represents so callers can correlate."""
    lock = InMemoryLock(
        key="profiles/abc", ttl_s=5.0, registry={}, clock=FakeClock(start=0.0)
    )
    token = lock.acquire()
    assert isinstance(token, LockToken)
    assert token.key == "profiles/abc"


# ---------------------------------------------------------------------------
# Key sanitization
# ---------------------------------------------------------------------------


def test_sanitize_key_replaces_slashes_with_double_underscore() -> None:
    """Slashes in keys would collide with filesystem path separators.

    Fails if _sanitize_key passes them through or uses a different
    replacement.
    """
    assert _sanitize_key("profiles/abc") == "profiles__abc"
    assert _sanitize_key("ledger/_lifecycle") == "ledger___lifecycle"


# ---------------------------------------------------------------------------
# Acquire / release semantics
# ---------------------------------------------------------------------------


def test_context_manager_releases_on_exit() -> None:
    """`with lock as token: ...` must release so the next acquire succeeds."""
    registry: dict[str, dict[str, float | str]] = {}
    clock = FakeClock(start=0.0)
    with InMemoryLock(key="k", ttl_s=5.0, registry=registry, clock=clock) as token:
        assert token.key == "k"
    # Second acquire must succeed immediately.
    second = InMemoryLock(key="k", ttl_s=5.0, registry=registry, clock=clock)
    assert second.acquire(blocking=False) is not None


def test_nonblocking_acquire_returns_none_on_contention() -> None:
    """blocking=False must NOT spin; it returns None when held by another."""
    registry: dict[str, dict[str, float | str]] = {}
    clock = FakeClock(start=0.0)
    holder = InMemoryLock(key="k", ttl_s=10.0, registry=registry, clock=clock)
    holder.acquire()
    contender = InMemoryLock(key="k", ttl_s=10.0, registry=registry, clock=clock)
    assert contender.acquire(blocking=False) is None


def test_blocking_with_timeout_raises_when_elapsed(monkeypatch: MonkeyPatch) -> None:
    """blocking=True, timeout_s=X must raise LockTimeout when X elapses.

    The fake clock advances on each poll so the test does not sleep.
    """
    registry: dict[str, dict[str, float | str]] = {}
    clock = FakeClock(start=0.0)
    holder = InMemoryLock(key="k", ttl_s=100.0, registry=registry, clock=clock)
    holder.acquire()
    contender = InMemoryLock(
        key="k",
        ttl_s=100.0,
        registry=registry,
        clock=clock,
        sleep=lambda _: clock.advance(0.6),
        poll_interval_s=0.5,
    )
    with pytest.raises(LockTimeout):
        contender.acquire(blocking=True, timeout_s=1.0)


def test_expired_lease_can_be_stolen() -> None:
    """A leader that holds past TTL must lose to a fresh acquirer.

    Fails if InMemoryLock ignores expires_at on the acquire path.
    """
    registry: dict[str, dict[str, float | str]] = {}
    clock = FakeClock(start=0.0)
    holder = InMemoryLock(key="k", ttl_s=5.0, registry=registry, clock=clock)
    holder.acquire()
    clock.advance(10.0)
    stealer = InMemoryLock(key="k", ttl_s=5.0, registry=registry, clock=clock)
    token = stealer.acquire(blocking=False)
    assert token is not None
    assert token.key == "k"


def test_release_after_steal_is_silent() -> None:
    """Original holder calling release after lease was stolen must not raise.

    Best-effort semantics — the work is already irrevocable; raising would
    surprise callers.
    """
    registry: dict[str, dict[str, float | str]] = {}
    clock = FakeClock(start=0.0)
    holder = InMemoryLock(key="k", ttl_s=5.0, registry=registry, clock=clock)
    token = holder.acquire()
    assert token is not None
    clock.advance(10.0)
    InMemoryLock(key="k", ttl_s=5.0, registry=registry, clock=clock).acquire()
    # Must NOT raise.
    holder.release(token)
