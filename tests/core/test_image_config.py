"""ImageConfig: the base KeyframeConfig extends (Layer R terminal-image work)."""

from __future__ import annotations

from pathlib import Path

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


IMAGE_CFG_MINIMAL = {
    "image": {"engine": "fake", "prompt": "a cat", "spec": {"model": "m"}},
}

# Every key that would be INERT on an image config. Refusing them is the
# U51/U56 lesson: a key that is accepted and ignored is a defect here.
FORBIDDEN_WITH_IMAGE = [
    ("engine", {"kind": "fake", "precision": ""}),
    ("models", [{"kind": "base", "ref": "hf:x/y", "target": "checkpoints"}]),
    ("compute", {"provider": "local"}),
    ("loras", [{"ref": "hf:a/b"}]),
    ("keyframe", {"engine": "fake", "prompt": "x"}),
    ("upscale", {"engine": "spandrel"}),
    ("interpolate", {"engine": "rife"}),
    ("splitter", {"kind": "heuristic"}),
    ("spec", {"model": "x"}),
    ("params", {"width": 512}),
    ("lifecycle", {"budget": 1.0}),
]


def test_image_config_loads_without_engine_or_models() -> None:
    """An image config carries no video engine and no models list.

    Bug this catches: leaving Config.engine/models required, which makes every
    image config unloadable and forces operators to write a fake video engine
    block just to satisfy a validator.
    """
    from kinoforge.core.config import Config

    cfg = Config.model_validate(IMAGE_CFG_MINIMAL)
    assert cfg.image is not None
    assert cfg.image.engine == "fake"
    assert cfg.engine is None
    assert cfg.models == []


@pytest.mark.parametrize(("key", "value"), FORBIDDEN_WITH_IMAGE)
def test_forbidden_key_alongside_image_is_refused_by_name(
    key: str, value: object
) -> None:
    """Each inert key is refused AND the message names that key.

    Bug this catches: a validator that refuses everything for the wrong reason
    (so the operator cannot tell which key was the problem), and the inert-key
    class itself — an image cfg carrying `loras:` would reproduce U56 on
    purpose, one carrying `lifecycle:` would reproduce U51's shape.
    """
    from kinoforge.core.config import Config

    data = {**IMAGE_CFG_MINIMAL, key: value}
    with pytest.raises(PydanticValidationError, match=key):
        Config.model_validate(data)


def test_mode_t2i_is_accepted_and_other_modes_are_not() -> None:
    """`mode` on an image config is VALIDATED, not documentary.

    Bug this catches: accepting `mode: t2v` on an image config and silently
    ignoring it — the same inert-config defect as a forbidden key, which an
    earlier draft of this design shipped as "documentary only".
    """
    from kinoforge.core.config import Config

    assert Config.model_validate({**IMAGE_CFG_MINIMAL, "mode": "t2i"}).mode == "t2i"
    assert Config.model_validate(IMAGE_CFG_MINIMAL).mode is None
    with pytest.raises(PydanticValidationError, match="t2i"):
        Config.model_validate({**IMAGE_CFG_MINIMAL, "mode": "t2v"})


def test_store_and_output_are_permitted_alongside_image() -> None:
    """The allowlist admits exactly the five keys an image run uses.

    Bug this catches: an allowlist built on the VALIDATED model rather than the
    raw input, which cannot distinguish an operator-written `output:` from the
    default_factory one and so either refuses every config or admits every key.
    """
    from kinoforge.core.config import Config

    cfg = Config.model_validate(
        {**IMAGE_CFG_MINIMAL, "store": {"kind": "local"}, "output": {"dir": "out"}}
    )
    assert cfg.output.dir == Path("out")


def test_video_config_still_requires_engine_and_a_base_model() -> None:
    """Configs without `image:` take the existing path byte for byte.

    Bug this catches: making engine/models optional for EVERY config, which
    would let a typo'd video cfg load and fail much later on a booted pod.
    """
    from kinoforge.core.config import Config

    with pytest.raises(PydanticValidationError):
        Config.model_validate({"models": []})  # no engine at all
    with pytest.raises(PydanticValidationError, match="kind: base"):
        Config.model_validate(
            {"engine": {"kind": "fake", "precision": ""}, "models": []}
        )


def test_capability_key_on_image_config_raises_configerror() -> None:
    """An image config has no video identity; say so instead of AttributeError.

    Bug this catches: `self.engine.diffusers` on engine=None raising a bare
    AttributeError from deep inside capability_key, which reads as a kinoforge
    crash rather than "you called the wrong method for this config".
    """
    from kinoforge.core.config import Config
    from kinoforge.core.errors import ConfigError

    cfg = Config.model_validate(IMAGE_CFG_MINIMAL)
    with pytest.raises(ConfigError, match="image"):
        cfg.capability_key()
