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

import json
import os
from typing import TYPE_CHECKING, Any

from kinoforge.core.interfaces import Artifact
from kinoforge.stores.base import ArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.locks import Lock


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
        """
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import google.cloud.storage as _gcs_storage  # type: ignore[import-untyped]  # noqa: PLC0415 — lazy: tests inject a fake and never trip this

            client = _gcs_storage.Client()
        if not_found_exc is None:
            import google.api_core.exceptions as _gax_exc  # noqa: PLC0415 — lazy: tests inject the exception class and never trip this

            not_found_exc = _gax_exc.NotFound
        self._bucket_handle: Any = client.bucket(bucket)
        self._not_found_exc: type[BaseException] = not_found_exc

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
        """Write ``data`` under ``<bucket>/<prefix>/<run_id>/<name>`` (auto-multipart over SDK threshold)."""
        key = self._key(run_id, name)
        self._bucket_handle.blob(key).upload_from_string(data)
        return Artifact(uri=f"gs://{self.bucket}/{key}")

    def get_bytes(self, uri: str) -> bytes:
        """Read the bytes at ``uri``; raise FileNotFoundError on miss."""
        _, key = self._split_uri(uri)
        try:
            return self._bucket_handle.blob(key).download_as_bytes()  # type: ignore[no-any-return]
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
            for blob in self._bucket_handle.list_blobs(prefix=run_prefix)
        ]

    def delete(self, uri: str) -> None:
        """Remove the object at ``uri``; raise FileNotFoundError on miss."""
        _, key = self._split_uri(uri)
        try:
            self._bucket_handle.blob(key).delete()
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
