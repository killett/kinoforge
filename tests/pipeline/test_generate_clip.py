"""Tests for GenerateClipStage — end-to-end single-clip happy path.

AC 5: t2v request via FakeEngine produces Artifact whose uri exists in LocalArtifactStore;
      same inputs yield same uri (determinism).
AC 6: supports_native_extension=True → 1 job (all segments); False → N jobs (one per segment).
      Exercised via segments_override with 3 segments; both branches → submit call count asserted.
AC 7: Unsupported mode raises ValidationError before backend.submit.
AC 8: store.get_bytes(artifact.uri) returns original engine output bytes (round-trip).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationEngine,
    GenerationJob,
    GenerationRequest,
    ModelProfile,
    Segment,
)
from kinoforge.core.pool import ConcurrentPool, SequentialPool
from kinoforge.engines.fake import FakeBackend
from kinoforge.pipeline.generate_clip import GenerateClipStage
from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local import LocalArtifactStore
from tests.core.conftest import BlockingFakeBackend

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
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None,
    result_override: Callable[[str], Artifact] | None = None,
) -> GenerateClipStage:
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    if engine is None:
        engine = _fake_engine_for_tests(profile)
    if result_override is not None:
        backend.result = result_override  # type: ignore[assignment]
    return GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,  # type: ignore[arg-type]
        http_get_bytes=http_get_bytes,
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
    only engine methods the stage calls are validate_spec and
    extract_last_frame.
    """

    def __init__(self, probe: ModelProfile) -> None:
        from kinoforge.engines.fake import FakeEngine

        self._inner = FakeEngine(
            probe_profile=probe,
            declared_flags_map={},
            required_spec_keys=set(),
        )
        self.extract_calls = 0

    def validate_spec(self, job: GenerationJob) -> None:
        """No-op: these tests focus on extract_last_frame call counting."""

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


# ---------------------------------------------------------------------------
# Layer F: GenerateClipStage calls engine.validate_spec after inject_tail_frame
# on each chained segment so misconfigured asset_node_ids / asset_paths surface
# before the engine HTTP round-trip.
# ---------------------------------------------------------------------------


class _ValidatingEngine:
    """Engine spy that records validate_spec calls and (optionally) raises.

    Wraps a real FakeEngine for extract_last_frame deterministic bytes; the
    stage only needs ``validate_spec`` and ``extract_last_frame`` from the
    engine surface, so we duck-type the rest.

    Args:
        probe: ModelProfile to forward into the inner FakeEngine.
        raise_on_validate_call: 1-based index of the validate_spec call that
            should raise ValidationError; ``None`` disables raising.
    """

    def __init__(
        self,
        probe: ModelProfile,
        *,
        raise_on_validate_call: int | None = None,
    ) -> None:
        from kinoforge.engines.fake import FakeEngine

        self._inner = FakeEngine(
            probe_profile=probe,
            declared_flags_map={},
            required_spec_keys=set(),
        )
        self.validate_calls: list[GenerationJob] = []
        self.raise_on_validate_call = raise_on_validate_call

    def validate_spec(self, job: GenerationJob) -> None:
        self.validate_calls.append(job)
        if (
            self.raise_on_validate_call is not None
            and len(self.validate_calls) == self.raise_on_validate_call
        ):
            raise ValidationError("simulated misconfig")

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        return self._inner.extract_last_frame(artifact)


