"""Tests for kinoforge.core.validation.validate_request.

Each test targets one acceptance criterion (AC 1–9) and is annotated with the
concrete bug it would catch if the implementation were wrong.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationRequest,
    ModelProfile,
)
from kinoforge.core.validation import validate_request

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(modes: set[str]) -> ModelProfile:
    """Return a minimal ModelProfile supporting only the given modes."""
    return ModelProfile(
        name="test-model",
        max_frames=120,
        fps=24,
        supported_modes=modes,
        max_resolution=(1280, 720),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def _image(role: str = "") -> ConditioningAsset:
    """Return a ConditioningAsset with kind='image' and the given role."""
    return ConditioningAsset(kind="image", role=role, ref=Artifact())


def _audio(role: str = "") -> ConditioningAsset:
    """Return a ConditioningAsset with kind='audio' and the given role."""
    return ConditioningAsset(kind="audio", role=role, ref=Artifact())


# ---------------------------------------------------------------------------
# AC 1: mode not in supported_modes → ValidationError mentioning the mode
# ---------------------------------------------------------------------------


def test_ac1_unsupported_mode_raises() -> None:
    """Bug caught: validate_request silently allows any mode string."""
    profile = _profile(modes={"i2v"})
    request = GenerationRequest(prompt="a", mode="flf2v", assets=[])

    with pytest.raises(ValidationError, match="flf2v"):
        validate_request(profile, request, accepted_kinds={"image"})


# ---------------------------------------------------------------------------
# AC 2: flf2v with first_frame + last_frame → success
# ---------------------------------------------------------------------------


def test_ac2_flf2v_valid_both_frames_succeeds() -> None:
    """Bug caught: validate_request rejects a correctly formed flf2v request."""
    profile = _profile(modes={"flf2v"})
    request = GenerationRequest(
        prompt="a",
        mode="flf2v",
        assets=[_image("first_frame"), _image("last_frame")],
    )

    result = validate_request(profile, request, accepted_kinds={"image"})

    assert result.mode == "flf2v"
    roles = {a.role for a in result.assets}
    assert "first_frame" in roles
    assert "last_frame" in roles


# ---------------------------------------------------------------------------
# AC 3: flf2v missing last_frame → ValidationError
# ---------------------------------------------------------------------------


def test_ac3_flf2v_missing_last_frame_raises() -> None:
    """Bug caught: validate_request allows flf2v with only one of two required roles."""
    profile = _profile(modes={"flf2v"})
    request = GenerationRequest(
        prompt="a",
        mode="flf2v",
        assets=[_image("first_frame")],
    )

    with pytest.raises(ValidationError, match="last_frame"):
        validate_request(profile, request, accepted_kinds={"image"})


# ---------------------------------------------------------------------------
# AC 4: flf2v with two first_frames and no last_frame → ValidationError
# ---------------------------------------------------------------------------


def test_ac4_flf2v_duplicated_first_frame_and_missing_last_frame_raises() -> None:
    """Bug caught: duplicate detection suppresses missing-role detection or vice versa."""
    profile = _profile(modes={"flf2v"})
    request = GenerationRequest(
        prompt="a",
        mode="flf2v",
        assets=[_image("first_frame"), _image("first_frame")],
    )

    # Either "duplicated" first_frame or missing last_frame must be flagged.
    with pytest.raises(ValidationError):
        validate_request(profile, request, accepted_kinds={"image"})


# ---------------------------------------------------------------------------
# AC 5: i2v with two init_images → ValidationError
# ---------------------------------------------------------------------------


def test_ac5_i2v_duplicated_init_image_raises() -> None:
    """Bug caught: validate_request counts roles but treats duplicates as 'present once'."""
    profile = _profile(modes={"i2v"})
    request = GenerationRequest(
        prompt="a",
        mode="i2v",
        assets=[_image("init_image"), _image("init_image")],
    )

    with pytest.raises(ValidationError, match="init_image"):
        validate_request(profile, request, accepted_kinds={"image"})


# ---------------------------------------------------------------------------
# AC 6: i2v with single un-roled image → success; returned role == "init_image"
# ---------------------------------------------------------------------------


def test_ac6_i2v_lone_unroled_image_defaults_role() -> None:
    """Bug caught: auto-role default not applied, so lone un-roled image is rejected
    or returned with role still empty.
    """
    profile = _profile(modes={"i2v"})
    request = GenerationRequest(
        prompt="a",
        mode="i2v",
        assets=[_image(role="")],
    )

    result = validate_request(profile, request, accepted_kinds={"image"})

    assert len(result.assets) == 1
    assert result.assets[0].role == "init_image"


# ---------------------------------------------------------------------------
# AC 7: flf2v with single un-roled image → ValidationError
# ---------------------------------------------------------------------------


def test_ac7_flf2v_lone_unroled_image_raises() -> None:
    """Bug caught: single-asset auto-routing applied to multi-asset modes."""
    profile = _profile(modes={"flf2v"})
    request = GenerationRequest(
        prompt="a",
        mode="flf2v",
        assets=[_image(role="")],
    )

    with pytest.raises(ValidationError):
        validate_request(profile, request, accepted_kinds={"image"})


# ---------------------------------------------------------------------------
# AC 8: asset with kind="audio" and accepted_kinds={"image"} → ValidationError
# ---------------------------------------------------------------------------


def test_ac8_rejected_asset_kind_raises() -> None:
    """Bug caught: validate_request ignores asset kinds and lets audio through."""
    profile = _profile(modes={"i2v"})
    request = GenerationRequest(
        prompt="a",
        mode="i2v",
        assets=[_audio(role="init_image")],
    )

    with pytest.raises(ValidationError, match="audio"):
        validate_request(profile, request, accepted_kinds={"image"})


# ---------------------------------------------------------------------------
# AC 9: returned object is new; input's assets list is unchanged
# ---------------------------------------------------------------------------


def test_ac9_no_input_mutation_and_new_object_returned() -> None:
    """Bug caught: validate_request returns the same object or mutates input assets."""
    profile = _profile(modes={"i2v"})
    original_asset = _image(role="")
    original_assets = [original_asset]
    request = GenerationRequest(
        prompt="a",
        mode="i2v",
        assets=original_assets,
    )

    result = validate_request(profile, request, accepted_kinds={"image"})

    # Must be a new GenerationRequest object.
    assert id(result) != id(request)

    # The input's assets list must be untouched.
    assert len(request.assets) == 1
    assert request.assets[0].role == "", (
        "Input asset role was mutated in place; validate_request must not modify input"
    )

    # The input asset object itself must be untouched (role still "").
    assert original_asset.role == ""

    # The output carries the defaulted role.
    assert result.assets[0].role == "init_image"
