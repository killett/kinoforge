"""Tests for the long-video strategy decision point.

Covers all 7 acceptance criteria:
1. Native extension + 4 segments → 1 job with all 4 segments.
2. Non-native + 4 segments → 4 jobs, each with 1 segment, order preserved.
3. Per-segment assets are preserved in both branches.
4. Segment-wins merge: segment param overrides base; base fills gaps.
5. Job-level params is unchanged base dict; merge only affects Segment.params.
6. _audio_mode is "joint" / "separate" per supports_joint_audio flag.
7. Purity: two calls with identical inputs return equal output.
"""

from __future__ import annotations

from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    ModelProfile,
    Segment,
)
from kinoforge.core.strategy import decide

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(*, native: bool, joint: bool) -> ModelProfile:
    """Build a minimal ModelProfile with only the flags under test varied."""
    return ModelProfile(
        name="test-model",
        max_frames=80,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=native,
        supports_joint_audio=joint,
    )


def _asset(role: str = "init_image") -> ConditioningAsset:
    """Return a minimal ConditioningAsset for asset-preservation checks."""
    return ConditioningAsset(
        kind="image", role=role, ref=Artifact(filename="frame.png")
    )


def _segments(*prompts: str, params: dict[str, object] | None = None) -> list[Segment]:
    """Return one Segment per prompt, each with the given params (default empty)."""
    seg_params: dict[str, object] = params if params is not None else {}
    return [Segment(prompt=p, params=dict(seg_params)) for p in prompts]


def _segments_with_assets(*prompts: str) -> list[Segment]:
    """Return one Segment per prompt, each carrying a unique asset."""
    return [
        Segment(prompt=p, assets=[_asset(f"role_{i}")], params={})
        for i, p in enumerate(prompts)
    ]


# ---------------------------------------------------------------------------
# AC 1: native + 4 segments → 1 job, all 4 segments, order preserved
# ---------------------------------------------------------------------------


def test_native_extension_produces_one_job() -> None:
    """Native engine: exactly 1 GenerationJob is returned for 4 segments."""
    profile = _profile(native=True, joint=False)
    segs = _segments("a", "b", "c", "d")
    result = decide(profile, segs, {}, {})

    assert len(result) == 1


def test_native_extension_job_carries_all_segments() -> None:
    """Native engine: the single job contains all 4 segments."""
    profile = _profile(native=True, joint=False)
    segs = _segments("a", "b", "c", "d")
    result = decide(profile, segs, {}, {})

    assert len(result[0].segments) == 4


def test_native_extension_segment_order_preserved() -> None:
    """Native engine: segment order inside the job matches input order."""
    profile = _profile(native=True, joint=False)
    segs = _segments("alpha", "beta", "gamma", "delta")
    result = decide(profile, segs, {}, {})

    prompts = [s.prompt for s in result[0].segments]
    assert prompts == ["alpha", "beta", "gamma", "delta"]


# ---------------------------------------------------------------------------
# AC 2: non-native + 4 segments → 4 jobs, 1 segment each, order preserved
# ---------------------------------------------------------------------------


def test_non_native_produces_n_jobs() -> None:
    """Non-native engine: 4 segments → 4 GenerationJobs."""
    profile = _profile(native=False, joint=False)
    segs = _segments("a", "b", "c", "d")
    result = decide(profile, segs, {}, {})

    assert len(result) == 4


def test_non_native_each_job_has_one_segment() -> None:
    """Non-native engine: each job holds exactly 1 segment."""
    profile = _profile(native=False, joint=False)
    segs = _segments("a", "b", "c", "d")
    result = decide(profile, segs, {}, {})

    for job in result:
        assert len(job.segments) == 1


def test_non_native_segment_order_preserved() -> None:
    """Non-native engine: job[i].segments[0].prompt matches input_segments[i].prompt."""
    profile = _profile(native=False, joint=False)
    prompts = ["alpha", "beta", "gamma", "delta"]
    segs = _segments(*prompts)
    result = decide(profile, segs, {}, {})

    for i, job in enumerate(result):
        assert job.segments[0].prompt == prompts[i]