def test_stage_calls_validate_spec_on_each_chained_job(tmp_path: Path) -> None:
    """3 segments → upfront loop (3 raw) + 2 chain hops → 5 total validate_spec calls.

    Layer K Task 2 adds an upfront validate_spec loop for every raw job before
    any dispatch, so all three jobs are validated once before the chained serial
    loop, and then the two chained jobs (i > 0) are validated again post
    inject_tail_frame (covering the asset_node_ids / asset_paths invariant).

    Bug catches:
    - pre-Layer-F (no post-chain validate_spec) yields 3 (upfront only).
    - a single trailing validate_spec after the loop yields 4.
    - missing upfront loop yields only 2.
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _ValidatingEngine(probe=profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="vs-count",
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

    # 3 upfront (raw) + 2 post-inject (chained) = 5 total
    assert len(engine.validate_calls) == 5


def test_stage_validates_chained_job_carries_injected_asset(tmp_path: Path) -> None:
    """The job handed to the post-inject validate_spec call carries the
    tail-frame in segments[0].assets; the upfront calls see empty assets.

    With the Layer K Task 2 upfront loop: 2 segments → 2 upfront calls (raw,
    empty assets) + 1 post-inject call (chained, injected asset) = 3 total.

    Bug catches:
    - missing post-inject validate_spec means 2 calls total (no asset-bearing call).
    - injecting a stale prior-segment asset would still pass this for 2-segment
      runs but is covered by the Layer E chain test ordering assertions.
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _ValidatingEngine(probe=profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="vs-asset",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,  # type: ignore[arg-type]
    )

    segments = [Segment(prompt="p-0"), Segment(prompt="p-1")]
    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v"),
        segments_override=segments,
    )

    # 2 upfront (raw, empty assets) + 1 post-inject (chained, with asset) = 3 total.
    assert len(engine.validate_calls) == 3
    # The first two calls are the upfront loop: raw jobs, no assets.
    for raw_call in engine.validate_calls[:2]:
        assert raw_call.segments[0].assets == []
    # The third call is the post-inject chained call: must carry the tail asset.
    chained_job = engine.validate_calls[2]
    chained_assets = chained_job.segments[0].assets
    assert len(chained_assets) == 1
    asset = chained_assets[0]
    assert asset.role == "init_image"
    assert asset.kind == "image"
    # The injected URI lives under the stage's run_id namespace.
    assert "vs-asset" in asset.ref.uri
    # Sanity: the same chained job (with asset) reached backend.submit afterwards.
    submitted_chain_assets = backend.submitted_seg0_assets[1]
    assert len(submitted_chain_assets) == 1
    assert submitted_chain_assets[0].role == "init_image"


