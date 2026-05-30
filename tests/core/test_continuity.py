"""Tests for core.continuity.inject_tail_frame — pure helper for tail-frame conditioning.

Spec: docs/superpowers/specs/2026-05-29-continuity-fallback-design.md §6.1
"""

from __future__ import annotations

import pytest

from kinoforge.core.continuity import inject_tail_frame
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationEngine,
    GenerationJob,
    Segment,
)


class _FakeExtractor(GenerationEngine):
    """Minimal engine override: only extract_last_frame is meaningful."""

    name: str = "fake-extractor"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def provision(self, instance, cfg):  # noqa: ANN001, D102
        pass

    def backend(self, instance, cfg):  # noqa: ANN001, D102
        raise NotImplementedError

    def profile_for(self, key):  # noqa: ANN001, D102
        raise NotImplementedError

    def declared_flags(self, key):  # noqa: ANN001, D102
        return {}

    def validate_spec(self, job):  # noqa: ANN001, D102
        pass

    def extract_last_frame(self, artifact: Artifact) -> ConditioningAsset:
        return ConditioningAsset(
            kind="image",
            role="init_image",
            ref=Artifact(filename=f"{artifact.filename}.tail.png"),
        )


def _make_job(
    *, prompts: list[str], seg0_assets: list[ConditioningAsset] | None = None
) -> GenerationJob:
    segs = [
        Segment(prompt=p, assets=list(seg0_assets) if (i == 0 and seg0_assets) else [])
        for i, p in enumerate(prompts)
    ]
    return GenerationJob(spec={}, segments=segs, params={})


def test_inject_tail_frame_replaces_seg0_assets() -> None:
    """When seg-0 starts empty, after inject it contains exactly [tail_asset].

    Bug this catches: helper appends instead of replacing -> splitter contract drift
    (segs 1..N-1 are guaranteed empty assets; appending would still work today but
    breaks the invariant the rest of the pipeline relies on).
    """
    next_job = _make_job(prompts=["next"])
    prev_artifact = Artifact(filename="prev.mp4")
    engine = _FakeExtractor()

    out = inject_tail_frame(next_job, prev_artifact, engine)

    assert len(out.segments[0].assets) == 1
    asset = out.segments[0].assets[0]
    assert asset.kind == "image"
    assert asset.role == "init_image"
    assert asset.ref.filename == "prev.mp4.tail.png"


def test_inject_tail_frame_preserves_other_segments() -> None:
    """Segments beyond index 0 are passed through unchanged.

    Bug this catches: helper rebuilds all segments instead of just seg-0.
    """
    next_job = _make_job(prompts=["seg0", "seg1", "seg2"])
    original_seg1 = next_job.segments[1]
    original_seg2 = next_job.segments[2]

    out = inject_tail_frame(next_job, Artifact(filename="p.mp4"), _FakeExtractor())

    assert out.segments[1] is original_seg1
    assert out.segments[2] is original_seg2


def test_inject_tail_frame_does_not_mutate_input() -> None:
    """Input job's seg-0 assets remain [] after the call.

    Bug this catches: helper mutates in place (e.g. .append) on the input segment.
    """
    next_job = _make_job(prompts=["next"])
    assert next_job.segments[0].assets == []

    inject_tail_frame(next_job, Artifact(filename="p.mp4"), _FakeExtractor())

    assert next_job.segments[0].assets == []


def test_inject_tail_frame_raises_when_engine_extract_raises() -> None:
    """NotImplementedError from engine.extract_last_frame propagates.

    Bug this catches: helper swallows the raise or wraps in a different exception.
    """

    class _Raising(_FakeExtractor):
        def extract_last_frame(self, artifact):  # noqa: ANN001
            raise NotImplementedError("nope")

    with pytest.raises(NotImplementedError, match="nope"):
        inject_tail_frame(
            _make_job(prompts=["x"]), Artifact(filename="p.mp4"), _Raising()
        )
