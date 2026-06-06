"""Filesystem-backed ArtifactStore implementation.

Items are written under ``<root>/<run_id>/<name>``.  The ``uri`` stored in
returned :class:`~kinoforge.core.interfaces.Artifact` objects is the
**resolved absolute path** so round-trips work regardless of the caller's CWD.

Self-registers under ``"local"`` on import via the store registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from kinoforge.core.interfaces import Artifact
from kinoforge.core.locks import Lock, _sanitize_key
from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local_lock import FileLock


class LocalArtifactStore(ArtifactStore):
    """Artifact store that writes to the local filesystem.

    Storage layout::

        <root>/
          <run_id>/
            <name>          # e.g. "out.bin" or "profiles/abc.json"

    Attributes:
        root: The resolved absolute root directory for all stored items.
    """

    def __init__(self, root: Path) -> None:
        """Initialise a store rooted at *root*.

        Args:
            root: Base directory.  It need not exist yet; it will be created
                  on the first ``put_*`` call.
        """
        self.root: Path = root.resolve()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _path(self, run_id: str, name: str) -> Path:
        """Return the absolute path for ``<run_id>/<name>`` (unsanitised).

        Args:
            run_id: Run identifier.
            name: Item name, may contain forward slashes.

        Returns:
            Resolved absolute path under *root*.
        """
        return (self.root / run_id / name).resolve()

    # ------------------------------------------------------------------
    # ArtifactStore implementation
    # ------------------------------------------------------------------

    def uri_for(self, run_id: str, name: str) -> str:
        """Return the absolute filesystem path for ``(run_id, name)`` as a string.

        Pure: no FS I/O. Matches what :meth:`put_bytes` / :meth:`put_json` would
        return for the same args.

        Args:
            run_id: Run identifier.
            name: Item name; may contain forward slashes.

        Returns:
            The resolved absolute path as a str.
        """
        return str(self._path(run_id, name))

    def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact:
        """Write *data* under ``<root>/<run_id>/<name>`` and return a handle.

        Args:
            run_id: Opaque run identifier.
            name: Relative item name within the run.
            data: Raw bytes to persist.

        Returns:
            :class:`~kinoforge.core.interfaces.Artifact` with ``uri`` set to
            the resolved absolute path string.
        """
        p = self._path(run_id, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return Artifact(uri=str(p))

    def get_bytes(self, uri: str) -> bytes:
        """Read and return the bytes stored at *uri*.

        Args:
            uri: The ``uri`` field of an :class:`~kinoforge.core.interfaces.Artifact`
                returned by :meth:`put_bytes` or :meth:`put_json`.

        Returns:
            The exact byte sequence that was stored.

        Raises:
            FileNotFoundError: No file exists at *uri*.
        """
        return Path(uri).read_bytes()

    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact:  # type: ignore[type-arg]
        """Serialise *obj* as UTF-8 JSON and persist it under ``<run_id>/<name>``.

        Args:
            run_id: Opaque run identifier.
            name: Relative item name within the run.
            obj: Any JSON-serialisable :class:`dict`.

        Returns:
            :class:`~kinoforge.core.interfaces.Artifact` with ``uri`` set.
        """
        return self.put_bytes(run_id, name, json.dumps(obj).encode("utf-8"))

    def get_json(self, uri: str) -> dict:  # type: ignore[type-arg]
        """Deserialise and return the JSON object stored at *uri*.

        Args:
            uri: The ``uri`` returned by :meth:`put_json`.

        Returns:
            The deserialised :class:`dict`.

        Raises:
            FileNotFoundError: No file exists at *uri*.
        """
        return json.loads(self.get_bytes(uri).decode("utf-8"))  # type: ignore[no-any-return]

    def list(self, run_id: str) -> list[str]:
        """Return the names of all items stored under *run_id*.

        Args:
            run_id: Run identifier to enumerate.

        Returns:
            List of ``name`` strings relative to ``<root>/<run_id>/``.  An
            empty list is returned when *run_id* has no stored items (or its
            directory does not exist yet).
        """
        run_dir = self.root / run_id
        if not run_dir.exists():
            return []
        return [str(p.relative_to(run_dir)) for p in run_dir.rglob("*") if p.is_file()]

    def delete(self, uri: str) -> None:
        """Remove the file at *uri*.

        Args:
            uri: The ``uri`` returned by a previous put call.

        Raises:
            FileNotFoundError: No file exists at *uri*.
        """
        p = Path(uri)
        if not p.exists():
            raise FileNotFoundError(f"artifact not found: {uri!r}")
        p.unlink()

    def acquire_lock(self, key: str, *, ttl_s: float) -> Lock:
        """Return a :class:`FileLock` rooted under ``<root>/_locks/``.

        Args:
            key: Logical lock key (may contain forward slashes).
            ttl_s: Lease duration in seconds (informational on local FS;
                ``fcntl`` owns mutual exclusion).

        Returns:
            A fresh :class:`FileLock` whose sidecar lives at
            ``<root>/_locks/<sanitized_key>.lock``.
        """
        sanitized = _sanitize_key(key)
        path = self.root / "_locks" / f"{sanitized}.lock"
        return FileLock(path=path, key=key, ttl_s=ttl_s)

    def signed_url(
        self,
        run_id: str,
        name: str,
        *,
        op: Literal["GET", "PUT"],
        ttl_s: int,
    ) -> str:
        """Signed URLs not supported on local filesystem artifacts.

        Raises:
            NotImplementedError: Always — local files have no transport-layer auth.
        """
        raise NotImplementedError("LocalArtifactStore does not support signed URLs")


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

from kinoforge.core.registry import register_store  # noqa: E402

register_store("local", lambda: LocalArtifactStore(Path(".kinoforge")))