def test_stage_aborts_when_validate_spec_raises_on_chained_segment(
    tmp_path: Path,
) -> None:
    """validate_spec raising on the first chained (post-inject) call aborts the
    stage: all 3 upfront validations passed, seg-0 was submitted, seg-1's
    post-inject validate_spec raises before submit, seg-2 is never reached.

    With the Layer K Task 2 upfront loop, 3 raw jobs are validated first (calls
    1-3 succeed). Calls 4+ are the post-inject chained calls. Raising on call 4
    (= first chained, seg-1 post-inject) allows seg-0 to submit first.

    Bug catches:
    - swallowing ValidationError would have run all 3 submissions.
    - validating AFTER pool.submit on the chained iter would have allowed
      submitted to reach 2 before raising.
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    # Raise on call 4 (= 3 upfront + first post-inject chained call for seg-1).
    engine = _ValidatingEngine(probe=profile, raise_on_validate_call=4)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="vs-abort",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,  # type: ignore[arg-type]
    )

    segments = [Segment(prompt=f"seg {i}") for i in range(3)]
    with pytest.raises(ValidationError, match="simulated misconfig"):
        stage.run(
            GenerationRequest(prompt="ignored", mode="i2v"),
            segments_override=segments,
        )

    # seg-0 was submitted (pre-chain), seg-1 raised before submit, seg-2 never reached.
    assert len(backend.submitted_seg0_assets) == 1
    # 3 upfront (pass) + 1 post-inject (raises) = 4 total validate_spec calls.
    assert len(engine.validate_calls) == 4


# ---------------------------------------------------------------------------
# Layer G: ConcurrentPool branch coverage
# ---------------------------------------------------------------------------


def _stage_with_concurrent_pool(
    backend: BlockingFakeBackend,
    *,
    cap: int,
    mode: str,
    profile: ModelProfile,
    store: ArtifactStore,
) -> tuple[GenerateClipStage, ConcurrentPool]:
    """Build a stage backed by ConcurrentPool(cap) with a non-chaining engine.

    The engine is FakeEngine so extract_last_frame is concrete; the backend is
    the supplied BlockingFakeBackend, registered into a fresh ConcurrentPool.
    """
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=cap)
    from kinoforge.engines.fake import FakeEngine

    fake_engine = FakeEngine(
        probe_profile=profile,
        declared_flags_map={},
        required_spec_keys=set(),
    )
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="r-concurrent",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=fake_engine,
    )
    return stage, pool


def test_unchained_branch_uses_pool_map_parallel_dispatch(tmp_path: Path) -> None:
    """t2v 3-segment fallback: all 3 jobs reach backend.submit before any release."""
    probe = _profile()
    store = LocalArtifactStore(root=tmp_path)
    backend = BlockingFakeBackend()
    stage, pool = _stage_with_concurrent_pool(
        backend, cap=3, mode="t2v", profile=probe, store=store
    )

    segments = [Segment(prompt=f"p{i}", assets=[]) for i in range(3)]
    request = GenerationRequest(prompt="x", mode="t2v")

    # Spawn a releaser thread that waits until all 3 reach backend.submit,
    # then releases them.  If the stage were serial, only 1 would arrive
    # before any release — the releaser's wait would time out.
    releaser_saw_three = threading.Event()

    def _releaser() -> None:
        for _ in range(100):
            if len(backend.submit_log) >= 3:
                releaser_saw_three.set()
                break
            time.sleep(0.01)
        for jid in list(backend._gates.keys()):
            backend.release(jid)

    threading.Thread(target=_releaser, daemon=True).start()
    try:
        result = stage.run(request, segments_override=segments)
        assert result is not None
    finally:
        pool.close()
    assert releaser_saw_three.is_set(), (
        "stage did not dispatch all 3 jobs in parallel — branch missed"
    )


def test_chained_branch_remains_serial_under_concurrent_pool(tmp_path: Path) -> None:
    """i2v 3-segment chain: each backend.submit preceded by prior release.

    We assert serialness by observing that at any moment, submit_log has
    at most one MORE entry than the number of completed releases.
    """
    probe = _profile()  # standard probe; i2v chaining triggers via
    # MODE_ROLE_REQUIREMENTS["i2v"] = {"init_image"} in interfaces.py,
    # not via profile config. Mirror the existing chained-3-segment
    # test at tests/pipeline/test_generate_clip.py:190.
    store = LocalArtifactStore(root=tmp_path)
    backend = BlockingFakeBackend()
    stage, pool = _stage_with_concurrent_pool(
        backend, cap=3, mode="i2v", profile=probe, store=store
    )

    # Seed asset for seg 0.
    seed_uri = store.put_bytes("r-concurrent", "seed.png", b"seed").uri
    seed_asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="seed.png", uri=seed_uri),
    )
    segments = [
        Segment(prompt="p0", assets=[seed_asset]),
        Segment(prompt="p1", assets=[]),
        Segment(prompt="p2", assets=[]),
    ]
    request = GenerationRequest(prompt="x", mode="i2v", assets=[seed_asset])

    released_count = [0]

    def _releaser() -> None:
        # Watchdog: every 10ms, release one more job if there's a pending one.
        for _ in range(200):
            if len(backend.submit_log) > released_count[0]:
                # Assert serial: only 1 ahead of released.
                assert len(backend.submit_log) - released_count[0] == 1, (
                    f"chained branch ran in parallel: "
                    f"submitted={backend.submit_log}, released={released_count[0]}"
                )
                # Release the most recent.
                with backend._lock:
                    last_jid = backend.submit_log[-1]
                backend.release(last_jid)
                released_count[0] += 1
                if released_count[0] >= 3:
                    break
            time.sleep(0.01)

    threading.Thread(target=_releaser, daemon=True).start()
    try:
        result = stage.run(request, segments_override=segments)
        assert result is not None
    finally:
        pool.close()
    assert released_count[0] == 3


def test_one_job_native_skips_map_uses_submit(tmp_path: Path) -> None:
    """Native single-job path uses pool.submit().result(), not pool.map()."""
    probe = _profile()
    store = LocalArtifactStore(root=tmp_path)
    backend = BlockingFakeBackend()
    stage, pool = _stage_with_concurrent_pool(
        backend, cap=3, mode="t2v", profile=probe, store=store
    )

    # Single segment — strategy.decide produces 1 job for native or
    # 1-segment fallback; either way len(jobs) == 1.
    segments = [Segment(prompt="solo", assets=[])]
    request = GenerationRequest(prompt="x", mode="t2v")

    def _releaser() -> None:
        for _ in range(50):
            if backend.submit_log:
                break
            time.sleep(0.01)
        for jid in list(backend._gates.keys()):
            backend.release(jid)

    # Spy on pool.map so we can prove the 1-job path does NOT call it.
    # Without the spy, len(submit_log) == 1 would pass equally for
    # pool.submit(j).result() AND pool.map([j]) — both produce one
    # backend.submit call. The spy is what makes the assertion discriminating.
    map_calls: list[list[GenerationJob]] = []
    original_map = pool.map

    def _spy_map(jobs: list[GenerationJob]) -> list[Artifact]:
        map_calls.append(list(jobs))
        return original_map(jobs)

    pool.map = _spy_map  # type: ignore[method-assign]

    threading.Thread(target=_releaser, daemon=True).start()
    try:
        result = stage.run(request, segments_override=segments)
        assert result is not None
    finally:
        pool.map = original_map  # type: ignore[method-assign]
        pool.close()
    # Only one job should have been submitted via pool.submit; map untouched.
    assert len(backend.submit_log) == 1
    assert map_calls == [], (
        f"pool.map was called for 1-job path; len(jobs) > 1 guard broke: {map_calls}"
    )


# ---------------------------------------------------------------------------
# _artifact_bytes resolution: local file > http(s) URL > synthetic fallback
# ---------------------------------------------------------------------------


def test_artifact_bytes_reads_local_file_when_uri_points_at_file(
    tmp_path: Path,
) -> None:
    """A backend Artifact whose uri points at an existing local file is read
    verbatim — the synthetic-bytes path is not triggered.

    Bug catch: a hosted engine that writes its result to a tempfile and
    sets ``Artifact.uri`` to that path would otherwise have its real bytes
    silently replaced with the FakeEngine debug synthesis.
    """
    profile = _profile()
    stage = _make_stage(tmp_path, profile=profile, backend=FakeBackend(probe=profile))
    payload = b"\x00\x00\x00\x18ftypiso5...real bytes..."
    src = tmp_path / "real.mp4"
    src.write_bytes(payload)
    artifact = Artifact(filename="real.mp4", uri=str(src))
    assert stage._artifact_bytes(artifact) == payload


def test_artifact_bytes_downloads_when_url_is_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend Artifact with an http(s) ``url`` is downloaded with urlopen.

    Bug catch: returning synthetic ``filename|meta`` bytes for hosted engines
    means the stored file is a debug stub rather than the real artifact —
    the very failure that the Layer I live smoke surfaced.
    """
    profile = _profile()
    stage = _make_stage(tmp_path, profile=profile, backend=FakeBackend(probe=profile))
    expected = b"\x00\x00\x00\x18ftypiso5\x00\x00\x00\x00downloaded"
    calls: list[str] = []

    class _Resp:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    import urllib.request as _urllib_request

    def _fake_urlopen(req: object) -> _Resp:
        # Layer M: _artifact_bytes routes through _default_http_get_bytes which
        # builds a urllib.request.Request; accept both str and Request so this
        # test stays resilient to the seam implementation detail.
        url_str = req.full_url if isinstance(req, _urllib_request.Request) else str(req)
        calls.append(url_str)
        return _Resp(expected)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen, raising=True)
    artifact = Artifact(filename="out.mp4", url="https://example.com/x.mp4")
    assert stage._artifact_bytes(artifact) == expected
    assert calls == ["https://example.com/x.mp4"]