# ---------------------------------------------------------------------------
# AC 3: per-segment assets preserved in both branches
# ---------------------------------------------------------------------------


def test_native_extension_assets_preserved() -> None:
    """Native engine: each segment's assets survive into the produced job."""
    profile = _profile(native=True, joint=False)
    segs = _segments_with_assets("a", "b", "c", "d")
    result = decide(profile, segs, {}, {})

    for i, seg in enumerate(result[0].segments):
        assert seg.assets[0].role == f"role_{i}"


def test_non_native_assets_preserved() -> None:
    """Non-native engine: each single-segment job retains the original assets."""
    profile = _profile(native=False, joint=False)
    segs = _segments_with_assets("a", "b", "c", "d")
    result = decide(profile, segs, {}, {})

    for i, job in enumerate(result):
        assert job.segments[0].assets[0].role == f"role_{i}"


# ---------------------------------------------------------------------------
# AC 4: segment-wins merge in both branches
# ---------------------------------------------------------------------------


def test_segment_wins_merge_native() -> None:
    """Native engine: segment param overrides base; base fills gaps.

    Input segment has params={"seed": 42}.
    Base params={"seed": 1, "fps": 16}.
    Expected per-segment params={"seed": 42, "fps": 16}.
    """
    profile = _profile(native=True, joint=False)
    segs = _segments("clip", params={"seed": 42})
    base_params = {"seed": 1, "fps": 16}
    result = decide(profile, segs, base_params, {})

    assert result[0].segments[0].params == {"seed": 42, "fps": 16}


def test_segment_wins_merge_non_native() -> None:
    """Non-native engine: segment param overrides base; base fills gaps.

    Input segment has params={"seed": 42}.
    Base params={"seed": 1, "fps": 16}.
    Expected per-segment params={"seed": 42, "fps": 16}.
    """
    profile = _profile(native=False, joint=False)
    segs = _segments("clip", params={"seed": 42})
    base_params = {"seed": 1, "fps": 16}
    result = decide(profile, segs, base_params, {})

    assert result[0].segments[0].params == {"seed": 42, "fps": 16}


# ---------------------------------------------------------------------------
# AC 5: job-level params is unchanged base dict
# ---------------------------------------------------------------------------


def test_job_params_is_unchanged_base_native() -> None:
    """Native engine: GenerationJob.params equals the original base params."""
    profile = _profile(native=True, joint=False)
    segs = _segments("clip", params={"seed": 99})
    base_params = {"seed": 1, "fps": 16}
    result = decide(profile, segs, base_params, {})

    assert result[0].params == {"seed": 1, "fps": 16}


def test_job_params_is_unchanged_base_non_native() -> None:
    """Non-native engine: each GenerationJob.params equals the original base params."""
    profile = _profile(native=False, joint=False)
    segs = _segments("a", "b", params={"seed": 99})
    base_params = {"seed": 1, "fps": 16}
    result = decide(profile, segs, base_params, {})

    for job in result:
        assert job.params == {"seed": 1, "fps": 16}


# ---------------------------------------------------------------------------
# AC 6: _audio_mode marker on spec
# ---------------------------------------------------------------------------


def test_audio_mode_joint_when_supports_joint_audio() -> None:
    """spec['_audio_mode'] == 'joint' when profile.supports_joint_audio is True."""
    profile = _profile(native=True, joint=True)
    result = decide(profile, _segments("a"), {}, {})

    assert result[0].spec["_audio_mode"] == "joint"


def test_audio_mode_separate_when_not_supports_joint_audio() -> None:
    """spec['_audio_mode'] == 'separate' when profile.supports_joint_audio is False."""
    profile = _profile(native=True, joint=False)
    result = decide(profile, _segments("a"), {}, {})

    assert result[0].spec["_audio_mode"] == "separate"


def test_audio_mode_propagated_to_all_non_native_jobs() -> None:
    """Non-native: every produced job carries the correct _audio_mode."""
    profile = _profile(native=False, joint=True)
    segs = _segments("a", "b", "c")
    result = decide(profile, segs, {}, {})

    for job in result:
        assert job.spec["_audio_mode"] == "joint"


