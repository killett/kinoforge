"""ImageConfig: the base KeyframeConfig extends (Layer R terminal-image work)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from kinoforge.core.config import ImageConfig, KeyframeConfig


def test_image_config_accepts_no_prompt() -> None:
    """ImageConfig permits prompt=None because --prompt may supply it at runtime.

    Bug this catches: a prompt-required validator placed on the BASE instead of
    KeyframeConfig, which would make every bare `image:` block unloadable.
    """
    cfg = ImageConfig(engine="luma_agents", spec={"model": "uni-1"})
    assert cfg.prompt is None
    assert cfg.engine == "luma_agents"


def test_keyframe_config_still_requires_a_prompt() -> None:
    """KeyframeConfig keeps its own prompt validator after re-parenting.

    Bug this catches: moving _at_least_one_prompt up to the base (which would
    break ImageConfig) or dropping it (which would un-guard keyframe configs).
    """
    with pytest.raises(PydanticValidationError, match="requires either top-level"):
        KeyframeConfig(engine="fal", spec={"model": "fal-ai/flux/schnell"})


def test_keyframe_config_is_an_image_config() -> None:
    """The subclass relationship is what lets resolve_image_stack take one type.

    Bug this catches: shipping ImageConfig as a SIBLING, which would force
    resolve_image_stack onto a structural Protocol and silently accept any
    object with the right attribute names.
    """
    kf = KeyframeConfig(engine="fal", prompt="a cat", spec={"model": "m"})
    assert isinstance(kf, ImageConfig)


def test_capability_key_identical_across_both_types() -> None:
    """capability_key() lives once on the base and derives the same key.

    Bug this catches: a second copy of capability_key() on the subclass that
    drifts — which would strand every cached image profile, because the cache
    filename IS the derived key.
    """
    spec = {"model": "uni-1", "precision": "fp16"}
    img = ImageConfig(engine="luma_agents", spec=spec)
    kf = KeyframeConfig(engine="luma_agents", prompt="x", spec=spec)
    assert img.capability_key() == kf.capability_key()
    assert img.capability_key().base_model == "uni-1"
    assert img.capability_key().precision == "fp16"
    assert img.capability_key().engine == "luma_agents"
    assert img.capability_key().loras == ()


def test_image_config_forbids_unknown_keys() -> None:
    """extra="forbid" is inherited, so a typo'd key is refused not ignored.

    Bug this catches: losing model_config on the base during the extraction,
    turning `promt:` into a silently-ignored key — the inert-config class this
    whole design treats as a defect (U51/U56).
    """
    with pytest.raises(PydanticValidationError):
        ImageConfig(engine="fal", promt="typo")  # type: ignore[call-arg]


def test_capability_key_not_duplicated_in_source() -> None:
    """Structural guard: the extraction must DELETE the old copy, not shadow it.

    Bug this catches: leaving KeyframeConfig.capability_key in place so the
    base's version is never used and the duplication the design set out to
    remove silently survives.
    """
    from pathlib import Path

    import kinoforge.core.config as config_mod

    source = Path(config_mod.__file__).read_text(encoding="utf-8")
    assert source.count("def capability_key(self) -> CapabilityKey:") == 2, (
        "expected exactly 2 capability_key definitions in config.py "
        "(ImageConfig and Config); found a third — the KeyframeConfig copy "
        "was not deleted"
    )