def test_artifact_bytes_synthetic_fallback_when_no_uri_or_url(
    tmp_path: Path,
) -> None:
    """With neither uri nor url, fall back to FakeEngine-style synthetic bytes.

    Bug catch: removing the synthetic path would break the FakeEngine unit
    test suite, which relies on deterministic bytes derived from filename+meta.
    """
    profile = _profile()
    stage = _make_stage(tmp_path, profile=profile, backend=FakeBackend(probe=profile))
    artifact = Artifact(filename="x.mp4", meta={"k": "v"})
    expected = b"x.mp4" + b"|" + repr(sorted({"k": "v"}.items())).encode("utf-8")
    assert stage._artifact_bytes(artifact) == expected


# ---------------------------------------------------------------------------
# Layer M Task 4 — http_get_bytes seam + Artifact.headers passthrough
# ---------------------------------------------------------------------------


def test_layer_m_seam_receives_artifact_headers(tmp_path: Path) -> None:
    """Spy seam receives the exact Artifact.headers dict.

    Bug catch: pipeline filters, strips, or wraps the headers before
    handing them to the downloader, silently dropping Authorization
    for engines that depend on it.
    """
    captured: dict[str, object] = {}

    def spy_http_get_bytes(url: str, headers: dict[str, str]) -> bytes:
        captured["url"] = url
        captured["headers"] = headers
        return b"downloaded-bytes"

    profile = _profile()
    expected_headers = {"Authorization": "Bearer test-token-9001"}
    backend = FakeBackend(probe=profile)
    stage = _make_stage(
        tmp_path,
        profile=profile,
        backend=backend,
        run_id="run-headers",
        http_get_bytes=spy_http_get_bytes,
        result_override=lambda job_id: Artifact(
            filename="clip.mp4",
            url="https://example.com/media/clip.mp4",
            headers=expected_headers,
        ),
    )

    request = GenerationRequest(mode="t2v", prompt="hi", assets=[])
    stage.run(request)

    assert captured["url"] == "https://example.com/media/clip.mp4"
    assert captured["headers"] == expected_headers


