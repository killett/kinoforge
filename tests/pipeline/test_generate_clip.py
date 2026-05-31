"""Tests for GenerateClipStage — end-to-end single-clip happy path.

AC 5: t2v request via FakeEngine produces Artifact whose uri exists in LocalArtifactStore;
      same inputs yield same uri (determinism).
AC 6: supports_native_extension=True → 1 job (all segments); False → N jobs (one per segment).
      Exercised via segments_override with 3 segments; both branches → submit call count asserted.
AC 7: Unsupported mode raises ValidationError before backend.submit.
AC 8: store.get_bytes(artifact.uri) returns original engine output bytes (round-trip).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    GenerationEngine,
    GenerationJob,
    GenerationRequest,
    ModelProfile,
    Segment,
)
from kinoforge.core.pool import SequentialPool
from kinoforge.engines.fake import FakeBackend
from kinoforge.pipeline.generate_clip import GenerateClipStage
from kinoforge.stores.local import LocalArtifactStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(*, supports_native_extension: bool = False) -> ModelProfile:
    return ModelProfile(
        name="test-model",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=supports_native_extension,
        supports_joint_audio=False,
    )


def _make_stage(
    tmp_path: Path,
    *,
    profile: ModelProfile,
    backend: FakeBackend,
    run_id: str = "run-001",
    engine: object | None = None,
) -> GenerateClipStage:
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    if engine is None:
        engine = _fake_engine_for_tests(profile)
    return GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# AC 5: happy path — artifact uri exists in store; re-run produces same uri
# ---------------------------------------------------------------------------


def test_happy_path_artifact_stored_and_uri_exists(tmp_path: Path) -> None:
    """t2v request produces Artifact; its uri points to an existing file."""
    profile = _profile()
    backend = FakeBackend(probe=profile)
    stage = _make_stage(tmp_path, profile=profile, backend=backend)

    request = GenerationRequest(prompt="a red sunset", mode="t2v")
    result = stage.run(request)

    assert result.uri != ""
    assert Path(result.uri).exists()


def test_same_inputs_produce_same_uri(tmp_path: Path) -> None:
    """Deterministic: identical request through same profile → same stored name."""
    profile = _profile()

    # Run 1
    backend1 = FakeBackend(probe=profile)
    stage1 = _make_stage(
        tmp_path / "r1", profile=profile, backend=backend1, run_id="r1"
    )
    result1 = stage1.run(GenerationRequest(prompt="consistent", mode="t2v"))

    # Run 2 — fresh backend but same logic
    backend2 = FakeBackend(probe=profile)
    stage2 = _make_stage(
        tmp_path / "r2", profile=profile, backend=backend2, run_id="r1"
    )
    result2 = stage2.run(GenerationRequest(prompt="consistent", mode="t2v"))

    # URIs differ only in root path prefix; the filename portion is identical
    assert Path(result1.uri).name == Path(result2.uri).name


# ---------------------------------------------------------------------------
# AC 6: native-extension branch vs fallback branch — submit call counting
# ---------------------------------------------------------------------------


class CountingBackend(FakeBackend):
    """FakeBackend that tracks how many times submit() is called."""

    def __init__(self, probe: ModelProfile) -> None:
        super().__init__(probe=probe)
        self.submit_count = 0

    def submit(self, job: GenerationJob) -> str:
        self.submit_count += 1
        return super().submit(job)


def test_native_extension_true_one_job_for_n_segments(tmp_path: Path) -> None:
    """supports_native_extension=True → 1 call to backend.submit for 3 segments."""
    profile = _profile(supports_native_extension=True)
    backend = CountingBackend(probe=profile)
    stage = _make_stage(tmp_path, profile=profile, backend=backend)

    segments = [Segment(prompt=f"segment {i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored when override set", mode="t2v"),
        segments_override=segments,
    )

    assert backend.submit_count == 1


def test_native_extension_false_n_jobs_for_n_segments(tmp_path: Path) -> None:
    """supports_native_extension=False → N calls to backend.submit for N segments."""
    profile = _profile(supports_native_extension=False)
    backend = CountingBackend(probe=profile)
    stage = _make_stage(tmp_path, profile=profile, backend=backend)

    n = 3
    segments = [Segment(prompt=f"segment {i}") for i in range(n)]
    stage.run(
        GenerationRequest(prompt="ignored when override set", mode="t2v"),
        segments_override=segments,
    )

    assert backend.submit_count == n


# ---------------------------------------------------------------------------
# Continuity (Layer B) — chain tail-frame into next segment's init_image
# ---------------------------------------------------------------------------


class RecordingBackend(FakeBackend):
    """FakeBackend that records each submitted job's seg-0 assets."""

    def __init__(self, probe: ModelProfile) -> None:
        super().__init__(probe=probe)
        self.submitted_seg0_assets: list[list] = []  # type: ignore[type-arg]

    def submit(self, job: GenerationJob) -> str:
        # Capture a snapshot of the first segment's assets at submit time.
        self.submitted_seg0_assets.append(list(job.segments[0].assets))
        return super().submit(job)


