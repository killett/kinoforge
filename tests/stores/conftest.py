"""Shared in-memory test doubles for S3ArtifactStore + GCSArtifactStore tests.

Both fakes implement only the surface the stores actually call. Real cloud
SDKs are never imported; tests pass `client=fake` (and for GCS, also
`not_found_exc=Fake.NotFound`) to bypass the lazy-import gates in the store
constructors.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

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
        # value = (body_bytes, etag)
        self._objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self._etag_counter = 0
        self.upload_fileobj_calls: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.generate_presigned_url_calls: list[tuple[str, dict[str, str], int]] = []
        self.meta = SimpleNamespace(
            config=SimpleNamespace(
                retries={"max_attempts": 0, "mode": "legacy"},
            ),
        )

    def set_retry_config(self, retries: dict[str, Any]) -> None:
        """Mirror what botocore.config.Config does at construction time."""
        self.meta.config.retries = retries

    def upload_fileobj(
        self,
        fileobj: Any,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        ExtraArgs: dict[str, str] | None = None,  # noqa: N803
    ) -> None:
        """Capture an upload_fileobj call and mirror into the in-memory object map."""
        body = fileobj.read()
        self.upload_fileobj_calls.append((Bucket, Key, body, dict(ExtraArgs or {})))
        etag = self._next_etag()
        self._objects[(Bucket, Key)] = (body, etag)

    def generate_presigned_url(
        self,
        op: str,
        *,
        Params: dict[str, str],  # noqa: N803
        ExpiresIn: int,  # noqa: N803
    ) -> str:
        """Capture a generate_presigned_url call and return a deterministic fake URL."""
        self.generate_presigned_url_calls.append((op, dict(Params), ExpiresIn))
        return (
            f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}"
            f"?X-Sig=fake&ttl={ExpiresIn}"
        )

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
        # Paginator only needs keys; pass through the key set.
        return _S3Paginator({k: v[0] for k, v in self._objects.items()})


# ---------------------------------------------------------------------------
# GCS fakes
# ---------------------------------------------------------------------------


class _GCSNotFound(Exception):
    """Stand-in for google.api_core.exceptions.NotFound."""


class _GCSPreconditionFailed(Exception):
    """Stand-in for google.api_core.exceptions.PreconditionFailed."""


class _FakeBlob:
    """Unified fake GCS blob supporting both lock (generation-CAS) and T4 (retry) paths."""

    def __init__(self, bucket: _FakeBucket, name: str) -> None:
        self.bucket = bucket
        self.name = name
        self._captured_generation: int | None = None
        # T4 attributes -------------------------------------------------------
        self.kms_key_name: str | None = None
        self.upload_from_file_calls: list[tuple[bytes, object]] = []
        self.download_as_bytes_calls: list[object] = []
        self.delete_calls: list[object] = []
        self.generate_signed_url_calls: list[dict[str, object]] = []

    @property
    def generation(self) -> int | None:
        return self.bucket._generations.get(self.name)

    # --- Original lock path (generation-CAS) ----------------------------------

    def upload_from_string(
        self, data: bytes, if_generation_match: int | None = None
    ) -> None:
        existing_gen = self.bucket._generations.get(self.name)
        if if_generation_match is not None:
            if if_generation_match == 0:
                if existing_gen is not None:
                    raise _GCSPreconditionFailed()
            else:
                if existing_gen != if_generation_match:
                    raise _GCSPreconditionFailed()
        self.bucket._blobs[self.name] = self
        new_gen = (existing_gen or 0) + 1
        self.bucket._generations[self.name] = new_gen
        self._captured_generation = new_gen
        # Mirror body so download_as_bytes can return it.
        self._body = data

    # --- T4 resumable-upload path ---------------------------------------------

    def upload_from_file(self, fileobj: Any, *, retry: object = None) -> None:
        body = fileobj.read()
        self._body = body
        self.upload_from_file_calls.append((body, retry))
        self.bucket._blobs[self.name] = self
        existing_gen = self.bucket._generations.get(self.name)
        new_gen = (existing_gen or 0) + 1
        self.bucket._generations[self.name] = new_gen
        self._captured_generation = new_gen

    # --- Read path ------------------------------------------------------------

    def download_as_bytes(self, *, retry: object = None) -> bytes:
        if self.name not in self.bucket._blobs:
            raise _GCSNotFound()
        self.download_as_bytes_calls.append(retry)
        # Capture current generation for release CAS.
        self._captured_generation = self.bucket._generations.get(self.name)
        blob_entry = self.bucket._blobs[self.name]
        # _blobs may store _FakeBlob instances (new path) or raw bytes (legacy).
        if isinstance(blob_entry, _FakeBlob):
            return blob_entry._body
        return blob_entry

    # --- Delete path ----------------------------------------------------------

    def delete(
        self,
        if_generation_match: int | None = None,
        *,
        retry: object = None,
    ) -> None:
        if self.name not in self.bucket._blobs:
            raise _GCSNotFound()
        existing_gen = self.bucket._generations.get(self.name)
        if if_generation_match is not None and existing_gen != if_generation_match:
            raise _GCSPreconditionFailed()
        self.delete_calls.append(retry)
        del self.bucket._blobs[self.name]
        self.bucket._generations.pop(self.name, None)

    # --- Signed-URL path ------------------------------------------------------

    def generate_signed_url(
        self, *, version: str, expiration: object, method: str
    ) -> str:
        call: dict[str, object] = {
            "version": version,
            "expiration": expiration,
            "method": method,
        }
        self.generate_signed_url_calls.append(call)
        return (
            f"https://storage.googleapis.com/{self.bucket.name}/{self.name}"
            f"?X-Goog-Signature=fake&method={method}"
        )

    def reload(self) -> None:
        """No-op reload (real SDK re-fetches metadata)."""

    def exists(self) -> bool:
        return self.name in self.bucket._blobs


class _FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        # Values are _FakeBlob instances (or raw bytes for legacy paths).
        self._blobs: dict[str, Any] = {}
        self._generations: dict[str, int] = {}
        # Per-name blob cache so test mutations on .kms_key_name stick.
        self._blob_cache: dict[str, _FakeBlob] = {}

    def blob(self, key: str) -> _FakeBlob:
        """Return or create the cached blob instance for ``key``."""
        if key not in self._blob_cache:
            self._blob_cache[key] = _FakeBlob(self, key)
        return self._blob_cache[key]

    def list_blobs(self, *, prefix: str, retry: object = None) -> Iterator[_FakeBlob]:
        for k in sorted(self._blobs):
            if k.startswith(prefix):
                yield self.blob(k)


class FakeGCSClient:
    """In-memory stand-in for google.cloud.storage.Client."""

    NotFound = _GCSNotFound
    PreconditionFailed = _GCSPreconditionFailed

    def __init__(self) -> None:
        self._buckets: dict[str, _FakeBucket] = {}

    @property
    def buckets(self) -> dict[str, _FakeBucket]:
        """Public alias for ``_buckets`` used by T4 tests."""
        return self._buckets

    def bucket(self, name: str) -> _FakeBucket:
        return self._buckets.setdefault(name, _FakeBucket(name))


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_s3_client() -> FakeS3Client:
    """Fresh FakeS3Client for each test."""
    return FakeS3Client()


@pytest.fixture()
def fake_gcs_client() -> FakeGCSClient:
    """Fresh FakeGCSClient for each test."""
    return FakeGCSClient()