def test_layer_m_seam_receives_empty_dict_not_none(tmp_path: Path) -> None:
    """No populated Artifact.headers → spy receives {}, not None.

    Bug catch: shape drift from ``dict`` to ``Optional[dict]`` would
    break consumers that always call ``headers.items()``.
    """
    captured: dict[str, object] = {}

    def spy_http_get_bytes(url: str, headers: dict[str, str]) -> bytes:
        captured["headers"] = headers
        return b"downloaded"

    profile = _profile()
    backend = FakeBackend(probe=profile)
    stage = _make_stage(
        tmp_path,
        profile=profile,
        backend=backend,
        run_id="run-empty",
        http_get_bytes=spy_http_get_bytes,
        result_override=lambda job_id: Artifact(
            filename="clip.mp4",
            url="https://example.com/media/clip.mp4",
        ),
    )

    request = GenerationRequest(mode="t2v", prompt="hi", assets=[])
    stage.run(request)

    assert captured["headers"] == {}
    assert captured["headers"] is not None


def test_layer_m_file_uri_bypasses_seam(tmp_path: Path) -> None:
    """Artifact(uri='file://...') bypasses the seam entirely.

    Bug catch: a refactor that always routes through the seam would
    HTTP-fetch local files, regressing LocalArtifactStore + FakeEngine.
    """
    seam_called = {"count": 0}

    def spy_http_get_bytes(url: str, headers: dict[str, str]) -> bytes:
        seam_called["count"] += 1
        return b"should-not-be-called"

    # Write a real local file the artifact will point at.
    local_clip = tmp_path / "local.mp4"
    local_clip.write_bytes(b"local-bytes")

    profile = _profile()
    backend = FakeBackend(probe=profile)
    stage = _make_stage(
        tmp_path / "store",
        profile=profile,
        backend=backend,
        run_id="run-local",
        http_get_bytes=spy_http_get_bytes,
        result_override=lambda job_id: Artifact(
            filename="local.mp4",
            uri=local_clip.as_uri(),
        ),
    )

    request = GenerationRequest(mode="t2v", prompt="hi", assets=[])
    stored = stage.run(request)

    # The point: the seam was NOT consulted because the file:// branch
    # short-circuited to Path.read_bytes(). Round-trip equality is covered
    # by other AC tests; don't re-assert it here.
    assert seam_called["count"] == 0
    assert stored.uri  # store returned a valid URI for the persisted bytes


