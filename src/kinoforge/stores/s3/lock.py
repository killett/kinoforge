"""S3-backed Lock implementation using conditional PUT (``IfNoneMatch="*"``).

Layout: ``s3://<bucket>/<prefix>/_locks/<sanitized_key>.lock``.

S3 added the ``If-None-Match`` precondition in November 2024.  This adapter
relies on that support; running against any S3-compatible store that
predates it will silently lose mutual exclusion.

Stealing path: when ``put_object(IfNoneMatch="*")`` fails with
``PreconditionFailed``, we GET the existing lock, parse ``expires_at``,
and if the TTL has elapsed we conditional-delete on the captured ETag
then retry the CAS PUT.  Loser-of-the-steal-race retries the full loop.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from kinoforge.core.clock import Clock
from kinoforge.core.locks import LockToken, _sanitize_key
from kinoforge.stores._lease import _LeaseLockBase

if TYPE_CHECKING:
    from kinoforge.stores.s3 import S3ArtifactStore


class S3CloudLock(_LeaseLockBase):
    """Lock backed by an S3 object created via conditional PUT.

    Args:
        store: Owning S3ArtifactStore.  ``store._client`` and ``store.bucket``
            are reused; no extra credentials.
        key: Logical lock key.
        ttl_s: Lease duration in seconds.
        clock: Wall-clock source.
        sleep: Injectable sleep callable.
        poll_interval_s: Seconds between blocking-acquire polls.
    """

    def __init__(
        self,
        *,
        store: S3ArtifactStore,
        key: str,
        ttl_s: float,
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_interval_s: float = 0.05,
    ) -> None:
        """Initialise the lock.

        Args:
            store: Owning S3ArtifactStore; its ``_client`` and ``bucket`` are
                reused without extra credentials.
            key: Logical lock key (may contain forward slashes).
            ttl_s: Lease duration in seconds recorded in the lock payload.
            clock: Wall-clock source; defaults to :class:`RealClock`.
            sleep: Injectable sleep callable for blocking poll loops.
            poll_interval_s: Seconds between blocking-acquire polls.
        """
        super().__init__(
            key=key,
            ttl_s=ttl_s,
            clock=clock,
            sleep=sleep,
            poll_interval_s=poll_interval_s,
        )
        self._store = store
        self._held_etag: str | None = None

    # ------------------------------------------------------------------
    # Storage layout helper
    # ------------------------------------------------------------------

    def _lock_key(self) -> str:
        """Return the S3 object key for the lock sidecar."""
        sanitized = _sanitize_key(self._key)
        parts = [p for p in (self._store.prefix, "_locks", f"{sanitized}.lock") if p]
        return "/".join(parts)

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def _try_take(self) -> LockToken | None:
        """One-shot non-blocking attempt; handles steal-after-TTL.

        Returns the token on success or None on contention.
        """
        client = self._store._client
        bucket = self._store.bucket
        lock_key = self._lock_key()
        token = LockToken(key=self._key)
        payload = json.dumps(
            {"nonce": token.nonce, "expires_at": self._clock.now() + self._ttl_s}
        ).encode("utf-8")
        try:
            resp = client.put_object(
                Bucket=bucket, Key=lock_key, Body=payload, IfNoneMatch="*"
            )
        except client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "PreconditionFailed":
                raise
            # Existing lock — check expiry to decide whether to steal.
            try:
                got = client.get_object(Bucket=bucket, Key=lock_key)
            except client.exceptions.NoSuchKey:
                # Race: gone now, retry from the top.
                return self._try_take()
            existing_etag = got["ETag"]
            try:
                existing = json.loads(got["Body"].read().decode("utf-8"))
            except (ValueError, KeyError):
                return None
            if self._clock.now() <= float(existing.get("expires_at", 0.0)):
                return None
            # Expired — attempt conditional delete then retry CAS PUT.
            try:
                client.delete_object(Bucket=bucket, Key=lock_key, IfMatch=existing_etag)
            except client.exceptions.ClientError as del_err:
                del_code = del_err.response.get("Error", {}).get("Code", "")
                if del_code != "PreconditionFailed":
                    raise
                # Someone else stole already; fall through to a retry.
                return None
            return self._try_take()
        self._held_token = token
        self._held_etag = resp["ETag"]
        return token

    def release(self, token: LockToken) -> None:
        """Conditional-delete the lock object; silent on stolen-after-TTL.

        Args:
            token: The token previously returned by ``acquire``.
        """
        if self._held_token is None or self._held_token.nonce != token.nonce:
            return
        if self._held_etag is None:
            return
        client = self._store._client
        try:
            client.delete_object(
                Bucket=self._store.bucket,
                Key=self._lock_key(),
                IfMatch=self._held_etag,
            )
        except client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "PreconditionFailed":
                raise
            # Stolen after TTL — best-effort: log via stdlib in production.
        self._held_token = None
        self._held_etag = None
