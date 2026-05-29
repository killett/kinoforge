"""Self-populating ModelProfile cache backed by an ArtifactStore.

``JsonProfileCache`` is the only production ``ModelProfileProvider``.  It
stores profiles as JSON under ``<store-root>/<run_id>/profiles/<hash>.json``
where ``<hash>`` is ``CapabilityKey.derive()``.

Thread safety
-------------
``resolve_or_discover`` implements a per-key single-flight guarantee: if two
threads race to populate the same missing key, exactly one backend probe is
issued and the result is shared via the store — not via in-memory state.

Serialisation notes
-------------------
:class:`~kinoforge.core.interfaces.ModelProfile` contains two fields that are
not natively JSON-serialisable:

* ``supported_modes: set[str]`` → stored as a sorted list for deterministic
  output; deserialised back to ``set``.
* ``max_resolution: tuple[int, int]`` → stored as a two-element list;
  deserialised back to ``tuple``.

URI reconstruction
------------------
``ArtifactStore.get_json`` takes a ``uri`` (not a ``(run_id, name)`` pair).
``JsonProfileCache`` keeps an in-process ``_uri_index`` dict seeded by every
``_persist`` call.  On a fresh instance (e.g. after process restart) the
index is empty; ``resolve`` falls back to ``_reconstruct_uri`` which derives
the URI from ``LocalArtifactStore._path`` when available, ensuring
cross-restart reads work for the local store.
"""

from __future__ import annotations

import threading
from typing import Any

from kinoforge.core.errors import CapabilityMismatch, ProfileNotCached
from kinoforge.core.interfaces import (
    CapabilityKey,
    GenerationBackend,
    GenerationEngine,
    ModelProfile,
    ModelProfileProvider,
)
from kinoforge.core.logging import get_logger
from kinoforge.stores.base import ArtifactStore

_log = get_logger("profiles")

# Probeable fields compared by verify() — strategy flags are intentionally excluded.
_PROBEABLE_FIELDS: tuple[str, ...] = (
    "max_frames",
    "fps",
    "max_resolution",
    "supported_modes",
)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _profile_to_dict(profile: ModelProfile) -> dict[str, Any]:
    """Serialise a ``ModelProfile`` to a JSON-safe dict.

    Args:
        profile: The profile to serialise.

    Returns:
        A JSON-safe dict with ``supported_modes`` as a sorted list and
        ``max_resolution`` as a two-element list.
    """
    return {
        "name": profile.name,
        "max_frames": profile.max_frames,
        "fps": profile.fps,
        "supported_modes": sorted(profile.supported_modes),
        "max_resolution": list(profile.max_resolution),
        "supports_native_extension": profile.supports_native_extension,
        "supports_joint_audio": profile.supports_joint_audio,
    }


def _dict_to_profile(d: dict[str, Any]) -> ModelProfile:
    """Deserialise a ``ModelProfile`` from a dict (e.g. loaded from JSON).

    Args:
        d: A dict as produced by :func:`_profile_to_dict`.

    Returns:
        A ``ModelProfile`` with ``supported_modes`` as ``set`` and
        ``max_resolution`` as ``tuple``.
    """
    return ModelProfile(
        name=d["name"],
        max_frames=int(d["max_frames"]),
        fps=int(d["fps"]),
        supported_modes=set(d["supported_modes"]),
        max_resolution=(int(d["max_resolution"][0]), int(d["max_resolution"][1])),
        supports_native_extension=bool(d["supports_native_extension"]),
        supports_joint_audio=bool(d["supports_joint_audio"]),
    )


# ---------------------------------------------------------------------------
# JsonProfileCache
# ---------------------------------------------------------------------------