def test_audio_mode_does_not_overwrite_unrelated_spec_keys() -> None:
    """Existing spec keys are preserved alongside _audio_mode."""
    profile = _profile(native=True, joint=True)
    result = decide(profile, _segments("a"), {}, {"engine_hint": "comfy"})

    assert result[0].spec["engine_hint"] == "comfy"
    assert result[0].spec["_audio_mode"] == "joint"


# ---------------------------------------------------------------------------
# AC 7: purity — two calls with same inputs return equal output
# ---------------------------------------------------------------------------


def test_purity_native() -> None:
    """Native: calling decide twice returns equal outputs with no global mutation."""
    profile = _profile(native=True, joint=True)
    segs = _segments("x", "y", params={"seed": 7})
    base_params: dict[str, object] = {"fps": 24}
    spec: dict[str, object] = {"hint": "test"}

    result_a = decide(profile, segs, base_params, spec)
    result_b = decide(profile, segs, base_params, spec)

    assert len(result_a) == len(result_b)
    assert result_a[0].segments[0].params == result_b[0].segments[0].params
    assert result_a[0].spec == result_b[0].spec
    assert result_a[0].params == result_b[0].params


def test_purity_non_native() -> None:
    """Non-native: calling decide twice returns equal outputs with no global mutation."""
    profile = _profile(native=False, joint=False)
    segs = _segments("x", "y", params={"seed": 7})
    base_params: dict[str, object] = {"fps": 24}
    spec: dict[str, object] = {"hint": "test"}

    result_a = decide(profile, segs, base_params, spec)
    result_b = decide(profile, segs, base_params, spec)

    assert len(result_a) == len(result_b)
    for job_a, job_b in zip(result_a, result_b, strict=True):
        assert job_a.segments[0].params == job_b.segments[0].params
        assert job_a.spec == job_b.spec
        assert job_a.params == job_b.params


# ---------------------------------------------------------------------------
# Layer K regression locks: merge semantics + strategy-authoritative _audio_mode
# ---------------------------------------------------------------------------


def test_decide_segment_params_merge_over_non_empty_base_params() -> None:
    """Segment-wins merge holds when base_params is non-empty.

    Bug catch: existing tests pass {} as base_params, so a regression
    where _merged_segment was switched to base-wins would only break
    when a real cfg.params is routed in (Layer K).
    """
    profile = ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    segments = [
        Segment(prompt="a", assets=[], params={"steps": 50}),
        Segment(prompt="b", assets=[], params={"fps": 30}),
    ]
    base_params = {"fps": 24, "steps": 30, "seed": 42}

    jobs = decide(profile, segments, base_params, {})

    # Non-native path: one job per segment.
    assert len(jobs) == 2
    assert jobs[0].segments[0].params == {"fps": 24, "steps": 50, "seed": 42}
    assert jobs[1].segments[0].params == {"fps": 30, "steps": 30, "seed": 42}
    # job.params is the unchanged base — but a defensive copy.
    assert jobs[0].params == base_params
    assert jobs[0].params is not base_params


def test_decide_strategy_overrides_user_supplied_audio_mode() -> None:
    """A YAML spec._audio_mode never beats strategy.decide's derivation.

    Bug catch: a user routing spec: {_audio_mode: bogus} into the
    orchestrator (Layer K) must not be able to override the engine's
    audio strategy.
    """
    profile = ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=True,
        supports_joint_audio=False,
    )
    segments = [Segment(prompt="a", assets=[], params={})]
    user_spec = {"_audio_mode": "user-set-wrong-value", "other": "x"}

    jobs = decide(profile, segments, {}, user_spec)

    assert len(jobs) == 1
    assert jobs[0].spec["_audio_mode"] != "user-set-wrong-value"
    # The actual derivation: supports_joint_audio=False -> "separate".
    assert jobs[0].spec["_audio_mode"] == "separate"
    assert jobs[0].spec["other"] == "x"
