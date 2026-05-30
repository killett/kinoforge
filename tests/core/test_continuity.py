"""Tests for core.continuity.inject_tail_frame — pure asset-injection helper.

Spec: docs/superpowers/specs/2026-05-30-extract-last-frame-design.md §4.4
"""

from __future__ import annotations

from kinoforge.core.continuity import inject_tail_frame
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationJob,
    Segment,
)


def _make_job(*, prompts: list[str]) -> GenerationJob:
    segs = [Segment(prompt=p, assets=[]) for p in prompts]
    return GenerationJob(spec={}, segments=segs, params={})


def _tail_asset(filename: str = "tail.png") -> ConditioningAsset:
    return ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename=filename, uri=f"file:///{filename}"),
    )


def test_inject_tail_frame_replaces_seg0_assets() -> None:
    """seg-0 ends with exactly [tail_asset]; the passed asset is preserved by identity.

    Bug this catches: helper wraps the asset in another container, copies it,
    or constructs a new ConditioningAsset from its fields — any of which breaks
    `is`-identity and prevents callers from equating the injected asset with
    the one they passed in.
    """
    next_job = _make_job(prompts=["next"])
    asset = _tail_asset()

    out = inject_tail_frame(next_job, asset)

    assert len(out.segments[0].assets) == 1
    assert out.segments[0].assets[0] is asset


def test_inject_tail_frame_preserves_other_segments() -> None:
    """Segments beyond index 0 are passed through unchanged.

    Bug this catches: helper rebuilds all segments instead of just seg-0.
    """
    next_job = _make_job(prompts=["seg0", "seg1", "seg2"])
    original_seg1 = next_job.segments[1]
    original_seg2 = next_job.segments[2]

    out = inject_tail_frame(next_job, _tail_asset())

    assert out.segments[1] is original_seg1
    assert out.segments[2] is original_seg2


def test_inject_tail_frame_does_not_mutate_input() -> None:
    """Input job's seg-0 assets remain [] after the call.

    Bug this catches: helper mutates in place (e.g. .append) on the input.
    """
    next_job = _make_job(prompts=["next"])
    assert next_job.segments[0].assets == []

    inject_tail_frame(next_job, _tail_asset())

    assert next_job.segments[0].assets == []
