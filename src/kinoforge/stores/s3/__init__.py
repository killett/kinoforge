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

import io
import json
import os
from typing import TYPE_CHECKING, Any, Literal

from kinoforge.core.config import StoreConfig
from kinoforge.core.interfaces import Artifact
from kinoforge.stores.base import ArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.locks import Lock

_OP_TO_BOTOCORE: dict[str, str] = {"GET": "get_object", "PUT": "put_object"}


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
        cfg: StoreConfig | None = None,
    ) -> None:
        """Initialise the store.

        Args:
            bucket: Target S3 bucket name.
            prefix: Optional key prefix.  Leading and trailing slashes are stripped.
            client: Optional boto3 S3 client.  When ``None``, a real client is
                lazily constructed via ``boto3.client("s3")`` (uses the SDK
                default credential chain) with a pinned retry config.
            cfg: Optional :class:`~kinoforge.core.config.StoreConfig`.  When
                ``None``, a default config is constructed from ``bucket``.
        """
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._cfg = cfg if cfg is not None else StoreConfig(kind="s3", bucket=bucket)
        _retry_config = {"max_attempts": 3, "mode": "standard"}
        if client is None:
            import boto3  # noqa: PLC0415 — lazy: tests inject a fake and never trip this
            from botocore.config import (  # noqa: PLC0415
                Config as BotocoreConfig,
            )

            client = boto3.client("s3", config=BotocoreConfig(retries=_retry_config))
        else:
            # Stamp the retry config onto the injected fake so tests can assert it.
            if hasattr(client, "set_retry_config"):
                client.set_retry_config(_retry_config)
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
        """Write ``data`` under ``<bucket>/<prefix>/<run_id>/<name>``.

        Uses ``upload_fileobj`` for multipart-aware uploads.  Encryption
        ``ExtraArgs`` are derived from :attr:`_cfg`.encryption.
        """
        key = self._key(run_id, name)
        extra_args: dict[str, str] = {}
        enc = self._cfg.encryption
        if enc.mode == "kms":
            if (
                enc.kms_key_id is None
            ):  # pragma: no cover — pydantic validator prevents this
                raise ValueError("encryption.mode='kms' requires encryption.kms_key_id")
            extra_args["ServerSideEncryption"] = "aws:kms"
            extra_args["SSEKMSKeyId"] = enc.kms_key_id
        self._client.upload_fileobj(
            io.BytesIO(data),
            Bucket=self.bucket,
            Key=key,
            ExtraArgs=extra_args,
        )
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

    def delete_run(self, run_id: str) -> None:
        """Paginate-list then batch-delete every object under ``<run_id>/``.

        Idempotent: a missing prefix produces zero API calls. S3
        ``delete_objects`` accepts at most 1000 keys per request; this method
        chunks accordingly.

        Args:
            run_id: Run namespace whose prefix is wiped.
        """
        run_prefix = self._key(run_id, "") + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        batch: list[dict[str, str]] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=run_prefix):
            for obj in page.get("Contents", []):
                batch.append({"Key": obj["Key"]})
                if len(batch) == 1000:
                    self._client.delete_objects(
                        Bucket=self.bucket, Delete={"Objects": batch}
                    )
                    batch = []
        if batch:
            self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})

    def manual_cleanup_command(self, run_id: str) -> str:
        """Return ``aws s3 rm s3://<bucket>/<prefix><run_id>/ --recursive``."""
        run_prefix = self._key(run_id, "") + "/"
        return f"aws s3 rm s3://{self.bucket}/{run_prefix} --recursive"

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

    def signed_url(
        self,
        run_id: str,
        name: str,
        *,
        op: Literal["GET", "PUT"],
        ttl_s: int,
    ) -> str:
        """Return a pre-signed URL for a single GET or PUT on the artifact.

        Args:
            run_id: Run namespace.
            name: Artifact name within the run.
            op: HTTP method the URL grants.  ``"GET"`` downloads; ``"PUT"`` uploads.
            ttl_s: Validity window in seconds from issuance.

        Returns:
            Absolute HTTPS pre-signed URL valid for ``ttl_s`` seconds.
        """
        key = self._key(run_id, name)
        return self._client.generate_presigned_url(  # type: ignore[no-any-return]
            _OP_TO_BOTOCORE[op],
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl_s,
        )


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