def _fake_engine_for_tests(probe: ModelProfile) -> GenerationEngine:
    """Construct a FakeEngine with no declared flags and no required spec keys."""
    from kinoforge.engines.fake import FakeEngine

    return FakeEngine(
        probe_profile=probe,
        declared_flags_map={},
        required_spec_keys=set(),
    )


def test_stage_non_native_i2v_n3_chains_segs_1_and_2(tmp_path: Path) -> None:
    """Non-native + i2v + 3 segments → jobs 1 and 2 receive prev tail as init_image.

    Bug this catches: chain skips a segment, or order is wrong (e.g. seg 1 gets
    seg 0's tail but seg 2 also gets seg 0's tail instead of seg 1's).
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _fake_engine_for_tests(profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="chain-i2v",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
    )

    segments = [Segment(prompt=f"segment {i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v"),
        segments_override=segments,
    )

    # Job 0: no chain (no prior render to extract from).
    assert backend.submitted_seg0_assets[0] == []
    # Jobs 1 and 2: exactly one ConditioningAsset, role=init_image, kind=image.
    for i in (1, 2):
        assets = backend.submitted_seg0_assets[i]
        assert len(assets) == 1
        asset = assets[0]
        assert asset.kind == "image"
        assert asset.role == "init_image"
        # URI contains the tail PNG name under the stage's run_id namespace.
        assert asset.ref.uri.endswith("-tail.png")
        assert asset.ref.filename == f"seg-{i - 1}-tail.png"


# ---------------------------------------------------------------------------
# AC 7: unsupported mode raises ValidationError before any submit
# ---------------------------------------------------------------------------


def test_unsupported_mode_raises_before_submit(tmp_path: Path) -> None:
    """ValidationError on bad mode; backend.submit is never called."""
    profile = _profile()  # supported_modes={"t2v"}
    backend = CountingBackend(probe=profile)
    stage = _make_stage(tmp_path, profile=profile, backend=backend)

    with pytest.raises(ValidationError):
        stage.run(GenerationRequest(prompt="test", mode="unsupported"))

    assert backend.submit_count == 0


# ---------------------------------------------------------------------------
# AC 8: round-trip — store.get_bytes returns original engine output bytes
# ---------------------------------------------------------------------------


def test_round_trip_bytes(tmp_path: Path) -> None:
    """store.get_bytes(artifact.uri) returns the bytes the stage stored."""
    profile = _profile()
    backend = FakeBackend(probe=profile)
    store = LocalArtifactStore(tmp_path)
    pool = SequentialPool(backend)
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="rt-test",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=_fake_engine_for_tests(profile),
    )

    request = GenerationRequest(prompt="round trip check", mode="t2v")
    artifact = stage.run(request)

    retrieved = store.get_bytes(artifact.uri)
    assert len(retrieved) > 0

    # Bytes are deterministic: repeating with a fresh backend gives same bytes
    backend2 = FakeBackend(probe=profile)
    store2 = LocalArtifactStore(tmp_path / "r2")
    pool2 = SequentialPool(backend2)
    stage2 = GenerateClipStage(
        profile=profile,
        pool=pool2,
        store=store2,
        run_id="rt-test",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=_fake_engine_for_tests(profile),
    )
    artifact2 = stage2.run(GenerationRequest(prompt="round trip check", mode="t2v"))
    retrieved2 = store2.get_bytes(artifact2.uri)

    assert retrieved == retrieved2


class _CountingExtractEngine:
    """Wraps a FakeEngine; counts extract_last_frame calls for no-chain tests.

    The stage type-hints `engine: GenerationEngine`; we duck-type since the
    only engine method the stage calls is extract_last_frame.
    """

    def __init__(self, probe: ModelProfile) -> None:
        from kinoforge.engines.fake import FakeEngine

        self._inner = FakeEngine(
            probe_profile=probe,
            declared_flags_map={},
            required_spec_keys=set(),
        )
        self.extract_calls = 0

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        self.extract_calls += 1
        return self._inner.extract_last_frame(artifact)


def test_stage_native_branch_i2v_no_chain(tmp_path: Path) -> None:
    """Native branch (1 job) + i2v → chain never triggers (i > 0 never true).

    Bug this catches: chain accidentally runs on N=1 jobs, calling extract_last_frame
    on the (nonexistent) prior render.
    """
    profile = _profile(supports_native_extension=True)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _CountingExtractEngine(probe=profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="native-i2v",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,  # type: ignore[arg-type]
    )

    segments = [Segment(prompt=f"seg {i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v"),
        segments_override=segments,
    )

    # Native branch: 1 job submitted.
    assert len(backend.submitted_seg0_assets) == 1
    assert engine.extract_calls == 0


def test_stage_non_native_t2v_n3_no_chain(tmp_path: Path) -> None:
    """Non-native + t2v + 3 segments → no chain (no init_image in t2v role contract).

    Bug this catches: chain mistakenly triggers for modes that don't accept
    init_image, breaking validate_spec or producing wrong-shape jobs.
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _CountingExtractEngine(probe=profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="t2v-no-chain",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,  # type: ignore[arg-type]
    )

    segments = [Segment(prompt=f"seg {i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored", mode="t2v"),
        segments_override=segments,
    )

    # 3 jobs submitted; all with empty seg-0 assets; engine never invoked.
    assert len(backend.submitted_seg0_assets) == 3
    for assets in backend.submitted_seg0_assets:
        assert assets == []
    assert engine.extract_calls == 0


