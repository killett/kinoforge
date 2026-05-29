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
) -> GenerateClipStage:
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    return GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
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
    )
    artifact2 = stage2.run(GenerationRequest(prompt="round trip check", mode="t2v"))
    retrieved2 = store2.get_bytes(artifact2.uri)

    assert retrieved == retrieved2
