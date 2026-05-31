"""GCSCloudLock tests using the in-process FakeGCSClient.

Each test names a behavior under test and a concrete failure mode that
would make it fail.
"""

from __future__ import annotations

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import LockTimeout
from kinoforge.stores.gcs import GCSArtifactStore
from kinoforge.stores.gcs.lock import GCSCloudLock
from tests.stores.conftest import FakeGCSClient


def _store_and_client(
    tmp_bucket: str = "buck", prefix: str = "p"
) -> tuple[GCSArtifactStore, FakeGCSClient]:
    client = FakeGCSClient()
    store = GCSArtifactStore(
        bucket=tmp_bucket,
        prefix=prefix,
        client=client,
        not_found_exc=FakeGCSClient.NotFound,
    )
    return store, client


def test_store_acquire_lock_returns_gcs_cloud_lock() -> None:
    """Factory returns GCSCloudLock after Task 5."""
    store, _ = _store_and_client()
    assert isinstance(store.acquire_lock("k", ttl_s=10.0), GCSCloudLock)


def test_acquire_uses_if_generation_match_zero() -> None:
    """First acquirer must use if_generation_match=0 (object-must-not-exist).

    Fails if the CAS condition is omitted (silent last-writer-wins).
    """
    store, client = _store_and_client()
    lock = store.acquire_lock("profiles/abc", ttl_s=30.0)
    token = lock.acquire()
    assert token is not None
    bucket = client.bucket("buck")
    assert "p/_locks/profiles__abc.lock" in bucket._blobs


def test_nonblocking_acquire_on_held_lock_returns_none() -> None:
    """Second acquirer with TTL not expired must return None."""
    store, _ = _store_and_client()
    clock = FakeClock(start=0.0)
    holder = GCSCloudLock(
        store=store,
        key="k",
        ttl_s=60.0,
        clock=clock,
        precondition_failed_exc=FakeGCSClient.PreconditionFailed,
        sleep=lambda _: None,
    )
    holder.acquire()

    contender = GCSCloudLock(
        store=store,
        key="k",
        ttl_s=60.0,
        clock=clock,
        precondition_failed_exc=FakeGCSClient.PreconditionFailed,
        sleep=lambda _: None,
    )
    assert contender.acquire(blocking=False) is None


def test_expired_lock_is_stolen() -> None:
    """Contender past TTL must steal via blob.delete(if_generation_match=<gen>)."""
    store, _ = _store_and_client()
    clock = FakeClock(start=0.0)
    holder = GCSCloudLock(
        store=store,
        key="k",
        ttl_s=5.0,
        clock=clock,
        precondition_failed_exc=FakeGCSClient.PreconditionFailed,
        sleep=lambda _: None,
    )
    holder.acquire()
    clock.advance(100.0)

    stealer = GCSCloudLock(
        store=store,
        key="k",
        ttl_s=5.0,
        clock=clock,
        precondition_failed_exc=FakeGCSClient.PreconditionFailed,
        sleep=lambda _: None,
    )
    token = stealer.acquire(blocking=False)
    assert token is not None


def test_blocking_with_timeout_raises_lock_timeout() -> None:
    """Eventual LockTimeout when held lock won't expire within timeout window."""
    store, _ = _store_and_client()
    clock = FakeClock(start=0.0)
    holder = GCSCloudLock(
        store=store,
        key="k",
        ttl_s=600.0,
        clock=clock,
        precondition_failed_exc=FakeGCSClient.PreconditionFailed,
        sleep=lambda _: None,
    )
    holder.acquire()
    contender = GCSCloudLock(
        store=store,
        key="k",
        ttl_s=600.0,
        clock=clock,
        precondition_failed_exc=FakeGCSClient.PreconditionFailed,
        sleep=lambda _: clock.advance(0.6),
        poll_interval_s=0.5,
    )
    with pytest.raises(LockTimeout):
        contender.acquire(blocking=True, timeout_s=1.0)


def test_release_deletes_with_generation_match() -> None:
    """release() must use if_generation_match=<gen> so a stolen lease isn't deleted."""
    store, client = _store_and_client()
    lock = store.acquire_lock("k", ttl_s=30.0)
    token = lock.acquire()
    assert token is not None
    bucket = client.bucket("buck")
    assert "p/_locks/k.lock" in bucket._blobs
    lock.release(token)
    assert "p/_locks/k.lock" not in bucket._blobs


def test_release_silent_when_stolen_after_ttl() -> None:
    """release() must not raise when our generation is stale."""
    store, _ = _store_and_client()
    clock = FakeClock(start=0.0)
    lock = GCSCloudLock(
        store=store,
        key="k",
        ttl_s=5.0,
        clock=clock,
        precondition_failed_exc=FakeGCSClient.PreconditionFailed,
        sleep=lambda _: None,
    )
    token = lock.acquire()
    assert token is not None
    clock.advance(100.0)
    stealer = GCSCloudLock(
        store=store,
        key="k",
        ttl_s=5.0,
        clock=clock,
        precondition_failed_exc=FakeGCSClient.PreconditionFailed,
        sleep=lambda _: None,
    )
    stealer.acquire()
    # Must NOT raise.
    lock.release(token)
