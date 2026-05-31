"""Amazon S3-backed ArtifactStore.

Self-registers under ``"s3"`` on import via the store registry.  The default
zero-arg factory reads ``KINOFORGE_S3_BUCKET`` (+ optional
``KINOFORGE_S3_PREFIX``) from the environment; library users wanting full
control construct ``S3ArtifactStore(bucket=..., prefix=..., client=...)``
directly.

The ``client`` parameter is injected by tests so the lazy ``import boto3``
inside ``__init__`` never fires under the test path.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from kinoforge.core.interfaces import Artifact
from kinoforge.stores.base import ArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.locks import Lock


class S3ArtifactStore(ArtifactStore):
    """ArtifactStore backed by S3.

    Storage layout: ``s3://<bucket>/<prefix>/<run_id>/<name>``.

    Attributes:
        bucket: Target S3 bucket name.
        prefix: Optional key prefix; normalised to have no leading or trailing slash.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,  # noqa: ANN401 — injected SDK client; typed Any to avoid SDK import in signature
    ) -> None:
        """Initialise the store.

        Args:
            bucket: Target S3 bucket name.
            prefix: Optional key prefix.  Leading and trailing slashes are stripped.
            client: Optional boto3 S3 client.  When ``None``, a real client is
                lazily constructed via ``boto3.client("s3")`` (uses the SDK
                default credential chain).
        """
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3  # type: ignore[import-untyped]  # noqa: PLC0415 — lazy: tests inject a fake and never trip this

            client = boto3.client("s3")
        self._client: Any = client

    def _key(self, run_id: str, name: str) -> str:
        """Return the absolute object key for ``(run_id, name)``."""
        parts = [p for p in (self.prefix, run_id, name) if p]
        return "/".join(parts)

    @staticmethod
    def _split_uri(uri: str) -> tuple[str, str]:
        """Split an ``s3://bucket/key`` URI into ``(bucket, key)``."""
        if not uri.startswith("s3://"):
            raise ValueError(f"not an s3:// uri: {uri!r}")
        bucket, _, key = uri[len("s3://") :].partition("/")
        return bucket, key

    # ------------------------------------------------------------------
    # ArtifactStore implementation
    # ------------------------------------------------------------------

    def uri_for(self, run_id: str, name: str) -> str:
        """Return the S3 URI for ``(run_id, name)`` — pure, no I/O."""
        return f"s3://{self.bucket}/{self._key(run_id, name)}"

    def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact:
        """Write ``data`` under ``<bucket>/<prefix>/<run_id>/<name>``."""
        key = self._key(run_id, name)
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return Artifact(uri=f"s3://{self.bucket}/{key}")

    def get_bytes(self, uri: str) -> bytes:
        """Read the bytes at ``uri``; raise FileNotFoundError on miss."""
        bucket, key = self._split_uri(uri)
        try:
            resp = self._client.get_object(Bucket=bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            raise FileNotFoundError(f"artifact not found: {uri!r}") from None
        return resp["Body"].read()  # type: ignore[no-any-return]

    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact:  # type: ignore[type-arg]
        """Serialise ``obj`` as UTF-8 JSON and persist under ``<run_id>/<name>``."""
        return self.put_bytes(run_id, name, json.dumps(obj).encode("utf-8"))

    def get_json(self, uri: str) -> dict:  # type: ignore[type-arg]
        """Deserialise and return the JSON object stored at ``uri``."""
        return json.loads(self.get_bytes(uri).decode("utf-8"))  # type: ignore[no-any-return]

    def list(self, run_id: str) -> list[str]:
        """Enumerate names stored under ``run_id`` (relative to ``<prefix>/<run_id>/``)."""
        run_prefix = self._key(run_id, "") + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        names: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=run_prefix):
            for obj in page.get("Contents", []):
                names.append(obj["Key"][len(run_prefix) :])
        return names

    def delete(self, uri: str) -> None:
        """Remove the object at ``uri``; raise FileNotFoundError on miss."""
        bucket, key = self._split_uri(uri)
        try:
            self._client.head_object(Bucket=bucket, Key=key)
        except self._client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(f"artifact not found: {uri!r}") from None
            raise
        self._client.delete_object(Bucket=bucket, Key=key)

    def acquire_lock(self, key: str, *, ttl_s: float) -> Lock:
        """Return an :class:`S3CloudLock` rooted under ``<prefix>/_locks/``.

        Args:
            key: Logical lock key (may contain forward slashes).
            ttl_s: Lease duration in seconds.

        Returns:
            A fresh :class:`~kinoforge.stores.s3.lock.S3CloudLock`.
        """
        import kinoforge.stores.s3.lock as _s3_lock  # noqa: PLC0415

        return _s3_lock.S3CloudLock(store=self, key=key, ttl_s=ttl_s)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

from kinoforge.core.registry import register_store  # noqa: E402


def _default_factory() -> S3ArtifactStore:
    """Zero-arg factory reading bucket + prefix from env."""
    bucket = os.environ.get("KINOFORGE_S3_BUCKET")
    if not bucket:
        raise RuntimeError(
            "S3ArtifactStore default factory needs KINOFORGE_S3_BUCKET; "
            "either set the env var or construct S3ArtifactStore(bucket=...) directly."
        )
    return S3ArtifactStore(
        bucket=bucket, prefix=os.environ.get("KINOFORGE_S3_PREFIX", "")
    )


register_store("s3", _default_factory)
