"""PodLockRegistry — per-pod_id threading.Lock serialization."""

from __future__ import annotations

import threading
import time

from kinoforge.core.warm_reuse.pod_lock import PodLockRegistry


def test_acquire_once_then_blocks() -> None:
    """Second non-blocking acquire on the same pod returns False.

    Bug: registry uses RLock instead of Lock, allowing recursive
    acquire from the same thread → defeats serialization semantic.
    """
    reg = PodLockRegistry()
    assert reg.acquire("pod-a", blocking=False) is True
    assert reg.acquire("pod-a", blocking=False) is False


def test_release_lets_next_acquire_succeed() -> None:
    """Bug: registry leaks held state after release."""
    reg = PodLockRegistry()
    reg.acquire("pod-a", blocking=False)
    reg.release("pod-a")
    assert reg.acquire("pod-a", blocking=False) is True


def test_membership_reflects_held_state() -> None:
    """Bug: __contains__ checks registry key presence instead of lock-held state."""
    reg = PodLockRegistry()
    assert "pod-a" not in reg
    reg.acquire("pod-a", blocking=False)
    assert "pod-a" in reg
    reg.release("pod-a")
    assert "pod-a" not in reg


def test_different_pods_acquire_independently() -> None:
    """Bug: registry uses a single shared lock instead of per-pod locks."""
    reg = PodLockRegistry()
    assert reg.acquire("pod-a", blocking=False) is True
    assert reg.acquire("pod-b", blocking=False) is True


def test_blocking_acquire_with_timeout_returns_false_on_timeout() -> None:
    """Bug: timeout=0 / negative inverted; or timeout argument ignored entirely."""
    reg = PodLockRegistry()
    reg.acquire("pod-a", blocking=False)
    start = time.monotonic()
    got = reg.acquire("pod-a", blocking=True, timeout=0.1)
    elapsed = time.monotonic() - start
    assert got is False
    assert 0.08 < elapsed < 0.5, "timeout should approximate 0.1s"


def test_thread_death_releases_lock() -> None:
    """A thread that acquires then dies must release the lock implicitly.

    Bug: registry uses a non-threading.Lock primitive that doesn't release
    on thread exit, so a crashed worker leaves the pod permanently locked.
    """
    reg = PodLockRegistry()

    def _worker() -> None:
        reg.acquire("pod-a", blocking=False)
        # No release — simulating crash. threading.Lock releases on thread death.

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=1.0)
    assert not t.is_alive(), "test guard — worker should have exited"
    assert reg.acquire("pod-a", blocking=True, timeout=0.5) is True