class JsonProfileCache(ModelProfileProvider):
    """Persistent, single-flight ModelProfile cache backed by an ArtifactStore.

    Profiles are stored as JSON files under the ``_profiles`` run-id namespace
    so they never collide with clip artifacts.  The storage path for a given
    key is::

        <store-root>/_profiles/profiles/<key.derive()>.json

    Attributes:
        _store: The backing :class:`~kinoforge.stores.base.ArtifactStore`.
        _run_id: The run-id namespace used for profile storage.
        _uri_index: Maps relative item names to absolute URIs; populated by
            :meth:`_persist` and lazily by :meth:`_reconstruct_uri`.
        _lock: Mutex protecting ``_inflight``.
        _inflight: Maps ``key.derive()`` hashes to ``threading.Event`` objects
            that follower threads wait on while the leader executes
            :meth:`discover`.

    Args:
        store: Any ``ArtifactStore`` implementation.
        run_id: Namespace under which profiles are stored.  Defaults to
            ``"_profiles"`` so they are isolated from clip artifacts.
    """

    def __init__(
        self,
        store: ArtifactStore,
        run_id: str = "_profiles",
    ) -> None:
        """Initialise the cache.

        Args:
            store: Backing artifact store.
            run_id: Namespace for profile JSON files.
        """
        self._store = store
        self._run_id = run_id
        self._uri_index: dict[str, str] = {}
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _profile_name(self, key: CapabilityKey) -> str:
        """Return the store-relative item name for *key*.

        Args:
            key: The ``CapabilityKey`` whose derived hash forms the filename.

        Returns:
            A relative name string suitable for ``put_json`` / ``get_json``.
        """
        return f"profiles/{key.derive()}.json"

    def _persist(self, key: CapabilityKey, profile: ModelProfile) -> None:
        """Serialise *profile* and write it to the store under *key*'s name.

        Populates ``_uri_index`` so that subsequent :meth:`resolve` calls can
        retrieve the profile without coupling to store internals.

        Args:
            key: The ``CapabilityKey`` that identifies this profile.
            profile: The ``ModelProfile`` to persist.
        """
        name = self._profile_name(key)
        artifact = self._store.put_json(self._run_id, name, _profile_to_dict(profile))
        self._uri_index[name] = artifact.uri

    def _uri_for(self, name: str) -> str:
        """Return the absolute URI for a stored item by name.

        Checks ``_uri_index`` first; falls back to ``_reconstruct_uri`` for
        cross-restart reads.

        Args:
            name: Relative item name as returned by :meth:`_profile_name`.

        Returns:
            The absolute URI string.

        Raises:
            ProfileNotCached: URI cannot be determined (name not in store or
                URI reconstruction not supported by the store backend).
        """
        if name in self._uri_index:
            return self._uri_index[name]
        return self._reconstruct_uri(name)

    def _reconstruct_uri(self, name: str) -> str:
        """Derive a URI for an existing item without having previously written it.

        This is called on a fresh ``JsonProfileCache`` instance (e.g. after
        process restart) when ``_uri_index`` has not been populated for *name*.
        For ``LocalArtifactStore`` the URI is the resolved absolute filesystem
        path, which can be reconstructed from the store's ``_path`` method.

        Args:
            name: Relative item name within the run-id namespace.

        Returns:
            The absolute URI string; also caches the result in ``_uri_index``.

        Raises:
            ProfileNotCached: The backing store does not expose ``_path`` and
                the URI cannot be reconstructed after a process restart.
        """
        if hasattr(self._store, "_path"):
            path = self._store._path(self._run_id, name)
            uri = str(path)
            self._uri_index[name] = uri
            return uri
        raise ProfileNotCached(
            f"cannot reconstruct URI for {name!r}: store does not expose _path "
            "and _uri_index was not populated (cross-restart on non-local store)"
        )

    # ------------------------------------------------------------------
    # ModelProfileProvider implementation
    # ------------------------------------------------------------------

    def resolve(self, key: CapabilityKey) -> ModelProfile:
        """Return the cached ``ModelProfile`` for *key*, or raise on miss.

        Does NOT call any backend or engine method.

        Args:
            key: The ``CapabilityKey`` whose profile to look up.

        Returns:
            The cached ``ModelProfile``.

        Raises:
            ProfileNotCached: No profile has been persisted for *key* yet.
        """
        name = self._profile_name(key)
        # Check store listing to detect cross-restart case where _uri_index
        # is empty but the file exists on disk.
        listed = self._store.list(self._run_id)
        if name not in listed:
            raise ProfileNotCached(
                f"no cached profile for capability key {key.derive()!r}; "
                "call discover() to populate the cache"
            )
        uri = self._uri_for(name)
        raw = self._store.get_json(uri)
        return _dict_to_profile(raw)

    def discover(
        self,
        key: CapabilityKey,
        engine: GenerationEngine,
        backend: GenerationBackend,
    ) -> ModelProfile:
        """Probe the live backend, merge engine flags, persist, and return.

        ``backend.inspect_capabilities()`` is called exactly once.  The result
        from ``engine.declared_flags(key)`` overrides ``supports_native_extension``
        and ``supports_joint_audio`` on the probed profile.

        A ``WARNING`` is emitted when BOTH flag keys are absent from
        ``declared_flags(key)`` — this indicates the engine has not declared
        strategy capabilities for this key combination.

        Args:
            key: The ``CapabilityKey`` identifying the model configuration.
            engine: The engine whose declared flags are merged onto the probe.
            backend: A live backend whose ``inspect_capabilities`` is called.

        Returns:
            The merged, persisted ``ModelProfile``.
        """
        probe = backend.inspect_capabilities()
        declared_flags = engine.declared_flags(key)

        # Emit an under-use warning when neither strategy flag is declared.
        flag_keys = ("supports_native_extension", "supports_joint_audio")
        if not any(f in declared_flags for f in flag_keys):
            _log.warning(
                "engine declared no strategy flags for capability key %s "
                "(supports_native_extension and supports_joint_audio both absent); "
                "check declared_flags_map for this engine/key combination",
                key.derive(),
            )

        # Build the merged profile: probe provides all probeable fields;
        # declared_flags override the two strategy flag fields only.
        merged = ModelProfile(
            name=probe.name,
            max_frames=probe.max_frames,
            fps=probe.fps,
            supported_modes=probe.supported_modes,
            max_resolution=probe.max_resolution,
            supports_native_extension=bool(
                declared_flags.get(
                    "supports_native_extension", probe.supports_native_extension
                )
            ),
            supports_joint_audio=bool(
                declared_flags.get("supports_joint_audio", probe.supports_joint_audio)
            ),
        )

        self._persist(key, merged)
        return merged

    def verify(self, profile: ModelProfile, backend: GenerationBackend) -> None:
        """Re-probe the backend and compare probeable fields against *profile*.

        Only ``max_frames``, ``fps``, ``max_resolution``, and
        ``supported_modes`` are compared.  Strategy flags (``supports_native_extension``
        and ``supports_joint_audio``) are intentionally excluded because they
        are engine-declared rather than probed.

        Args:
            profile: The cached profile to verify against.
            backend: A live backend whose ``inspect_capabilities`` is called.

        Raises:
            CapabilityMismatch: Any probeable field differs between *profile*
                and the live probe, with a message containing both the expected
                and actual values.
        """
        probe = backend.inspect_capabilities()
        for field_name in _PROBEABLE_FIELDS:
            cached_val = getattr(profile, field_name)
            probed_val = getattr(probe, field_name)
            if cached_val != probed_val:
                raise CapabilityMismatch(
                    f"profile drift on field {field_name!r}: "
                    f"expected {cached_val!r} got {probed_val!r}"
                )

    def resolve_or_discover(
        self,
        key: CapabilityKey,
        engine: GenerationEngine,
        backend: GenerationBackend,
    ) -> ModelProfile:
        """Return the cached profile for *key*, discovering it if necessary.

        Implements per-key single-flight: if two threads race to discover the
        same uncached key, exactly one backend probe is issued.  The follower
        thread waits for the leader to finish persisting, then reads the result
        from the store.

        Args:
            key: The ``CapabilityKey`` whose profile to return.
            engine: Passed to :meth:`discover` if discovery is needed.
            backend: Passed to :meth:`discover` if discovery is needed.

        Returns:
            The ``ModelProfile`` for *key*, from cache or freshly discovered.
        """
        try:
            return self.resolve(key)
        except ProfileNotCached:
            pass

        hash_key = key.derive()

        with self._lock:
            ev = self._inflight.get(hash_key)
            if ev is None:
                ev = threading.Event()
                self._inflight[hash_key] = ev
                is_leader = True
            else:
                is_leader = False

        if is_leader:
            try:
                profile = self.discover(key, engine, backend)
            finally:
                with self._lock:
                    ev.set()
                    self._inflight.pop(hash_key, None)
            return profile

        ev.wait()
        return self.resolve(key)
