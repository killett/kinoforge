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


def test_local_store_stub_raises_not_implemented(tmp_path: Path) -> None:
    """Local store stub must raise NotImplementedError mentioning Task 3.

    The Task-number reference is the load-bearing part: it tells future
    readers (and Task 3's reviewer) which task replaces the stub.
    """
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(NotImplementedError, match="Layer H Task 3"):
        store.acquire_lock("k", ttl_s=10.0)


def test_s3_store_stub_raises_not_implemented() -> None:
    """S3 store stub must raise NotImplementedError mentioning Task 4."""
    from kinoforge.stores.s3 import S3ArtifactStore
    from tests.stores.conftest import FakeS3Client

    store = S3ArtifactStore(bucket="b", client=FakeS3Client())
    with pytest.raises(NotImplementedError, match="Layer H Task 4"):
        store.acquire_lock("k", ttl_s=10.0)


def test_gcs_store_stub_raises_not_implemented() -> None:
    """GCS store stub must raise NotImplementedError mentioning Task 5."""
    from kinoforge.stores.gcs import GCSArtifactStore
    from tests.stores.conftest import FakeGCSClient

    store = GCSArtifactStore(
        bucket="b",
        client=FakeGCSClient(),
        not_found_exc=FakeGCSClient.NotFound,
    )
    with pytest.raises(NotImplementedError, match="Layer H Task 5"):
        store.acquire_lock("k", ttl_s=10.0)


def test_acquire_lock_signature_has_ttl_kwonly() -> None:
    """ttl_s must be keyword-only to prevent positional misuse.

    Fails if the spec evolves to drop the ``*`` marker.
    """
    import inspect

    from kinoforge.stores.base import ArtifactStore

    sig = inspect.signature(ArtifactStore.acquire_lock)
    ttl = sig.parameters["ttl_s"]
    assert ttl.kind is inspect.Parameter.KEYWORD_ONLY
