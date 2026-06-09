"""Abstract base class for artifact stores.

An ArtifactStore persists named blobs scoped by ``run_id``.  Every item is
addressable by a ``uri`` that the store issues on write.  Reads, listings, and
deletes all go through that uri (or the run_id/name pair for listing).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Literal

from kinoforge.core.interfaces import Artifact

if TYPE_CHECKING:
    from kinoforge.core.locks import Lock


class ArtifactStore(ABC):
    """Content-addressed blob store scoped by run_id.

    All methods that write content return an :class:`~kinoforge.core.interfaces.Artifact`
    whose ``uri`` field uniquely identifies the stored item and can be passed back
    to the read/delete methods.
    """

    @abstractmethod
    def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact:
        """Persist raw bytes under ``<run_id>/<name>`` and return a handle.

        Args:
            run_id: Opaque identifier grouping items from one pipeline run.
            name: Relative name within the run, e.g. ``"profiles/abc.json"``.
                  May contain forward-slash path separators for sub-namespacing.
            data: The raw bytes to store.

        Returns:
            An :class:`~kinoforge.core.interfaces.Artifact` with ``uri`` set
            to an address that can be passed to :meth:`get_bytes` or
            :meth:`delete`.
        """

    @abstractmethod
    def get_bytes(self, uri: str) -> bytes:
        """Return the bytes previously stored at ``uri``.

        Args:
            uri: The ``uri`` field of an :class:`~kinoforge.core.interfaces.Artifact`
                returned by :meth:`put_bytes` or :meth:`put_json`.

        Returns:
            The exact byte sequence that was stored.

        Raises:
            FileNotFoundError: No item exists at ``uri``.
        """

    @abstractmethod
    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact:  # type: ignore[type-arg]
        """Serialise *obj* as UTF-8 JSON and store it under ``<run_id>/<name>``.

        Args:
            run_id: Opaque run identifier.
            name: Relative name within the run (e.g. ``"profiles/abc.json"``).
            obj: Any JSON-serialisable :class:`dict`.

        Returns:
            An :class:`~kinoforge.core.interfaces.Artifact` with ``uri`` set.
        """

    @abstractmethod
    def get_json(self, uri: str) -> dict:  # type: ignore[type-arg]
        """Deserialise and return the JSON object stored at ``uri``.

        Args:
            uri: The ``uri`` returned by :meth:`put_json`.

        Returns:
            The deserialised :class:`dict`.

        Raises:
            FileNotFoundError: No item exists at ``uri``.
        """

    @abstractmethod
    def list(self, run_id: str) -> list[str]:
        """Enumerate the names stored under ``run_id``.

        Args:
            run_id: The run whose stored items to enumerate.

        Returns:
            A list of ``name`` strings exactly as they were passed to
            :meth:`put_bytes` / :meth:`put_json`.  Returns an empty list when
            nothing has been stored under ``run_id`` yet.
        """

    @abstractmethod
    def delete(self, uri: str) -> None:
        """Remove a single stored item.

        Args:
            uri: The ``uri`` returned by a previous put call.

        Raises:
            FileNotFoundError: No item exists at ``uri``.
        """

    @abstractmethod
    def uri_for(self, run_id: str, name: str) -> str:
        """Return the URI that would address ``(run_id, name)`` under this store.

        Pure: performs no I/O.  Does NOT check whether the item exists; callers
        that care about existence should use :meth:`list` or let
        :meth:`get_bytes` raise ``FileNotFoundError`` on miss.

        The returned URI MUST equal the ``uri`` field of the
        :class:`~kinoforge.core.interfaces.Artifact` that :meth:`put_bytes` or
        :meth:`put_json` would return for the same ``(run_id, name)`` pair — this
        is the invariant consumers rely on for cross-restart reads.

        Args:
            run_id: Opaque identifier grouping items from one pipeline run.
            name: Relative item name within the run.

        Returns:
            The absolute URI string.
        """

    @abstractmethod
    def acquire_lock(self, key: str, *, ttl_s: float) -> Lock:
        """Return a fresh :class:`~kinoforge.core.locks.Lock` for ``key``.

        The returned lock is best-effort lease-based: the holder keeps it for
        up to ``ttl_s`` seconds; after expiry another acquirer may steal.

        Args:
            key: Logical lock key.  May contain forward slashes (sanitized
                internally to flat filenames).
            ttl_s: Lease duration in seconds.

        Returns:
            A new :class:`~kinoforge.core.locks.Lock` instance.  Each call
            returns a fresh object; sharing across threads/processes is the
            caller's concern.
        """

    @abstractmethod
    def delete_run(self, run_id: str) -> None:
        """Remove every artifact stored under ``run_id``.

        Idempotent: a missing ``run_id`` is a no-op, not an error. Atomic at
        the per-name level; implementations unable to remove the prefix
        atomically MUST iterate :meth:`list` and delete each item, raising
        on the first per-name failure that is not ``FileNotFoundError``.

        Args:
            run_id: The run namespace to wipe.

        Raises:
            OSError: A per-name delete failed for a reason other than the
                item being absent.
        """

    @abstractmethod
    def manual_cleanup_command(self, run_id: str) -> str:
        """Return a single-line shell command that wipes ``run_id``'s prefix.

        Used in error messages when :meth:`delete_run` fails so the user can
        finish the cleanup by hand. Must produce an absolute, copy-pasteable
        command.

        Args:
            run_id: The run namespace the command targets.

        Returns:
            A single-line shell command that, when run by the operator, will
            wipe everything under this store's ``run_id`` prefix.
        """

    @abstractmethod
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
            op: HTTP method the URL grants. ``"GET"`` downloads; ``"PUT"`` uploads.
            ttl_s: Validity window in seconds from issuance.

        Returns:
            Absolute HTTPS URL valid for ``ttl_s`` seconds.

        Raises:
            NotImplementedError: Backend does not support signed URLs (e.g.
                ``LocalArtifactStore``).
        """
