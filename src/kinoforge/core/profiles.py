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

URI lookup
----------
URIs are resolved via ``ArtifactStore.uri_for(run_id, name)`` — pure, no I/O,
deterministic. No in-process cache is needed.
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
        *,
        discover_ttl_s: float = 300.0,
    ) -> None:
        """Initialise the cache.

        Args:
            store: Backing artifact store.
            run_id: Namespace for profile JSON files.
            discover_ttl_s: Outer cross-process lease duration for discovery.
                Should cover worst-case ``inspect_capabilities`` round-trip
                including any provisioner setup.  Default 300s (5 minutes).
        """
        self._store = store
        self._run_id = run_id
        self._discover_ttl_s = discover_ttl_s
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

        Args:
            key: The ``CapabilityKey`` that identifies this profile.
            profile: The ``ModelProfile`` to persist.
        """
        name = self._profile_name(key)
        self._store.put_json(self._run_id, name, _profile_to_dict(profile))

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
        if name not in self._store.list(self._run_id):
            raise ProfileNotCached(
                f"no cached profile for capability key {key.derive()!r}; "
                "call discover() to populate the cache"
            )
        uri = self._store.uri_for(self._run_id, name)
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

        A ``DEBUG`` log message is emitted when BOTH flag keys are absent from
        ``declared_flags(key)`` — on the discovery path the probe is the source
        of truth, so missing declared flags is normal and not actionable.  The
        analogous condition is escalated to ``WARNING`` inside :meth:`verify`
        where it signals real drift against a previously cached profile.

        Args:
            key: The ``CapabilityKey`` identifying the model configuration.
            engine: The engine whose declared flags are merged onto the probe.
            backend: A live backend whose ``inspect_capabilities`` is called.

        Returns:
            The merged, persisted ``ModelProfile``.
        """
        probe = backend.inspect_capabilities()
        declared_flags = engine.declared_flags(key)

        # Quiet DEBUG breadcrumb — empty declared_flags is expected on first
        # discovery for many engines and the probe is authoritative.
        flag_keys = ("supports_native_extension", "supports_joint_audio")
        if not any(f in declared_flags for f in flag_keys):
            _log.debug(
                "engine declared no strategy flags for capability key %s "
                "(supports_native_extension and supports_joint_audio both absent); "
                "this is normal on a fresh-discovery path",
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

    def verify(
        self,
        profile: ModelProfile,
        backend: GenerationBackend,
        *,
        engine: GenerationEngine | None = None,
        key: CapabilityKey | None = None,
    ) -> None:
        """Re-probe the backend and compare probeable fields against *profile*.

        Only ``max_frames``, ``fps``, ``max_resolution``, and
        ``supported_modes`` are compared.  Strategy flags (``supports_native_extension``
        and ``supports_joint_audio``) are intentionally excluded because they
        are engine-declared rather than probed.

        When *both* ``engine`` and ``key`` are provided, a ``WARNING`` is
        emitted if the engine no longer declares either strategy flag for the
        cached key — this surfaces the case where ``declared_flags_map``
        regressed or the engine was downgraded between cache-write and
        cache-read.  On the discovery path the analogous condition is logged
        at ``DEBUG`` only (see :meth:`discover`).

        Args:
            profile: The cached profile to verify against.
            backend: A live backend whose ``inspect_capabilities`` is called.
            engine: Optional engine to query for current ``declared_flags``.
                Must be paired with *key* — passing one without the other is
                accepted for backward compatibility but skips the drift
                WARNING silently, because ``declared_flags`` is indexed by
                ``CapabilityKey`` (which carries engine + precision + LoRAs)
                and cannot be reconstructed from the profile alone.
            key: ``CapabilityKey`` to query ``engine.declared_flags(key)`` with.
                Must be paired with *engine*; see above.

        Raises:
            CapabilityMismatch: Any probeable field differs between *profile*
                and the live probe, with a message containing both the expected
                and actual values.
        """
        probe = backend.inspect_capabilities()

        # Strategy-flag drift check — only meaningful when the caller supplies
        # BOTH the live engine and the key its declared_flags is indexed by.
        # Passing only one is accepted (for ABC/legacy compatibility) but
        # silently skips the check: synthesising a key from the profile name
        # would produce a guaranteed lookup miss and a misleading WARNING.
        if engine is not None and key is not None:
            declared = engine.declared_flags(key)
            if (
                "supports_native_extension" not in declared
                and "supports_joint_audio" not in declared
            ):
                _log.warning(
                    "engine no longer declares strategy flags for cached key %s; "
                    "either declared_flags_map regressed or the engine was "
                    "downgraded",
                    key.derive(),
                )

        for field_name in _PROBEABLE_FIELDS:
            cached_val = getattr(profile, field_name)
            probed_val = getattr(probe, field_name)
            if cached_val != probed_val:
                raise CapabilityMismatch(
                    f"profile drift on field {field_name!r}: "
                    f"expected {cached_val!r} got {probed_val!r}"
                )

    def _discover_single_flight(
        self,
        key: CapabilityKey,
        engine: GenerationEngine,
        backend: GenerationBackend,
    ) -> ModelProfile:
        """In-process leader/follower single-flight body.

        Used as the inner guard under the outer cross-process lock.  Mirrors
        the pre-Layer-H ``resolve_or_discover`` body.

        Args:
            key: The ``CapabilityKey`` whose profile to discover.
            engine: Passed to :meth:`discover` if this thread is the leader.
            backend: Passed to :meth:`discover` if this thread is the leader.

        Returns:
            The ``ModelProfile`` for *key*.
        """
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

    def resolve_or_discover(
        self,
        key: CapabilityKey,
        engine: GenerationEngine,
        backend: GenerationBackend,
    ) -> ModelProfile:
        """Return the cached profile for *key*, discovering it if necessary.

        Cache hits return without taking any lock.  On a miss, an outer
        cross-process lock serializes discovery across processes; the
        in-process ``_discover_single_flight`` body remains as the inner
        guard so multi-thread safety within one process is preserved.

        Args:
            key: The ``CapabilityKey`` whose profile to return.
            engine: Passed to :meth:`discover` if discovery is needed.
            backend: Passed to :meth:`discover` if discovery is needed.

        Returns:
            The ``ModelProfile`` for *key*, from cache or freshly discovered.
        """
        # Cache-hit fast path: no lock.
        try:
            return self.resolve(key)
        except ProfileNotCached:
            pass

        hash_key = key.derive()
        with self._store.acquire_lock(
            f"profiles/{hash_key}", ttl_s=self._discover_ttl_s
        ):
            # Re-check under outer lock: another process may have populated it.
            try:
                return self.resolve(key)
            except ProfileNotCached:
                pass
            return self._discover_single_flight(key, engine, backend)
