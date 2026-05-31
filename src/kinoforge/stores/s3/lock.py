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
import time as _time
from collections.abc import Callable
from typing import TYPE_CHECKING

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.errors import LockTimeout
from kinoforge.core.locks import LockToken, _sanitize_key

if TYPE_CHECKING:
    from kinoforge.stores.s3 import S3ArtifactStore


class S3CloudLock:
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
        self._store = store
        self._key = key
        self._ttl_s = ttl_s
        self._clock: Clock = clock or RealClock()
        self._sleep: Callable[[float], None] = sleep or _time.sleep
        self._poll_interval_s = poll_interval_s
        self._held_token: LockToken | None = None
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

    def acquire(
        self,
        *,
        blocking: bool = True,
        timeout_s: float | None = None,
    ) -> LockToken | None:
        """Acquire the lock, optionally blocking until timeout.

        Args:
            blocking: If ``False``, return ``None`` immediately on contention.
            timeout_s: When blocking, raise ``LockTimeout`` after this many
                seconds.  ``None`` means poll forever.

        Returns:
            A ``LockToken`` on success, or ``None`` if ``blocking=False`` and
            the lock is held.

        Raises:
            LockTimeout: If ``blocking=True`` and the timeout elapsed.
        """
        if not blocking:
            return self._try_take()
        deadline = self._clock.now() + timeout_s if timeout_s is not None else None
        while True:
            token = self._try_take()
            if token is not None:
                return token
            if deadline is not None and self._clock.now() >= deadline:
                raise LockTimeout(
                    f"failed to acquire lock {self._key!r} within {timeout_s}s"
                )
            self._sleep(self._poll_interval_s)

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

    def __enter__(self) -> LockToken:
        """Acquire the lock and return the token.

        Returns:
            The :class:`~kinoforge.core.locks.LockToken` for this lease.
        """
        token = self.acquire()
        if token is None:  # pragma: no cover — blocking acquire never returns None
            raise RuntimeError("acquire() returned None in blocking context manager")
        return token

    def __exit__(self, *exc: object) -> None:
        """Release the lock on context exit."""
        if self._held_token is not None:
            self.release(self._held_token)
