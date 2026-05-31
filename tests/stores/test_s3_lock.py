"""S3CloudLock tests using the in-process FakeS3Client.

Each test names a behavior under test and a concrete failure mode that
would make it fail.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import LockTimeout
from kinoforge.stores.s3 import S3ArtifactStore
from kinoforge.stores.s3.lock import S3CloudLock
from tests.stores.conftest import FakeS3Client


def _store_and_client(
    tmp_bucket: str = "buck", prefix: str = "p"
) -> tuple[S3ArtifactStore, FakeS3Client]:
    client = FakeS3Client()
    store = S3ArtifactStore(bucket=tmp_bucket, prefix=prefix, client=client)
    return store, client


def test_store_acquire_lock_returns_s3_cloud_lock() -> None:
    """Factory returns the real S3CloudLock after Task 4."""
    store, _ = _store_and_client()
    assert isinstance(store.acquire_lock("k", ttl_s=10.0), S3CloudLock)


def test_acquire_writes_lock_object_with_if_none_match(monkeypatch: Any) -> None:
    """First acquirer must use IfNoneMatch='*' to prove the key was absent.

    Spying on put_object catches a regression where the CAS condition is
    dropped (would cause a silent last-writer-wins race in production).
    """
    store, client = _store_and_client()
    calls: list[dict[str, Any]] = []
    real_put = client.put_object

    def spy_put(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return real_put(**kwargs)

    client.put_object = spy_put  # type: ignore[method-assign]
    lock = store.acquire_lock("profiles/abc", ttl_s=30.0)
    token = lock.acquire()
    assert token is not None
    assert any(call.get("IfNoneMatch") == "*" for call in calls)
    # Lock object lives at <prefix>/_locks/<sanitized>.lock
    assert ("buck", "p/_locks/profiles__abc.lock") in client._objects


def test_nonblocking_acquire_on_held_lock_returns_none() -> None:
    """Second acquirer with TTL not expired must return None.

    Fails if the lock implementation doesn't catch PreconditionFailed.
    """
    store, _ = _store_and_client()
    clock = FakeClock(start=0.0)
    holder = S3CloudLock(
        store=store, key="k", ttl_s=60.0, clock=clock, sleep=lambda _: None
    )
    holder.acquire()

    contender = S3CloudLock(
        store=store, key="k", ttl_s=60.0, clock=clock, sleep=lambda _: None
    )
    assert contender.acquire(blocking=False) is None


def test_expired_lock_is_stolen() -> None:
    """A contender past TTL must steal via delete_object(IfMatch) + retry CAS PUT.

    Fails if the stealing path doesn't include the conditional delete.
    """
    store, _ = _store_and_client()
    clock = FakeClock(start=0.0)
    holder = S3CloudLock(
        store=store, key="k", ttl_s=5.0, clock=clock, sleep=lambda _: None
    )
    holder.acquire()
    clock.advance(100.0)

    stealer = S3CloudLock(
        store=store, key="k", ttl_s=5.0, clock=clock, sleep=lambda _: None
    )
    token = stealer.acquire(blocking=False)
    assert token is not None


def test_blocking_with_timeout_raises_lock_timeout() -> None:
    """Eventual LockTimeout when held lock won't expire within timeout window."""
    store, _ = _store_and_client()
    clock = FakeClock(start=0.0)
    holder = S3CloudLock(
        store=store, key="k", ttl_s=600.0, clock=clock, sleep=lambda _: None
    )
    holder.acquire()
    contender = S3CloudLock(
        store=store,
        key="k",
        ttl_s=600.0,
        clock=clock,
        sleep=lambda _: clock.advance(0.6),
        poll_interval_s=0.5,
    )
    with pytest.raises(LockTimeout):
        contender.acquire(blocking=True, timeout_s=1.0)


def test_release_deletes_with_if_match() -> None:
    """release() must use IfMatch=<etag> so a stolen lease isn't deleted."""
    store, client = _store_and_client()
    calls: list[dict[str, Any]] = []
    real_delete = client.delete_object

    def spy_delete(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return real_delete(**kwargs)

    client.delete_object = spy_delete  # type: ignore[method-assign]
    lock = store.acquire_lock("k", ttl_s=30.0)
    token = lock.acquire()
    assert token is not None
    lock.release(token)
    assert any("IfMatch" in c for c in calls)


def test_release_silent_when_stolen_after_ttl() -> None:
    """release() must not raise when our ETag no longer matches.

    Spec says best-effort: stolen leases produce silent releases.
    """
    store, _ = _store_and_client()
    clock = FakeClock(start=0.0)
    lock = S3CloudLock(
        store=store, key="k", ttl_s=5.0, clock=clock, sleep=lambda _: None
    )
    token = lock.acquire()
    assert token is not None
    clock.advance(100.0)
    stealer = S3CloudLock(
        store=store, key="k", ttl_s=5.0, clock=clock, sleep=lambda _: None
    )
    stealer.acquire()
    # Must NOT raise.
    lock.release(token)
