"""Tests for the pure height-target upscale resolver."""

import pytest

from kinoforge.core.errors import ScaleUnsatisfiableError
from kinoforge.core.scale_resolver import HeightPlan, resolve_height_target


def test_overshoot_picks_smallest_sufficient_factor() -> None:
    # Behaviour: 480p -> 1080p with a (2x,4x) menu. 2x=960<1080 insufficient,
    # 4x=1920>=1080 sufficient. Bug caught: picking 2x (undersized) or picking
    # the largest factor blindly (needless overshoot when a smaller one fits).
    assert resolve_height_target(480, (2.0, 4.0), 1080) == HeightPlan(
        upscale_factor=4.0, downscale_to=1080
    )


def test_exact_hit_sets_no_downscale() -> None:
    # Behaviour: 540p x 2 == 1080p exactly. Bug caught: emitting a needless
    # downscale (re-encode) when the factor already lands on the target.
    assert resolve_height_target(540, (2.0, 4.0), 1080) == HeightPlan(
        upscale_factor=2.0, downscale_to=None
    )


def test_single_factor_menu_flashvsr() -> None:
    # Behaviour: FlashVSR (4x only) 480p -> 1080p. Bug caught: resolver assuming
    # a multi-entry menu and IndexError-ing on a one-factor engine.
    assert resolve_height_target(480, (4.0,), 1080) == HeightPlan(
        upscale_factor=4.0, downscale_to=1080
    )


def test_source_taller_than_target_is_downscale_only() -> None:
    # Behaviour: 1080p source, 720p requested -> skip GPU, downscale to 720.
    # Bug caught: forcing an upscale (or erroring) when the source is already big.
    assert resolve_height_target(1080, (2.0, 4.0), 720) == HeightPlan(
        upscale_factor=None, downscale_to=720
    )


def test_source_equals_target_is_passthrough() -> None:
    # Behaviour: source == target -> no upscale, no downscale. Bug caught: a
    # no-op re-encode when nothing needs to change.
    assert resolve_height_target(720, (2.0, 4.0), 720) == HeightPlan(
        upscale_factor=None, downscale_to=None
    )


def test_undershoot_raises_with_context() -> None:
    # Behaviour: 240p, 4x-only, want 1080p -> 960p < 1080p, unsatisfiable.
    # Bug caught: silently delivering a below-target result.
    with pytest.raises(ScaleUnsatisfiableError) as ei:
        resolve_height_target(240, (4.0,), 1080)
    err = ei.value
    assert err.source_h == 240
    assert err.largest_factor == 4.0
    assert err.reached_h == 960
    assert err.requested_h == 1080


def test_empty_factors_raises_valueerror() -> None:
    # Behaviour: an engine with no declared factors can't serve a height target.
    with pytest.raises(ValueError, match="non-empty"):
        resolve_height_target(480, (), 1080)
