"""Abstract base class for artifact stores.

An ArtifactStore persists named blobs scoped by ``run_id``.  Every item is
addressable by a ``uri`` that the store issues on write.  Reads, listings, and
deletes all go through that uri (or the run_id/name pair for listing).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kinoforge.core.interfaces import Artifact


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
