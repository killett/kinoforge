"""Tests for JsonProfileCache — Task 12 Acceptance Criteria.

Each test maps 1-to-1 with one of the 7 ACs in the spec, covers a concrete
failure mode, and derives expected values independently of the implementation.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.errors import CapabilityMismatch, ProfileNotCached
from kinoforge.core.interfaces import (
    CapabilityKey,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.stores.local import LocalArtifactStore

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_probe(
    *,
    max_frames: int = 24,
    fps: int = 8,
    supported_modes: set[str] | None = None,
    max_resolution: tuple[int, int] = (512, 512),
    supports_native_extension: bool = False,
    supports_joint_audio: bool = False,
    name: str = "probe",
) -> ModelProfile:
    return ModelProfile(
        name=name,
        max_frames=max_frames,
        fps=fps,
        supported_modes=supported_modes if supported_modes is not None else {"t2v"},
        max_resolution=max_resolution,
        supports_native_extension=supports_native_extension,
        supports_joint_audio=supports_joint_audio,
    )


def _make_key(loras: tuple[str, ...] = ()) -> CapabilityKey:
    return CapabilityKey(
        base_model="hf:org/model",
        loras=loras,
        engine="fake",
        precision="fp16",
    )


# --- minimal fake engine -----------------------------------------------------


class _FakeEngine(GenerationEngine):
    """Minimal test double; real FakeEngine not used here to keep tests isolated."""

    name: str = "test-engine"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(
        self, flags_by_derive: dict[str, dict[str, bool]] | None = None
    ) -> None:
        self._flags = flags_by_derive or {}

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: object | None = None,
    ) -> None:  # noqa: D102
        pass

    def backend(  # noqa: D102
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> GenerationBackend:
        raise NotImplementedError

    def profile_for(self, key: CapabilityKey) -> ModelProfile:  # noqa: D102
        raise NotImplementedError

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:  # noqa: D102
        return dict(self._flags.get(key.derive(), {}))

    def validate_spec(self, job: GenerationJob) -> None:  # noqa: D102
        pass

    def model_identity(self, cfg: dict[str, object]) -> str:  # noqa: D102
        return ""


# --- minimal fake backend ----------------------------------------------------


class _CountingBackend(GenerationBackend):
    """Records every inspect_capabilities call; raises on submit/result/endpoints."""

    def __init__(self, probe: ModelProfile) -> None:
        self._probe = probe
        self.call_count = 0

    def inspect_capabilities(self) -> ModelProfile:  # noqa: D102
        self.call_count += 1
        return self._probe

    def capabilities(self) -> ModelProfile:  # noqa: D102
        return self._probe

    def submit(  # noqa: D102
        self,
        job: GenerationJob,
        *,
        cancel_token: object | None = None,
    ) -> str:
        raise NotImplementedError

    def result(  # noqa: D102
        self,
        job_id: str,
        *,
        cancel_token: object | None = None,
    ) -> Any:
        raise NotImplementedError

    def endpoints(self) -> dict[str, str]:  # noqa: D102
        raise NotImplementedError


class _SlowBackend(_CountingBackend):
    """Stalls inside inspect_capabilities until released by the test.

    The leader thread calls inspect_capabilities and blocks at ``_gate``
    (a threading.Event).  The test releases the gate after both threads have
    been started, ensuring the follower thread has time to reach
    resolve_or_discover and observe the in-flight event before the leader
    completes.
    """

    def __init__(self, probe: ModelProfile, gate: threading.Event) -> None:
        super().__init__(probe)
        self._gate = gate

    def inspect_capabilities(self) -> ModelProfile:  # noqa: D102
        self._gate.wait()  # block until test releases
        self.call_count += 1
        return self._probe


# ---------------------------------------------------------------------------
# AC 1 — resolve() on miss raises ProfileNotCached, no backend called
# ---------------------------------------------------------------------------


def test_resolve_miss_raises_profile_not_cached(tmp_path: Path) -> None:
    """resolve() on an empty store must raise ProfileNotCached.

    Bug caught: if resolve() falls through to backend probing instead of
    raising, the assertion fails.  We don't even pass a backend — anything
    calling it would raise AttributeError, giving a secondary failure signal.
    """
    from kinoforge.core.profiles import JsonProfileCache

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)
    key = _make_key()

    with pytest.raises(ProfileNotCached):
        cache.resolve(key)


def test_resolve_miss_never_calls_backend(tmp_path: Path) -> None:
    """resolve() must not touch any backend even when one could be reached.

    Bug caught: if resolve() tries to probe the backend before checking
    the cache, the call_count would be > 0 and the assertion below would fail.
    """
    from kinoforge.core.profiles import JsonProfileCache

    probe = _make_probe()
    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)
    backend = _CountingBackend(probe)
    key = _make_key()

    with pytest.raises(ProfileNotCached):
        cache.resolve(key)

    # Backend was never touched.
    assert backend.call_count == 0


# ---------------------------------------------------------------------------
# AC 2 — resolve() on hit returns cached profile, max_segment_seconds correct
# ---------------------------------------------------------------------------


def test_resolve_hit_returns_cached_profile_no_compute(tmp_path: Path) -> None:
    """resolve() on a pre-persisted profile must return it with correct field values.

    Expected: max_segment_seconds == 24 / 8 == 3.0  (hand-calculated).
    max_resolution must be a tuple, not a list (JSON round-trip hazard).

    Bug caught: wrong deserialization (list not tuple, fps as string, etc.)
    would break max_segment_seconds or cause a type error downstream.
    """
    from kinoforge.core.profiles import JsonProfileCache

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)
    key = _make_key()

    # Pre-populate the cache by going through discover with a counting backend.
    probe = _make_probe(max_frames=24, fps=8)
    backend = _CountingBackend(probe)
    engine = _FakeEngine()
    cache.discover(key, engine, backend)

    # Now resolve — must not call backend (pass None to prove no compute).
    resolved = cache.resolve(key)

    assert resolved.max_segment_seconds == pytest.approx(3.0)
    assert isinstance(resolved.max_resolution, tuple), (
        "max_resolution must round-trip as tuple, not list"
    )
    assert resolved.max_frames == 24
    assert resolved.fps == 8
    assert resolved.supported_modes == {"t2v"}


# ---------------------------------------------------------------------------
# AC 3 — discover() calls inspect_capabilities exactly once, merges flags,
#         persists, and subsequent resolve() returns same profile
# ---------------------------------------------------------------------------


def test_discover_calls_inspect_capabilities_once_and_merges_flags(
    tmp_path: Path,
) -> None:
    """discover() must probe exactly once and merge declared_flags onto the profile.

    Bug caught: double-probing raises call_count to 2; forgetting to merge
    flags leaves supports_native_extension=False when the engine declares True.
    Expected: call_count == 1 (by inspection); flag == True (from engine fixture).
    """
    from kinoforge.core.profiles import JsonProfileCache

    probe = _make_probe(supports_native_extension=False)
    backend = _CountingBackend(probe)
    key = _make_key()
    engine = _FakeEngine(
        flags_by_derive={key.derive(): {"supports_native_extension": True}}
    )

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    result = cache.discover(key, engine, backend)

    assert backend.call_count == 1
    assert result.supports_native_extension is True


def test_discover_persists_and_resolve_returns_same_profile(tmp_path: Path) -> None:
    """After discover(), resolve() must return the persisted profile — same max_frames.

    Bug caught: if discover() doesn't persist to the store, resolve() would
    raise ProfileNotCached instead of returning the profile.
    """
    from kinoforge.core.profiles import JsonProfileCache

    probe = _make_probe(max_frames=32)
    backend = _CountingBackend(probe)
    key = _make_key()
    engine = _FakeEngine(
        flags_by_derive={key.derive(): {"supports_native_extension": True}}
    )

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    discovered = cache.discover(key, engine, backend)
    resolved = cache.resolve(key)

    assert resolved.max_frames == discovered.max_frames == 32
    assert resolved.supports_native_extension is True


# ---------------------------------------------------------------------------
# AC 4 — Key distinctness: bare base vs base+LoRA produce separate cache entries
# ---------------------------------------------------------------------------


def test_key_distinctness_bare_vs_lora(tmp_path: Path) -> None:
    """bare and +LoRA keys must resolve independently with their own flags.

    Bug caught: if derive() ignores the lora tuple, both keys map to the
    same file path and the second discover() overwrites the first — so
    resolve(key_bare) would return the LoRA profile (wrong flag).
    """
    from kinoforge.core.profiles import JsonProfileCache

    key_bare = _make_key(loras=())
    key_lora = _make_key(loras=("civitai:lora42",))
    # Keys must differ (sanity check for the test itself).
    assert key_bare.derive() != key_lora.derive()

    probe_bare = _make_probe(supports_native_extension=False)
    probe_lora = _make_probe(supports_native_extension=False)

    engine = _FakeEngine(
        flags_by_derive={
            key_bare.derive(): {"supports_native_extension": False},
            key_lora.derive(): {"supports_native_extension": True},
        }
    )

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    cache.discover(key_bare, engine, _CountingBackend(probe_bare))
    cache.discover(key_lora, engine, _CountingBackend(probe_lora))

    resolved_bare = cache.resolve(key_bare)
    resolved_lora = cache.resolve(key_lora)

    assert resolved_bare.supports_native_extension is False
    assert resolved_lora.supports_native_extension is True


# ---------------------------------------------------------------------------
# AC 5 — Single-flight: two racing threads trigger exactly ONE inspect_capabilities
# ---------------------------------------------------------------------------


def test_single_flight_two_threads_one_probe(tmp_path: Path) -> None:
    """Two concurrent resolve_or_discover calls for the same key must produce
    exactly one inspect_capabilities call; both threads receive the same profile.

    Bug caught: without the single-flight lock, both threads enter discover()
    and call_count reaches 2.

    Choreography:
      1. A ``gate`` Event starts closed (not set).
      2. Both threads start and reach ``resolve_or_discover`` simultaneously
         (guaranteed by a start_barrier(2)).
      3. One thread becomes the leader and enters ``inspect_capabilities``,
         where it blocks on ``gate.wait()``.
      4. The other becomes the follower and blocks on the inflight Event inside
         ``resolve_or_discover``.
      5. The main thread gives both threads time to reach their block points,
         then sets ``gate``, unblocking the leader.
      6. After both threads join, assert call_count == 1.
    """
    from kinoforge.core.profiles import JsonProfileCache

    probe = _make_probe(max_frames=48, fps=24)
    gate = threading.Event()
    backend = _SlowBackend(probe, gate)
    key = _make_key()
    engine = _FakeEngine(
        flags_by_derive={key.derive(): {"supports_native_extension": True}}
    )

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    start_barrier: threading.Barrier = threading.Barrier(2)

    results: list[ModelProfile] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            start_barrier.wait()  # both threads reach here before either proceeds
            p = cache.resolve_or_discover(key, engine, backend)
            results.append(p)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()

    # Give both threads time to: pass the start_barrier, miss the cache, and
    # reach their respective block points (leader in inspect_capabilities,
    # follower on the inflight Event).
    import time

    time.sleep(0.05)
    gate.set()  # release the leader

    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"worker thread(s) raised: {errors}"
    assert backend.call_count == 1, (
        f"expected exactly 1 inspect_capabilities call, got {backend.call_count}"
    )
    assert len(results) == 2
    assert results[0].max_frames == results[1].max_frames == 48


# ---------------------------------------------------------------------------
# AC 6 — Under-use warning when declared_flags is empty
# ---------------------------------------------------------------------------


def test_discover_with_no_declared_flags_logs_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fresh discovery with no declared_flags should be a quiet DEBUG, not WARNING.

    Bug catch: previous behaviour emitted WARNING on every fresh-cache run,
    drowning real signals.  Probe is source of truth on first-discover; the
    missing-flags condition only matters once a cached profile exists and a
    later run finds the engine no longer declares them (handled in verify()).
    """
    import logging

    from kinoforge.core.profiles import JsonProfileCache

    probe = _make_probe()
    backend = _CountingBackend(probe)
    key = _make_key()
    engine = _FakeEngine(flags_by_derive={})  # returns {} for every key

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    caplog.set_level(logging.DEBUG, logger="kinoforge.profiles")
    cache.discover(key, engine, backend)

    # No WARNING-or-higher records anywhere on the fresh-discovery path.
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), (
        f"unexpected WARNING-or-higher records: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )
    # Confirm the DEBUG message did fire and mentions the fresh-discovery context
    # (proves the codepath was actually exercised, not silently skipped).
    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "fresh-discovery" in msg and key.derive() in msg for msg in debug_messages
    ), (
        f"expected a DEBUG mentioning 'fresh-discovery' and {key.derive()!r}; "
        f"got: {debug_messages}"
    )


