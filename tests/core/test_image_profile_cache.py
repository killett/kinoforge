"""Layer R T5: JsonImageProfileCache namespace tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.errors import CapabilityMismatch, ProfileNotCached
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    Instance,
)
from kinoforge.core.profiles import JsonImageProfileCache, JsonProfileCache
from kinoforge.stores.local import LocalArtifactStore


def _key() -> CapabilityKey:
    return CapabilityKey(base_model="m", engine="fake", precision="")


def _profile() -> ImageProfile:
    return ImageProfile(
        name="m",
        max_resolution=(1024, 1024),
        supported_modes={"t2i"},
    )


class _FakeImageBackend(ImageBackend):
    def __init__(self, p: ImageProfile) -> None:
        self.p = p

    def capabilities(self) -> ImageProfile:
        return self.p

    def inspect_capabilities(self) -> ImageProfile:
        return self.p

    def submit(self, job: ImageJob) -> str:
        return "id"

    def result(self, job_id: str) -> Artifact:
        return Artifact(filename="x.png")

    def endpoints(self) -> dict[str, str]:
        return {}


class _FakeImageEngine(ImageEngine):
    name = "fake"
    requires_compute = False
    requires_local_weights = False

    def __init__(self, p: ImageProfile) -> None:
        self.p = p

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        return

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> ImageBackend:
        return _FakeImageBackend(self.p)

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        return self.p

    def validate_spec(self, job: ImageJob) -> None:
        return


def test_resolve_miss_raises(tmp_path: Path) -> None:
    """Bug guard: missing image profile must raise the same ProfileNotCached as video side.
    A silent None return would be caught by the orchestrator's discover branch indirectly,
    but the explicit raise is the documented contract."""
    cache = JsonImageProfileCache(LocalArtifactStore(tmp_path))
    with pytest.raises(ProfileNotCached):
        cache.resolve(_key())


def test_discover_writes_image_json_namespace(tmp_path: Path) -> None:
    """Persisted file must end with `.image.json`, NOT `.json`.
    Bug guard: writing to the video namespace would silently overwrite a video profile
    with the same CapabilityKey."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    out = cache.discover(_key(), eng, eng.backend(None, {}))
    assert out == p
    image_files = list(tmp_path.glob("**/*.image.json"))
    assert len(image_files) == 1
    video_files = [
        f for f in tmp_path.glob("**/*.json") if not f.name.endswith(".image.json")
    ]
    assert video_files == [], video_files


def test_resolve_hit_reads_discovered_profile(tmp_path: Path) -> None:
    """Round-trip: discover then resolve returns the same profile."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    out = cache.resolve(_key())
    assert out == p  # type: ignore[comparison-overlap]


def test_image_and_video_cache_do_not_collide(tmp_path: Path) -> None:
    """Same CapabilityKey, different namespaces — must not overwrite each other.
    Bug guard: a cache-key collision would let an image discover overwrite a
    video profile and the next video resolve would deserialise garbage."""
    store = LocalArtifactStore(tmp_path)
    image_cache = JsonImageProfileCache(store)
    video_cache = JsonProfileCache(store)
    key = _key()
    ip = _profile()
    eng = _FakeImageEngine(ip)
    image_cache.discover(key, eng, eng.backend(None, {}))
    with pytest.raises(ProfileNotCached):
        video_cache.resolve(key)


def test_verify_match_succeeds(tmp_path: Path) -> None:
    """Verify with matching live capabilities is a no-op."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    cache.verify(p, eng.backend(None, {}), engine=eng, key=_key())


def test_verify_mismatch_raises(tmp_path: Path) -> None:
    """Drift between cached and live profile must raise CapabilityMismatch.
    Bug guard: silent acceptance lets a model swap go undetected and the next
    generate runs against a model that doesn't match the cached profile."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    drifted = ImageProfile(
        name="m",
        max_resolution=(2048, 2048),
        supported_modes={"t2i"},
    )
    drifted_eng = _FakeImageEngine(drifted)
    with pytest.raises(CapabilityMismatch):
        cache.verify(p, drifted_eng.backend(None, {}), engine=drifted_eng, key=_key())


def test_supported_modes_set_round_trip(tmp_path: Path) -> None:
    """JSON has no `set`; persistence must round-trip via sorted list.
    Bug guard: a regression that serialises the set's repr would deserialise
    to a string, not a set, and verify would always fail."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = ImageProfile(
        name="m",
        max_resolution=(1024, 1024),
        supported_modes={"t2i", "i2i", "inpaint"},
    )
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    out = cache.resolve(_key())
    assert isinstance(out.supported_modes, set)
    assert out.supported_modes == {"t2i", "i2i", "inpaint"}


def test_max_resolution_tuple_round_trip(tmp_path: Path) -> None:
    """Tuple must survive JSON list round-trip (JSON has no tuple type).
    Bug guard: list-typed max_resolution after deserialise breaks `(w, h)` unpacking
    in any consumer that destructures."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = ImageProfile(name="m", max_resolution=(2048, 1024), supported_modes={"t2i"})
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    out = cache.resolve(_key())
    assert isinstance(out.max_resolution, tuple)
    assert out.max_resolution == (2048, 1024)


def test_namespace_filename_pattern(tmp_path: Path) -> None:
    """Persisted filename starts with CapabilityKey.derive() hex + ends `.image.json`."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    files = sorted(p.name for p in tmp_path.glob("**/*.image.json"))
    assert len(files) == 1
    assert files[0].endswith(".image.json")
    assert files[0].split(".")[0] == _key().derive()


def test_inflight_dedup_persists_after_first_discover(tmp_path: Path) -> None:
    """Subsequent discover calls on the same key short-circuit via the cache.
    Bug guard: re-running inspect_capabilities on every discover would be
    expensive against real hosted backends."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    backend = eng.backend(None, {})

    n = 0
    original = backend.inspect_capabilities

    def counting() -> ImageProfile:
        nonlocal n
        n += 1
        return original()

    backend.inspect_capabilities = counting  # type: ignore[method-assign]
    cache.discover(_key(), eng, backend)
    cache.discover(_key(), eng, backend)
    assert n == 1, f"expected single inspect_capabilities call, got {n}"
