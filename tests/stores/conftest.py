"""Shared in-memory test doubles for S3ArtifactStore + GCSArtifactStore tests.

Both fakes implement only the surface the stores actually call. Real cloud
SDKs are never imported; tests pass `client=fake` (and for GCS, also
`not_found_exc=Fake.NotFound`) to bypass the lazy-import gates in the store
constructors.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

# ---------------------------------------------------------------------------
# S3 fakes
# ---------------------------------------------------------------------------


class _NoSuchKeyError(Exception):
    """Stand-in for boto3.client('s3').exceptions.NoSuchKey."""


class _ClientErrorFake(Exception):
    """Stand-in for botocore.exceptions.ClientError.

    Carries a `response` dict shaped like boto3's so the store's error mapping
    code path is exercised exactly as the real SDK would trigger it.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _S3Exceptions:
    """Stand-in for the `.exceptions` namespace on a real boto3 S3 client."""

    NoSuchKey = _NoSuchKeyError
    ClientError = _ClientErrorFake


class _BytesBody:
    """Stand-in for the StreamingBody returned in get_object()['Body']."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _S3Paginator:
    """Stand-in for the paginator returned by client.get_paginator('list_objects_v2')."""

    def __init__(self, objects: dict[tuple[str, str], tuple[bytes, str]]) -> None:
        self._objects = objects

    def paginate(self, *, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:
        contents = [
            {"Key": k}
            for (b, k) in sorted(self._objects)
            if b == Bucket and k.startswith(Prefix)
        ]
        # Single page is sufficient for test workloads; real S3 paginates
        # beyond 1000 keys but S3ArtifactStore.list iterates the paginator
        # generically so the shape is enough.
        yield {"Contents": contents}


class FakeS3Client:
    """In-memory stand-in for boto3.client('s3') covering the S3ArtifactStore surface."""

    exceptions = _S3Exceptions()

    def __init__(self) -> None:
        # value = (body_bytes, etag)
        self._objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self._etag_counter = 0

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return f'"fake-etag-{self._etag_counter}"'

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        IfNoneMatch: str | None = None,
        IfMatch: str | None = None,
    ) -> dict[str, Any]:
        existing = self._objects.get((Bucket, Key))
        if IfNoneMatch == "*" and existing is not None:
            raise self.exceptions.ClientError("PreconditionFailed")
        if IfMatch is not None and (existing is None or existing[1] != IfMatch):
            raise self.exceptions.ClientError("PreconditionFailed")
        etag = self._next_etag()
        self._objects[(Bucket, Key)] = (Body, etag)
        return {"ETag": etag}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self._objects:
            raise self.exceptions.NoSuchKey()
        body, etag = self._objects[(Bucket, Key)]
        return {"Body": _BytesBody(body), "ETag": etag}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self._objects:
            raise self.exceptions.ClientError("NoSuchKey")
        _, etag = self._objects[(Bucket, Key)]
        return {"ETag": etag}

    def delete_object(
        self, *, Bucket: str, Key: str, IfMatch: str | None = None
    ) -> dict[str, Any]:
        existing = self._objects.get((Bucket, Key))
        if IfMatch is not None and (existing is None or existing[1] != IfMatch):
            raise self.exceptions.ClientError("PreconditionFailed")
        self._objects.pop((Bucket, Key), None)
        return {}

    def get_paginator(self, op: str) -> _S3Paginator:
        assert op == "list_objects_v2", f"unexpected paginator op: {op!r}"
        # Paginator only needs keys; pass through body+etag tuples.
        return _S3Paginator(self._objects)


# ---------------------------------------------------------------------------
# GCS fakes (used by Task 2's tests; landed here in Task 1 to avoid two
# conftest.py edits)
# ---------------------------------------------------------------------------


class _GCSNotFound(Exception):
    """Stand-in for google.api_core.exceptions.NotFound."""


class _GCSPreconditionFailed(Exception):
    """Stand-in for google.api_core.exceptions.PreconditionFailed."""


class _FakeBlob:
    def __init__(self, bucket: _FakeBucket, name: str) -> None:
        self.bucket = bucket
        self.name = name
        self._captured_generation: int | None = None

    @property
    def generation(self) -> int | None:
        """Return the current generation for this blob name from the bucket."""
        return self.bucket._generations.get(self.name)

    def upload_from_string(
        self, data: bytes, if_generation_match: int | None = None
    ) -> None:
        """Write data; honors if_generation_match precondition."""
        existing_gen = self.bucket._generations.get(self.name)
        if if_generation_match is not None:
            if if_generation_match == 0:
                if existing_gen is not None:
                    raise _GCSPreconditionFailed()
            else:
                if existing_gen != if_generation_match:
                    raise _GCSPreconditionFailed()
        self.bucket._blobs[self.name] = data
        new_gen = (existing_gen or 0) + 1
        self.bucket._generations[self.name] = new_gen
        self._captured_generation = new_gen

    def download_as_bytes(self) -> bytes:
        """Read bytes; captures current generation for subsequent release CAS."""
        if self.name not in self.bucket._blobs:
            raise _GCSNotFound()
        # Capture current generation so the lock can use it for conditional delete.
        self._captured_generation = self.bucket._generations.get(self.name)
        return self.bucket._blobs[self.name]

    def delete(self, if_generation_match: int | None = None) -> None:
        """Delete blob; honors if_generation_match precondition."""
        if self.name not in self.bucket._blobs:
            raise _GCSNotFound()
        existing_gen = self.bucket._generations.get(self.name)
        if if_generation_match is not None and existing_gen != if_generation_match:
            raise _GCSPreconditionFailed()
        del self.bucket._blobs[self.name]
        self.bucket._generations.pop(self.name, None)

    def exists(self) -> bool:
        return self.name in self.bucket._blobs


class _FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self._blobs: dict[str, bytes] = {}
        self._generations: dict[str, int] = {}

    def blob(self, key: str) -> _FakeBlob:
        return _FakeBlob(self, key)

    def list_blobs(self, *, prefix: str) -> Iterator[_FakeBlob]:
        for k in sorted(self._blobs):
            if k.startswith(prefix):
                yield _FakeBlob(self, k)


class FakeGCSClient:
    """In-memory stand-in for google.cloud.storage.Client."""

    NotFound = _GCSNotFound
    PreconditionFailed = _GCSPreconditionFailed

    def __init__(self) -> None:
        self._buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return self._buckets.setdefault(name, _FakeBucket(name))