def test_stage_non_native_i2v_n1_no_chain(tmp_path: Path) -> None:
    """Non-native + i2v + 1 segment → no chain (i > 0 never true).

    Bug this catches: off-by-one tries to inject on the first segment, calling
    extract_last_frame with no prior artifact.
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _CountingExtractEngine(probe=profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="i2v-n1",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,  # type: ignore[arg-type]
    )

    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v"),
        segments_override=[Segment(prompt="only")],
    )

    assert len(backend.submitted_seg0_assets) == 1
    assert engine.extract_calls == 0


def test_stage_chain_persists_tail_via_store(tmp_path: Path) -> None:
    """Non-native chain writes one tail PNG per gap via store.put_bytes,
    under the stage's run_id namespace, with name 'seg-<i>-tail.png'.

    Bug this catches: stage skips persistence, persists under wrong run_id,
    or names files inconsistently — breaking `kinoforge gc --run` cleanup.
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _fake_engine_for_tests(profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="run-persist",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
    )

    segments = [Segment(prompt=f"seg {i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v"),
        segments_override=segments,
    )

    listed = store.list("run-persist")
    tails = sorted(n for n in listed if n.endswith("-tail.png"))
    # 3 segments → 2 chain gaps → 2 tail PNGs.
    assert tails == ["seg-0-tail.png", "seg-1-tail.png"]

    # Bytes round-trip: store returned the FakeEngine's deterministic bytes
    # derived from each prior segment's BACKEND-OUTPUT filename (which is
    # sha256-of-job for FakeBackend — segment-specific).
    seg0_tail_bytes = store.get_bytes(store.uri_for("run-persist", "seg-0-tail.png"))
    seg1_tail_bytes = store.get_bytes(store.uri_for("run-persist", "seg-1-tail.png"))

    assert seg0_tail_bytes.startswith(b"FAKE_TAIL:")
    assert seg1_tail_bytes.startswith(b"FAKE_TAIL:")
    # Off-by-one in results[-1] indexing (e.g. always using results[0]) would
    # leave both tail PNGs identical. They MUST differ because each is derived
    # from a different prior segment's artifact filename.
    assert seg0_tail_bytes != seg1_tail_bytes
