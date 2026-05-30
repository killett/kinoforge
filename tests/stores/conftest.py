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

    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
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
        self._objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]:
        self._objects[(Bucket, Key)] = Body
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self._objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": _BytesBody(self._objects[(Bucket, Key)])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self._objects:
            raise self.exceptions.ClientError("NoSuchKey")
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._objects.pop((Bucket, Key), None)
        return {}

    def get_paginator(self, op: str) -> _S3Paginator:
        assert op == "list_objects_v2", f"unexpected paginator op: {op!r}"
        return _S3Paginator(self._objects)


# ---------------------------------------------------------------------------
# GCS fakes (used by Task 2's tests; landed here in Task 1 to avoid two
# conftest.py edits)
# ---------------------------------------------------------------------------


class _GCSNotFound(Exception):
    """Stand-in for google.api_core.exceptions.NotFound."""


class _FakeBlob:
    def __init__(self, bucket: _FakeBucket, name: str) -> None:
        self.bucket = bucket
        self.name = name

    def upload_from_string(self, data: bytes) -> None:
        self.bucket._blobs[self.name] = data

    def download_as_bytes(self) -> bytes:
        if self.name not in self.bucket._blobs:
            raise _GCSNotFound()
        return self.bucket._blobs[self.name]

    def delete(self) -> None:
        if self.name not in self.bucket._blobs:
            raise _GCSNotFound()
        del self.bucket._blobs[self.name]

    def exists(self) -> bool:
        return self.name in self.bucket._blobs


class _FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self._blobs: dict[str, bytes] = {}

    def blob(self, key: str) -> _FakeBlob:
        return _FakeBlob(self, key)

    def list_blobs(self, *, prefix: str) -> Iterator[_FakeBlob]:
        for k in sorted(self._blobs):
            if k.startswith(prefix):
                yield _FakeBlob(self, k)


class FakeGCSClient:
    """In-memory stand-in for google.cloud.storage.Client."""

    NotFound = _GCSNotFound

    def __init__(self) -> None:
        self._buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return self._buckets.setdefault(name, _FakeBucket(name))
