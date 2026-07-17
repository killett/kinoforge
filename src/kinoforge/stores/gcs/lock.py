"""GCS-backed Lock implementation using native ``if_generation_match=0``.

Layout: ``gs://<bucket>/<prefix>/_locks/<sanitized_key>.lock``.

GCS provides strong, single-region CAS semantics for ``if_generation_match``;
this adapter does not need the eventual-consistency dance that S3 used to
require pre-2020.

Stealing path mirrors S3CloudLock: when the CAS upload fails with
``PreconditionFailed``, GET the existing blob, parse ``expires_at``, and if
the TTL has elapsed conditional-delete on the captured generation then
retry.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from kinoforge.core.clock import Clock
from kinoforge.core.locks import LockToken, _sanitize_key
from kinoforge.stores._lease import _LeaseLockBase

if TYPE_CHECKING:
    from kinoforge.stores.gcs import GCSArtifactStore


class GCSCloudLock(_LeaseLockBase):
    """Lock backed by a GCS object created via if_generation_match=0.

    Args:
        store: Owning GCSArtifactStore.
        key: Logical lock key.
        ttl_s: Lease duration in seconds.
        clock: Wall-clock source.
        precondition_failed_exc: Exception class raised on CAS failure.
            When ``None``, lazily imports
            ``google.api_core.exceptions.PreconditionFailed``.  Tests must
            pass the class explicitly so the lazy import never fires.
        sleep: Injectable sleep callable.
        poll_interval_s: Seconds between blocking-acquire polls.
    """

    _blocking_none_message = "acquire() returned None in blocking mode"

    def __init__(
        self,
        *,
        store: GCSArtifactStore,
        key: str,
        ttl_s: float,
        clock: Clock | None = None,
        precondition_failed_exc: type[BaseException] | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_interval_s: float = 0.05,
    ) -> None:
        """Initialise the lock; see class-level docstring for parameter docs."""
        super().__init__(
            key=key,
            ttl_s=ttl_s,
            clock=clock,
            sleep=sleep,
            poll_interval_s=poll_interval_s,
        )
        self._store = store
        if precondition_failed_exc is None:
            import google.api_core.exceptions as _gax_exc  # noqa: PLC0415 — lazy: tests inject the class and never trip this

            precondition_failed_exc = _gax_exc.PreconditionFailed
        self._precondition_failed: type[BaseException] = precondition_failed_exc
        self._held_generation: int | None = None

    # ------------------------------------------------------------------
    # Storage layout
    # ------------------------------------------------------------------

    def _lock_key(self) -> str:
        """Return the GCS object key for the lock sidecar."""
        sanitized = _sanitize_key(self._key)
        parts = [p for p in (self._store.prefix, "_locks", f"{sanitized}.lock") if p]
        return "/".join(parts)

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def _try_take(self) -> LockToken | None:
        """One-shot non-blocking attempt; handles steal-after-TTL.

        Returns:
            A :class:`LockToken` on success, or ``None`` on contention.
        """
        bucket = self._store._bucket_handle
        blob = bucket.blob(self._lock_key())
        token = LockToken(key=self._key)
        payload = json.dumps(
            {"nonce": token.nonce, "expires_at": self._clock.now() + self._ttl_s}
        ).encode("utf-8")
        try:
            blob.upload_from_string(payload, if_generation_match=0)
        except self._precondition_failed:
            # Existing lock — check expiry to decide whether to steal.
            existing_blob = bucket.blob(self._lock_key())
            try:
                raw = existing_blob.download_as_bytes()
            except self._store._not_found_exc:
                # Race: blob gone between CAS fail and download; retry from top.
                return self._try_take()
            existing_gen = existing_blob.generation
            try:
                existing = json.loads(raw.decode("utf-8"))
            except (ValueError, KeyError):
                return None
            if self._clock.now() <= float(existing.get("expires_at", 0.0)):
                return None
            # Expired — attempt conditional delete then retry CAS upload.
            try:
                existing_blob.delete(if_generation_match=existing_gen)
            except self._precondition_failed:
                # Someone else stole already; fall through to a retry.
                return None
            return self._try_take()
        # CAS succeeded — capture the generation written by upload_from_string.
        self._held_token = token
        self._held_generation = blob.generation
        return token

    def release(self, token: LockToken) -> None:
        """Conditional-delete the lock object; silent on stolen-after-TTL or NotFound.

        Args:
            token: The :class:`LockToken` returned by :meth:`acquire`.
        """
        if self._held_token is None or self._held_token.nonce != token.nonce:
            return
        if self._held_generation is None:
            return
        bucket = self._store._bucket_handle
        try:
            bucket.blob(self._lock_key()).delete(
                if_generation_match=self._held_generation
            )
        except self._precondition_failed:
            # Stolen after TTL — best-effort: log in production.
            pass
        except self._store._not_found_exc:
            # Already gone (e.g. external delete); silent per spec.
            pass
        self._held_token = None
        self._held_generation = None
