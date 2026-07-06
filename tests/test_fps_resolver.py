"""Exhaustive table for the pure fps resolver — zero cloud spend."""

import pytest

from kinoforge.core.fps_resolver import (
    FpsPlan,
    InterpCapability,
    resolve_fps_target,
)

ARB = InterpCapability.ARBITRARY_TIMESTEP
REC = InterpCapability.RECURSIVE_2X


def test_passthrough_when_equal():
    # Bug caught: booting a GPU pod to "interpolate" 30->30 is pure waste.
    plan = resolve_fps_target(30.0, 30.0, ARB, source_frame_count=90)
    assert plan == FpsPlan(
        schedule=None, recursion_depth=None, decimate_to=None, skip_gpu=True
    )


def test_decimate_only_when_target_below_source():
    # Bug caught: 30->24 is frame *removal*, must skip GPU and ffmpeg-decimate.
    plan = resolve_fps_target(30.0, 24.0, ARB, source_frame_count=90)
    assert plan.skip_gpu is True
    assert plan.schedule is None
    assert plan.decimate_to == 24.0


def test_arbitrary_exact_double():
    # 3 source frames at 16fps -> 32fps: insert one midpoint between each pair.
    # Output frames at times j/32 for j in 0..(round(3/16*32)-1)=0..5 -> 6 frames.
    plan = resolve_fps_target(16.0, 32.0, ARB, source_frame_count=3)
    assert plan.decimate_to is None
    assert plan.recursion_depth is None
    assert plan.schedule is not None
    assert len(plan.schedule) == 6
    # timesteps land on 0.0 (copy) and 0.5 (midpoint) only for an exact 2x.
    assert {round(t, 3) for _, t in plan.schedule} == {0.0, 0.5}
    # first output copies source frame 0 at t=0.0
    assert plan.schedule[0] == (0, 0.0)


def test_arbitrary_non_multiple_hits_exact_count():
    # 16 -> 24 over 2s (32 source frames): output count == round(2*24)=48.
    plan = resolve_fps_target(16.0, 24.0, ARB, source_frame_count=32)
    assert plan.decimate_to is None
    assert plan.schedule is not None
    assert len(plan.schedule) == 48
    # Non-multiple => at least one fractional (non-0, non-0.5-only) timestep.
    fracs = {round(t, 4) for _, t in plan.schedule}
    assert any(f not in (0.0, 0.5) for f in fracs)
    # Every source index referenced is in range.
    assert all(0 <= i < 32 for i, _ in plan.schedule)


def test_recursive_overshoot_then_decimate():
    # 16 -> 60: recursive engine can only do powers of two. ceil(60/16)=4 ->
    # next_pow2(4)=4 -> depth 2 -> 64fps, then decimate to exact 60.
    plan = resolve_fps_target(16.0, 60.0, REC, source_frame_count=16)
    assert plan.schedule is None
    assert plan.recursion_depth == 2
    assert plan.decimate_to == 60.0


def test_recursive_exact_power_of_two_no_decimate():
    # 16 -> 64: ceil(64/16)=4 -> next_pow2=4 -> depth 2 -> exactly 64, no trim.
    plan = resolve_fps_target(16.0, 64.0, REC, source_frame_count=16)
    assert plan.recursion_depth == 2
    assert plan.decimate_to is None


def test_non_positive_target_raises():
    with pytest.raises(ValueError):
        resolve_fps_target(16.0, 0.0, ARB, source_frame_count=16)