def test_no_debug_or_warning_when_one_flag_declared(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No noise (DEBUG nor WARNING) when at least one strategy flag is declared.

    Bug caught: if the guard emits the under-use message regardless of
    declared_flags content, the key's derive() would still show up in the
    DEBUG records — this assertion locks that out.
    """
    import logging

    from kinoforge.core.profiles import JsonProfileCache

    probe = _make_probe()
    backend = _CountingBackend(probe)
    key = _make_key()
    engine = _FakeEngine(
        flags_by_derive={key.derive(): {"supports_native_extension": True}}
    )

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    caplog.set_level(logging.DEBUG, logger="kinoforge.profiles")
    cache.discover(key, engine, backend)

    underuse_records = [
        r
        for r in caplog.records
        if key.derive() in r.message and "fresh-discovery" in r.message
    ]
    assert not underuse_records, (
        f"should not emit the under-use message when a flag is declared; "
        f"got: {[(r.levelname, r.message) for r in underuse_records]}"
    )


def test_verify_warns_when_cache_and_probe_disagree_and_engine_stopped_declaring(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """verify() must WARN when the cached strategy flags disagree with the fresh probe
    AND the engine no longer declares those flags.

    This is the genuine regression case: a cached profile was discovered when
    the engine declared strategy flags (so the merged profile carries the
    declared value), then the engine's declared_flags_map regressed to empty.
    The cached bits no longer reflect either the declared value (gone) or the
    probe (which now wins by default in discover()), so warm-attach should
    surface the drift.

    Bug catch: an over-eager noise reduction that drops the warning entirely
    would silence this legitimate regression signal.
    """
    import logging

    from kinoforge.core.profiles import JsonProfileCache

    # Cached profile carries True (e.g. discovered when declared_flags={"native": True})
    profile = _make_probe(supports_native_extension=True, supports_joint_audio=True)
    # Live probe reports False on both — engine no longer overrides probe
    backend = _CountingBackend(
        _make_probe(supports_native_extension=False, supports_joint_audio=False)
    )
    key = _make_key()
    # Engine returns empty dict — the "regression" scenario.
    engine = _FakeEngine(flags_by_derive={})

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    caplog.set_level(logging.WARNING, logger="kinoforge.profiles")
    cache.verify(profile, backend, engine=engine, key=key)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, (
        f"expected at least one WARNING on verify-side regression; "
        f"got: {[(r.levelname, r.message) for r in caplog.records]}"
    )
    # The WARNING should mention the drift, not generic capability mismatch.
    assert any(
        "no longer declares" in r.message and key.derive() in r.message
        for r in warning_records
    ), (
        f"expected WARNING to mention 'no longer declares' and the key hash; "
        f"got: {[r.message for r in warning_records]}"
    )


def test_verify_no_warning_when_engine_declares_no_flags_and_cache_matches_probe(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """verify() must NOT warn when engine declares no flags AND cache agrees with probe.

    This is the baseline state for engines like DiffusersEngine / FALEngine
    whose registry factories construct them without a declared_flags_map.
    `engine.declared_flags(key)` returns `{}` for every key. There is no drift
    — the cached profile's flag values were written from the probe in
    discover() and the fresh probe reports the same values.

    Bug catch: before the fix, this combination fired the warning on every
    warm-attach generation, training operators to ignore the line. Observed
    in production on 2026-06-28 against Wan 2.2 14B.
    """
    import logging

    from kinoforge.core.profiles import JsonProfileCache

    # Cache and probe agree exactly on flag fields — no drift.
    profile = _make_probe(supports_native_extension=False, supports_joint_audio=False)
    backend = _CountingBackend(
        _make_probe(supports_native_extension=False, supports_joint_audio=False)
    )
    key = _make_key()
    engine = _FakeEngine(flags_by_derive={})  # diffusers / fal baseline

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    caplog.set_level(logging.WARNING, logger="kinoforge.profiles")
    cache.verify(profile, backend, engine=engine, key=key)

    drift_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "no longer declares" in r.message
    ]
    assert not drift_warnings, (
        f"should not warn when there is no actual cache/probe drift; "
        f"got: {[(r.levelname, r.message) for r in drift_warnings]}"
    )


def test_verify_no_warning_when_engine_still_declares_flags(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """verify() must NOT warn when the engine still declares strategy flags.

    Bug catch: if the verify-side guard fires on every call regardless of
    declared_flags content, the WARNING becomes background noise again.
    """
    import logging

    from kinoforge.core.profiles import JsonProfileCache

    profile = _make_probe(supports_native_extension=True)
    backend = _CountingBackend(_make_probe(supports_native_extension=True))
    key = _make_key()
    engine = _FakeEngine(
        flags_by_derive={key.derive(): {"supports_native_extension": True}}
    )

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    caplog.set_level(logging.WARNING, logger="kinoforge.profiles")
    cache.verify(profile, backend, engine=engine, key=key)

    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "no longer declares" in r.message
    ]
    assert not warning_records, (
        f"should not warn when engine still declares strategy flags; "
        f"got: {[r.message for r in warning_records]}"
    )


def test_verify_without_engine_kwarg_does_not_raise(tmp_path: Path) -> None:
    """verify() must remain callable as verify(profile, backend) — engine is optional.

    Bug catch: if engine becomes required positional, every existing caller
    (orchestrator + tests above) breaks at runtime. Optionality preserves
    the ABC contract and lets callers that lack engine reference still verify.
    """
    from kinoforge.core.profiles import JsonProfileCache

    profile = _make_probe()
    backend = _CountingBackend(_make_probe())  # same probeable fields → no drift

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    # Must not raise on the engine-less call (also exercises the legacy ABC shape).
    cache.verify(profile, backend)


def test_verify_engine_without_key_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """verify(engine=..., key=None) must NOT emit the drift WARNING.

    Bug catch: a previous implementation synthesised a placeholder
    ``CapabilityKey`` from the profile name when ``key`` was omitted.  That
    key never indexes into a real ``declared_flags_map`` (which is keyed on
    engine + precision + LoRAs), so ``declared_flags()`` returned ``{}`` and
    the "no longer declares" WARNING fired spuriously.  The tightened
    precondition requires BOTH kwargs — when only ``engine`` is passed the
    drift check is silently skipped.
    """
    import logging

    from kinoforge.core.profiles import JsonProfileCache

    profile = _make_probe(supports_native_extension=True)
    backend = _CountingBackend(_make_probe(supports_native_extension=True))
    # Engine has flags for the real key, but we will not pass `key` at all.
    real_key = _make_key()
    engine = _FakeEngine(
        flags_by_derive={real_key.derive(): {"supports_native_extension": True}}
    )

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    caplog.set_level(logging.WARNING, logger="kinoforge.profiles")
    cache.verify(profile, backend, engine=engine)  # key intentionally omitted

    spurious = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "no longer declares" in r.message
    ]
    assert not spurious, (
        f"verify(engine=..., key=None) must skip the drift check silently "
        f"(no synthesised placeholder key); got: {[r.message for r in spurious]}"
    )


# ---------------------------------------------------------------------------
# AC 7 — verify(): matching probe passes; max_frames drift raises CapabilityMismatch;
#         flag drift is ignored
# ---------------------------------------------------------------------------


def test_verify_matching_probe_no_exception(tmp_path: Path) -> None:
    """verify() must not raise when the probe exactly matches the cached profile.

    Bug caught: if verify() always raises, this test fails (contrast with the
    drift test which must raise).
    """
    from kinoforge.core.profiles import JsonProfileCache

    profile = _make_probe(
        max_frames=24,
        fps=8,
        supported_modes={"t2v", "i2v"},
        max_resolution=(512, 512),
        supports_native_extension=True,  # flag value — must NOT be compared
    )
    # Backend returns same probeable fields but differs on flag (must be ignored).
    backend_probe = _make_probe(
        max_frames=24,
        fps=8,
        supported_modes={"t2v", "i2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,  # differs from profile — must be ignored
    )
    backend = _CountingBackend(backend_probe)

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    # Must not raise.
    cache.verify(profile, backend)


def test_verify_max_frames_drift_raises_capability_mismatch(tmp_path: Path) -> None:
    """verify() must raise CapabilityMismatch when max_frames differs.

    Bug caught: if verify() only checks fps/resolution and skips max_frames,
    the assertion fails.
    Expected error message contains "expected 24" and "got 32" (known from fixture).
    """
    from kinoforge.core.profiles import JsonProfileCache

    profile = _make_probe(max_frames=24)
    backend_probe = _make_probe(max_frames=32)  # drift
    backend = _CountingBackend(backend_probe)

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    with pytest.raises(CapabilityMismatch) as exc_info:
        cache.verify(profile, backend)

    msg = str(exc_info.value)
    assert "24" in msg and "32" in msg, (
        f"error message should mention expected (24) and actual (32); got: {msg!r}"
    )


def test_verify_flag_drift_does_not_raise(tmp_path: Path) -> None:
    """verify() must ignore flag fields; only probeable fields are compared.

    Bug caught: if verify() also compares supports_native_extension, this test
    fails because the profile has True but the probe returns False.
    """
    from kinoforge.core.profiles import JsonProfileCache

    profile = _make_probe(supports_native_extension=True, supports_joint_audio=True)
    # Same probeable fields, flags flipped — must NOT trigger a mismatch.
    backend_probe = _make_probe(
        supports_native_extension=False, supports_joint_audio=False
    )
    backend = _CountingBackend(backend_probe)

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    # Must not raise.
    cache.verify(profile, backend)


def test_verify_supported_modes_drift_raises(tmp_path: Path) -> None:
    """verify() must raise CapabilityMismatch when supported_modes differs.

    Bug caught: if supported_modes is not compared (e.g., excluded from
    probeable fields), silent capability regression would go undetected.
    """
    from kinoforge.core.profiles import JsonProfileCache

    profile = _make_probe(supported_modes={"t2v", "i2v"})
    backend_probe = _make_probe(supported_modes={"t2v"})  # i2v dropped
    backend = _CountingBackend(backend_probe)

    store = LocalArtifactStore(tmp_path)
    cache = JsonProfileCache(store=store)

    with pytest.raises(CapabilityMismatch):
        cache.verify(profile, backend)


# ---------------------------------------------------------------------------
# Cross-instance regression — exercises uri_for path for profile lookups
# ---------------------------------------------------------------------------


def test_resolve_works_across_jsonprofilecache_instances(tmp_path: Path) -> None:
    """A fresh JsonProfileCache reads a profile persisted by a prior instance.

    Bug this catches: the cache leaks _uri_index into the contract; restarting
    the process (a fresh cache pointed at the same store + run_id) breaks
    lookups. Pre-uri_for this worked only via hasattr(_path) peek; post-refactor
    it must work via store.uri_for(run_id, name).
    """
    from kinoforge.core.profiles import JsonProfileCache

    store = LocalArtifactStore(tmp_path)
    key = _make_key()
    probe = _make_probe(max_frames=24, fps=8)
    engine = _FakeEngine()
    backend = _CountingBackend(probe)

    cache_a = JsonProfileCache(store=store)
    persisted = cache_a.discover(key, engine, backend)

    # Brand-new cache instance on the same store + default run_id.
    cache_b = JsonProfileCache(store=store)
    recovered = cache_b.resolve(key)

    assert recovered == persisted


# ---------------------------------------------------------------------------
# _RecordingBackend — counts calls, shared across cross-process tests
# ---------------------------------------------------------------------------


class _RecordingBackend(GenerationBackend):
    """Records how many times inspect_capabilities was called; returns probe."""

    def __init__(self, probe: ModelProfile) -> None:
        self._probe = probe
        self.calls: int = 0

    def inspect_capabilities(self) -> ModelProfile:  # noqa: D102
        self.calls += 1
        return self._probe

    def capabilities(self) -> ModelProfile:  # noqa: D102
        return self._probe

    def submit(  # noqa: D102
        self,
        job: GenerationJob,
        *,
        cancel_token: object | None = None,
    ) -> str:
        raise NotImplementedError

    def result(  # noqa: D102
        self,
        job_id: str,
        *,
        cancel_token: object | None = None,
    ) -> Any:
        raise NotImplementedError

    def endpoints(self) -> dict[str, str]:  # noqa: D102
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Cross-process single-flight (Layer H)
# ---------------------------------------------------------------------------


def test_cross_process_single_flight_one_probe(tmp_path: Path) -> None:
    """Two JsonProfileCache instances sharing a store + lock registry probe once.

    Without the outer lock, both caches would see the miss, both would
    call inspect_capabilities, and both would persist — last writer wins
    but both incurred the cost.  This test asserts the outer lock
    serializes the two instances and only one probe runs.
    """
    from kinoforge.core.clock import FakeClock
    from kinoforge.core.locks import InMemoryLock
    from kinoforge.core.profiles import JsonProfileCache
    from kinoforge.stores.local import LocalArtifactStore

    registry: dict[str, dict[str, Any]] = {}
    clock = FakeClock(start=0.0)

    class _LockingStore(LocalArtifactStore):
        def acquire_lock(self, key: str, *, ttl_s: float) -> InMemoryLock:  # noqa: D102
            return InMemoryLock(
                key=key,
                ttl_s=ttl_s,
                registry=registry,
                clock=clock,
                sleep=lambda _: clock.advance(0.01),
            )

    store = _LockingStore(tmp_path)
    cache_a = JsonProfileCache(store)
    cache_b = JsonProfileCache(store)

    probe = _make_probe()
    backend_a = _RecordingBackend(probe=probe)
    backend_b = _RecordingBackend(probe=probe)
    engine = _FakeEngine()
    key = _make_key()

    # Serialised execution: cache_a discovers first, cache_b sees cache hit.
    profile_a = cache_a.resolve_or_discover(key, engine, backend_a)
    profile_b = cache_b.resolve_or_discover(key, engine, backend_b)

    assert profile_a == profile_b
    total_probes = backend_a.calls + backend_b.calls
    assert total_probes == 1, f"expected 1 probe across both caches, got {total_probes}"


def test_cache_hit_fast_path_skips_lock(tmp_path: Path) -> None:
    """resolve() success must not call acquire_lock.

    Spec §5.1 says cache hits take no lock.  Otherwise every cache hit
    pays a CAS round-trip in production, defeating the whole point of
    the cache.
    """
    from kinoforge.core.locks import InMemoryLock
    from kinoforge.core.profiles import JsonProfileCache
    from kinoforge.stores.local import LocalArtifactStore

    lock_calls: list[str] = []

    class _CountingStore(LocalArtifactStore):
        def acquire_lock(self, key: str, *, ttl_s: float) -> InMemoryLock:  # noqa: D102
            lock_calls.append(key)
            return InMemoryLock(key=key, ttl_s=ttl_s, registry={})

    store = _CountingStore(tmp_path)
    cache = JsonProfileCache(store)
    probe = _make_probe()
    backend = _RecordingBackend(probe=probe)
    engine = _FakeEngine()
    key = _make_key()

    cache.resolve_or_discover(key, engine, backend)
    pre_count = len(lock_calls)

    # Second call should be a cache hit and acquire NO lock.
    cache.resolve_or_discover(key, engine, backend)
    assert len(lock_calls) == pre_count, (
        f"cache-hit fast path took {len(lock_calls) - pre_count} extra locks"
    )