def test_layer_m_default_seam_passes_headers_to_urllib_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default seam (no override) builds a urllib Request carrying the headers.

    Bug catch: production path skips headers because the seam is only
    consulted in tests; default falls back to bare ``urlopen(url)``
    instead of ``urlopen(Request(url, headers=...))``.
    """
    import urllib.request

    captured: dict[str, object] = {}

    class _FakeResp:
        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b"downloaded-default"

    def fake_urlopen(req: object) -> _FakeResp:
        # urllib.request.urlopen accepts either a str or a Request; we expect
        # a Request because our default seam wraps the url+headers.
        assert isinstance(req, urllib.request.Request)
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    profile = _profile()
    backend = FakeBackend(probe=profile)
    stage = _make_stage(
        tmp_path,
        profile=profile,
        backend=backend,
        run_id="run-default-seam",
        # No http_get_bytes override — exercises the module-level default.
        result_override=lambda job_id: Artifact(
            filename="clip.mp4",
            url="https://example.com/media/clip.mp4",
            headers={"Authorization": "Bearer default-seam-token"},
        ),
    )

    request = GenerationRequest(mode="t2v", prompt="hi", assets=[])
    stage.run(request)

    assert captured["url"] == "https://example.com/media/clip.mp4"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    # urllib.request.Request capitalises header names; check both shapes
    # to make this test resilient to that.
    assert any(
        k.lower() == "authorization" and v == "Bearer default-seam-token"
        for k, v in headers.items()
    )


# ---------------------------------------------------------------------------
# Layer M Task 6 — E2E integration: hosted-style engine → seam → store
# ---------------------------------------------------------------------------


def test_layer_m_e2e_hosted_auth_artifact_persisted(tmp_path: Path) -> None:
    """E2E: hosted-style engine returns auth'd Artifact → spy seam → store.

    Bug catch: spy passthrough works in unit tests but the orchestrator-
    constructed stage forgets to wire the seam; production downloads
    silently use the default seam where any monkeypatch-based test is
    blind.
    """
    captured: dict[str, object] = {}

    def spy_http_get_bytes(url: str, headers: dict[str, str]) -> bytes:
        captured["url"] = url
        captured["headers"] = headers
        return b"AUTHED-DOWNLOADED-BYTES"

    profile = _profile()
    bearer = {"Authorization": "Bearer e2e-tok"}
    backend = FakeBackend(probe=profile)
    stage = _make_stage(
        tmp_path,
        profile=profile,
        backend=backend,
        run_id="e2e-layer-m",
        http_get_bytes=spy_http_get_bytes,
        result_override=lambda job_id: Artifact(
            filename="e2e.mp4",
            url="https://hosted.example.com/media/e2e.mp4",
            headers=bearer,
        ),
    )
    request = GenerationRequest(mode="t2v", prompt="hello", assets=[])
    persisted = stage.run(request)

    # Spy was called with the exact bearer header.
    assert captured["url"] == "https://hosted.example.com/media/e2e.mp4"
    assert captured["headers"] == bearer

    # Persisted artifact carries the bytes returned by the spy.
    assert persisted.uri  # non-empty store URI
    stored_bytes = stage.store.get_bytes(persisted.uri)
    assert stored_bytes == b"AUTHED-DOWNLOADED-BYTES"


# ---------------------------------------------------------------------------
# Layer O Task 4 — GenerateClipStage sink + namespace integration
# ---------------------------------------------------------------------------


@dataclass
class _SpyOutputSink:
    """Dataclass spy that records every publish() call for assertion."""

    calls: list[dict[str, object]] = field(default_factory=list)

    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "data": data,
                "prompt": prompt,
                "extension": extension,
                "namespace": namespace,
            }
        )
        return "/spy/published"


def test_sink_none_no_publish_call(tmp_path: Path) -> None:
    """AC-1: sink=None (default) — store.put_bytes is the only side-effect.

    The stage should behave bit-for-bit identically to pre-Layer-O: no
    sink.publish call, return value is the Artifact from store.put_bytes.
    """
    profile = _profile()
    backend = FakeBackend(probe=profile)
    stage = _make_stage(tmp_path, profile=profile, backend=backend)
    # No sink argument — stage constructed without it.
    assert stage.sink is None

    request = GenerationRequest(prompt="a red sunset", mode="t2v")
    result = stage.run(request)

    # Returns a valid stored Artifact (uri exists).
    assert result.uri != ""
    assert Path(result.uri).exists()


def test_sink_present_publishes_once_with_correct_args(tmp_path: Path) -> None:
    """AC-2: sink present → publish called exactly once with expected kwargs.

    prompt=request.prompt, extension = Path(artifact.filename).suffix,
    namespace=None (no namespace arg passed).

    Catches a regression where Path(last.filename).suffix returns empty and
    falls through to '.bin'.
    """
    profile = _profile()
    backend = FakeBackend(probe=profile)
    spy = _SpyOutputSink()
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _fake_engine_for_tests(profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="sink-ac2",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
        sink=spy,
    )

    request = GenerationRequest(prompt="a red sunset", mode="t2v")
    stage.run(request)

    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["prompt"] == "a red sunset"
    # FakeBackend produces clip-<hex12>.mp4 — suffix must be exactly ".mp4".
    assert call["extension"] == ".mp4"
    assert call["namespace"] is None


def test_sink_namespace_propagated(tmp_path: Path) -> None:
    """AC-3: namespace='batch-X' propagates to sink.publish(namespace='batch-X')."""
    profile = _profile()
    backend = FakeBackend(probe=profile)
    spy = _SpyOutputSink()
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _fake_engine_for_tests(profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="sink-ac3",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
        sink=spy,
        namespace="batch-X",
    )

    request = GenerationRequest(prompt="namespace test", mode="t2v")
    stage.run(request)

    assert len(spy.calls) == 1
    assert spy.calls[0]["namespace"] == "batch-X"


def test_sink_multi_segment_publishes_only_last(tmp_path: Path) -> None:
    """AC-4: multi-segment non-native run publishes only the final artifact.

    The stage already persists only results[-1]; the sink must mirror that:
    one publish call even for 3 segments.

    Catches a regression where the stage publishes seg-0's prompt instead of
    the final segment's prompt.
    """
    profile = _profile(supports_native_extension=False)
    backend = FakeBackend(probe=profile)
    spy = _SpyOutputSink()
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _fake_engine_for_tests(profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="sink-ac4",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
        sink=spy,
    )

    segments_override = [Segment(prompt=f"seg-{i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored", mode="t2v"),
        segments_override=segments_override,
    )

    # Only 1 publish call regardless of segment count.
    assert len(spy.calls) == 1
    # The prompt comes from segments[-1].prompt, not segments[0].prompt.
    assert spy.calls[0]["prompt"] == "seg-2"
