"""Layer R T8: KeyframeConfig pydantic validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from kinoforge.core.config import KeyframeConfig
from kinoforge.core.interfaces import CapabilityKey


def test_keyframe_top_level_prompt_alone_loads() -> None:
    """Bug guard: simplest valid config — just engine + top-level prompt — must load."""
    cfg = KeyframeConfig(engine="fal", prompt="cat in meadow")
    assert cfg.prompt == "cat in meadow"


def test_keyframe_per_role_prompt_alone_loads() -> None:
    """Bug guard: per-role-only config (no top-level prompt) must load when role prompt is set."""
    cfg = KeyframeConfig(
        engine="fal",
        roles={"init_image": {"prompt": "cat"}},  # type: ignore[dict-item]
    )
    assert cfg.roles["init_image"].prompt == "cat"


def test_keyframe_no_prompt_anywhere_raises() -> None:
    """Bug guard: empty prompt config silently producing empty fal POSTs would burn money."""
    with pytest.raises(PydanticValidationError, match="prompt"):
        KeyframeConfig(engine="fal")


def test_keyframe_empty_prompt_strings_treated_as_unset() -> None:
    """Bug guard: a whitespace-only prompt is a typo, not a valid prompt."""
    with pytest.raises(PydanticValidationError, match="prompt"):
        KeyframeConfig(engine="fal", prompt="   ")


def test_keyframe_unknown_role_raises() -> None:
    """Bug guard: typo in role name silently no-ops the stage."""
    with pytest.raises(PydanticValidationError, match="unknown role"):
        KeyframeConfig(
            engine="fal",
            prompt="x",
            roles={"init_imag": {"prompt": "typo"}},  # type: ignore[dict-item]
        )


def test_keyframe_extra_top_level_key_raises() -> None:
    """extra='forbid' lockdown — typo in YAML key fails loud."""
    with pytest.raises(PydanticValidationError):
        KeyframeConfig(engine="fal", prompt="x", endpooint="y")  # type: ignore[call-arg]


def test_keyframe_capability_key_deterministic() -> None:
    """Bug guard: dict-key ordering in `spec` must not change derived hash."""
    a = KeyframeConfig(
        engine="fal", prompt="x", spec={"model": "m", "precision": "fp16"}
    )
    b = KeyframeConfig(
        engine="fal", prompt="x", spec={"precision": "fp16", "model": "m"}
    )
    assert a.capability_key() == b.capability_key()
    key = a.capability_key()
    assert isinstance(key, CapabilityKey)
    assert key.base_model == "m"
    assert key.engine == "fal"
    assert key.precision == "fp16"


def test_keyframe_per_role_spec_and_params_load() -> None:
    """Bug guard: per-role spec/params must be readable as plain dicts."""
    cfg = KeyframeConfig(
        engine="fal",
        prompt="x",
        roles={
            "first_frame": {  # type: ignore[dict-item]
                "prompt": "a",
                "spec": {"seed": 1},
                "params": {"k": "v"},
            },
        },
    )
    assert cfg.roles["first_frame"].spec == {"seed": 1}
    assert cfg.roles["first_frame"].params == {"k": "v"}
