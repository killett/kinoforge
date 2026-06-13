"""ABC-level contract for ArtifactStore.acquire_lock.

Verifies the factory method is declared abstract and that the temporary
NotImplementedError stubs on the three concrete stores are wired so
existing test suites keep passing between Tasks 2 and 5.

Import note: kinoforge.stores.base has a circular-import dependency that
prevents direct top-level import.  Importing a concrete store first (e.g.
LocalArtifactStore) bootstraps the module graph correctly; ArtifactStore is
then imported in each test function that needs it, after the graph is ready.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Must be imported before kinoforge.stores.base to avoid a circular import
# in kinoforge.core.__init__ → registry → stores.base.
from kinoforge.stores.local import LocalArtifactStore  # noqa: E402


def test_artifact_store_abc_declares_acquire_lock() -> None:
    """Removing acquire_lock from the ABC must regress this test."""
    from kinoforge.stores.base import ArtifactStore

    assert "acquire_lock" in ArtifactStore.__abstractmethods__


def test_partial_store_cannot_be_instantiated() -> None:
    """Subclass omitting acquire_lock must remain abstract.

    Fails if `acquire_lock` is added as a concrete method or removed from
    the ABC.
    """
    from kinoforge.stores.base import ArtifactStore

    class _PartialStore(ArtifactStore):
        def put_bytes(self, run_id, name, data):  # noqa: ANN001,ANN201,D102
            raise NotImplementedError

        def get_bytes(self, uri):  # noqa: ANN001,ANN201,D102
            raise NotImplementedError

        def put_json(self, run_id, name, obj):  # noqa: ANN001,ANN201,D102
            raise NotImplementedError

        def get_json(self, uri):  # noqa: ANN001,ANN201,D102
            raise NotImplementedError

        def list(self, run_id):  # noqa: ANN001,ANN201,D102
            raise NotImplementedError

        def delete(self, uri):  # noqa: ANN001,ANN201,D102
            raise NotImplementedError

        def uri_for(self, run_id, name):  # noqa: ANN001,ANN201,D102
            raise NotImplementedError

    with pytest.raises(TypeError, match="abstract"):
        _PartialStore()  # type: ignore[abstract]


def test_local_store_acquire_lock_returns_file_lock(tmp_path: Path) -> None:
    """Local store acquire_lock must return a real FileLock after Task 3."""
    from kinoforge.stores.local_lock import FileLock

    store = LocalArtifactStore(tmp_path)
    lock = store.acquire_lock("k", ttl_s=5.0)
    assert isinstance(lock, FileLock)


def test_s3_store_acquire_lock_returns_s3_cloud_lock() -> None:
    """S3 store acquire_lock must return a real S3CloudLock after Task 4."""
    from kinoforge.stores.s3 import S3ArtifactStore
    from kinoforge.stores.s3.lock import S3CloudLock
    from tests.stores.conftest import FakeS3Client

    store = S3ArtifactStore(bucket="b", client=FakeS3Client())
    lock = store.acquire_lock("k", ttl_s=10.0)
    assert isinstance(lock, S3CloudLock)


def test_gcs_store_acquire_lock_returns_gcs_cloud_lock() -> None:
    """GCS store acquire_lock must return a real GCSCloudLock after Task 5."""
    from kinoforge.stores.gcs import GCSArtifactStore
    from kinoforge.stores.gcs.lock import GCSCloudLock
    from tests.stores.conftest import FakeGCSClient

    store = GCSArtifactStore(
        bucket="b",
        client=FakeGCSClient(),
        not_found_exc=FakeGCSClient.NotFound,
    )
    lock = store.acquire_lock("k", ttl_s=10.0)
    assert isinstance(lock, GCSCloudLock)


def test_acquire_lock_signature_has_ttl_kwonly() -> None:
    """ttl_s must be keyword-only to prevent positional misuse.

    Fails if the spec evolves to drop the ``*`` marker.
    """
    import inspect

    from kinoforge.stores.base import ArtifactStore

    sig = inspect.signature(ArtifactStore.acquire_lock)
    ttl = sig.parameters["ttl_s"]
    assert ttl.kind is inspect.Parameter.KEYWORD_ONLY


def test_held_while_orchestrator_runs(tmp_path: Path) -> None:
    """B7: outer holder of provision:<id> blocks an inner non-blocking
    probe; after release, the probe succeeds.

    Pins the contract that ``LocalArtifactStore.acquire_lock`` returns a
    Lock whose ``acquire(blocking=False)`` returns ``None`` on contention
    and a non-None token after the holder releases. This is the exact
    pattern used by ``act_on_verdict``'s B7 probe.

    Cloud-store cross-host semantics deferred to the B16 neighborhood
    per spec §F5.
    """
    store = LocalArtifactStore(tmp_path)
    outer = store.acquire_lock("provision:i-contract", ttl_s=60.0)
    outer_token = outer.acquire(blocking=True, timeout_s=5.0)
    assert outer_token is not None

    inner = store.acquire_lock("provision:i-contract", ttl_s=60.0)
    inner_token = inner.acquire(blocking=False)
    assert inner_token is None, "non-blocking probe should fail while outer holds"

    outer.release(outer_token)

    inner_token_2 = inner.acquire(blocking=False)
    assert inner_token_2 is not None, (
        "non-blocking probe should succeed after outer releases"
    )
    inner.release(inner_token_2)
