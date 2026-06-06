"""Google Cloud Storage-backed ArtifactStore.

Self-registers under ``"gcs"`` on import.  Default zero-arg factory reads
``KINOFORGE_GCS_BUCKET`` (+ optional ``KINOFORGE_GCS_PREFIX``); library users
construct ``GCSArtifactStore(bucket=..., prefix=..., client=..., not_found_exc=...)``
directly.

Both the SDK client AND the ``NotFound`` exception class are injectable so
tests pass *both* parameters to bypass the two lazy-import gates inside
``__init__``.
"""

from __future__ import annotations

import io
import json
import os
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from google.api_core.retry import Retry

from kinoforge.core.interfaces import Artifact
from kinoforge.stores.base import ArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.config import StoreConfig
    from kinoforge.core.locks import Lock

# ---------------------------------------------------------------------------
# Module-scope retry baseline (Layer W T4).
# Passed as ``retry=`` on every SDK read + write call so that transient 5xx /
# connection errors are retried with exponential back-off without relying on
# the caller to configure retry policy.
# ---------------------------------------------------------------------------

_GCS_RETRY = Retry(initial=0.1, maximum=2.0, multiplier=2.0, deadline=30.0)


class GCSArtifactStore(ArtifactStore):
    """ArtifactStore backed by Google Cloud Storage.

    Storage layout: ``gs://<bucket>/<prefix>/<run_id>/<name>``.

    Attributes:
        bucket: Target GCS bucket name.
        prefix: Optional key prefix; normalised to have no leading or trailing slash.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,  # noqa: ANN401 — injected SDK client; typed Any to avoid SDK import in signature
        not_found_exc: type[BaseException] | None = None,
        cfg: StoreConfig | None = None,
    ) -> None:
        """Initialise the store.

        Args:
            bucket: Target GCS bucket name.
            prefix: Optional key prefix.  Leading and trailing slashes are stripped.
            client: Optional ``google.cloud.storage.Client``.  When ``None``,
                a real client is lazily constructed (uses gcloud ADC).
            not_found_exc: Optional exception class to catch as "missing key".
                When ``None``, lazily imports ``google.api_core.exceptions.NotFound``.
                Tests must pass both ``client`` AND ``not_found_exc`` to bypass
                both lazy-import gates.
            cfg: Optional :class:`~kinoforge.core.config.StoreConfig` carrying
                encryption settings.  When ``None``, defaults are used
                (provider-managed encryption, no KMS).
        """
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import google.cloud.storage as _gcs_storage  # type: ignore[import-untyped]  # noqa: PLC0415 — lazy: tests inject a fake and never trip this

            client = _gcs_storage.Client()
        if not_found_exc is None:
            import google.api_core.exceptions as _gax_exc  # noqa: PLC0415 — lazy: tests inject the exception class and never trip this

            not_found_exc = _gax_exc.NotFound
        self._client: Any = client
        self._bucket_handle: Any = client.bucket(bucket)
        self._not_found_exc: type[BaseException] = not_found_exc
        # Lazy import to avoid forcing config module into every import path.
        if cfg is None:
            from kinoforge.core.config import (
                StoreConfig as _StoreConfig,  # noqa: PLC0415
            )

            cfg = _StoreConfig(kind="gcs", bucket=bucket)
        self._cfg: StoreConfig = cfg

    def _key(self, run_id: str, name: str) -> str:
        """Return the absolute object key for ``(run_id, name)``."""
        parts = [p for p in (self.prefix, run_id, name) if p]
        return "/".join(parts)

    @staticmethod
    def _split_uri(uri: str) -> tuple[str, str]:
        """Split a ``gs://bucket/key`` URI into ``(bucket, key)``."""
        if not uri.startswith("gs://"):
            raise ValueError(f"not a gs:// uri: {uri!r}")
        bucket, _, key = uri[len("gs://") :].partition("/")
        return bucket, key

    # ------------------------------------------------------------------
    # ArtifactStore implementation
    # ------------------------------------------------------------------

    def uri_for(self, run_id: str, name: str) -> str:
        """Return the GCS URI for ``(run_id, name)`` — pure, no I/O."""
        return f"gs://{self.bucket}/{self._key(run_id, name)}"

    def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact:
        """Write ``data`` under ``<bucket>/<prefix>/<run_id>/<name>`` via resumable upload.

        Resumable uploads (via ``upload_from_file``) are used in preference to
        ``upload_from_string`` because the GCS SDK automatically switches to a
        resumable multi-part upload above ~5 MiB, matching the behaviour of
        ``upload_fileobj`` on the S3 side.

        If ``cfg.encryption.mode == "kms"``, ``blob.kms_key_name`` is set
        **before** the upload so the SDK routes the write through the caller's
        Cloud KMS key.
        """
        key = self._key(run_id, name)
        blob = self._bucket_handle.blob(key)
        enc = self._cfg.encryption
        if enc.mode == "kms":
            if (
                enc.kms_key_id is None
            ):  # pragma: no cover — guaranteed by pydantic validator
                raise RuntimeError(
                    "encryption.mode='kms' requires kms_key_id; pydantic should have caught this"
                )
            blob.kms_key_name = enc.kms_key_id
        blob.upload_from_file(io.BytesIO(data), retry=_GCS_RETRY)
        return Artifact(uri=f"gs://{self.bucket}/{key}")

    def get_bytes(self, uri: str) -> bytes:
        """Read the bytes at ``uri``; raise FileNotFoundError on miss."""
        _, key = self._split_uri(uri)
        try:
            return self._bucket_handle.blob(key).download_as_bytes(retry=_GCS_RETRY)  # type: ignore[no-any-return]
        except self._not_found_exc:
            raise FileNotFoundError(f"artifact not found: {uri!r}") from None

    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact:  # type: ignore[type-arg]
        """Serialise ``obj`` as UTF-8 JSON and persist under ``<run_id>/<name>``."""
        return self.put_bytes(run_id, name, json.dumps(obj).encode("utf-8"))

    def get_json(self, uri: str) -> dict:  # type: ignore[type-arg]
        """Deserialise and return the JSON object stored at ``uri``."""
        return json.loads(self.get_bytes(uri).decode("utf-8"))  # type: ignore[no-any-return]

    def list(self, run_id: str) -> list[str]:
        """Enumerate names stored under ``run_id`` (relative to ``<prefix>/<run_id>/``)."""
        run_prefix = self._key(run_id, "") + "/"
        return [
            blob.name[len(run_prefix) :]
            for blob in self._bucket_handle.list_blobs(
                prefix=run_prefix, retry=_GCS_RETRY
            )
        ]

    def delete(self, uri: str) -> None:
        """Remove the object at ``uri``; raise FileNotFoundError on miss."""
        _, key = self._split_uri(uri)
        try:
            self._bucket_handle.blob(key).delete(retry=_GCS_RETRY)
        except self._not_found_exc:
            raise FileNotFoundError(f"artifact not found: {uri!r}") from None

    def acquire_lock(self, key: str, *, ttl_s: float) -> Lock:
        """Return a :class:`GCSCloudLock` rooted under ``<prefix>/_locks/``.

        Args:
            key: Logical lock key (may contain forward slashes).
            ttl_s: Lease duration in seconds.

        Returns:
            A fresh :class:`~kinoforge.stores.gcs.lock.GCSCloudLock`.
        """
        import kinoforge.stores.gcs.lock as _gcs_lock  # noqa: PLC0415

        return _gcs_lock.GCSCloudLock(store=self, key=key, ttl_s=ttl_s)

    def signed_url(
        self,
        run_id: str,
        name: str,
        *,
        op: Literal["GET", "PUT"],
        ttl_s: int,
    ) -> str:
        """Return a v4 signed URL granting a single GET or PUT on the artifact.

        Args:
            run_id: Run namespace.
            name: Artifact name within the run.
            op: HTTP method granted by the URL (``"GET"`` or ``"PUT"``).
            ttl_s: Validity window in seconds from issuance.

        Returns:
            Absolute HTTPS URL valid for ``ttl_s`` seconds.
        """
        key = self._key(run_id, name)
        blob = self._bucket_handle.blob(key)
        return blob.generate_signed_url(  # type: ignore[no-any-return]
            version="v4",
            expiration=timedelta(seconds=ttl_s),
            method=op,
        )


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

from kinoforge.core.registry import register_store  # noqa: E402


def _default_factory() -> GCSArtifactStore:
    """Zero-arg factory reading bucket + prefix from env."""
    bucket = os.environ.get("KINOFORGE_GCS_BUCKET")
    if not bucket:
        raise RuntimeError(
            "GCSArtifactStore default factory needs KINOFORGE_GCS_BUCKET; "
            "either set the env var or construct GCSArtifactStore(bucket=...) directly."
        )
    return GCSArtifactStore(
        bucket=bucket, prefix=os.environ.get("KINOFORGE_GCS_PREFIX", "")
    )


register_store("gcs", _default_factory)
