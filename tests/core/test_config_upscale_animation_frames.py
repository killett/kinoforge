"""`upscale.max_animation_frames` — the plan-time cap on frames per animated input."""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ConfigError


def _cfg(extra: dict[str, object] | None = None) -> dict[str, object]:
    upscale: dict[str, object] = {
        "engine": "spandrel",
        "scale": "2x",
        "spandrel": {
            "model_url": "hf:foo/bar.pth",
            "arch": "realesrgan",
            "precision": "fp16",
            "tile_size": 512,
            "batch_size": 4,
        },
    }
    upscale.update(extra or {})
    return {
        "engine": {"kind": "diffusers", "precision": "fp8"},
        "models": [
            {
                "kind": "base",
                "ref": "hf:Wan-AI/Wan2.2-T2V",
                "target": "diffusion_models",
            }
        ],
        "compute": {"provider": "fake", "image": "fake:latest"},
        "upscale": upscale,
    }


def test_default_is_300() -> None:
    # Bug caught: no default → every existing config fails to load, or a
    # default of 0 refuses every animation.
    cfg = Config.model_validate(_cfg())
    assert cfg.upscale is not None
    assert cfg.upscale.max_animation_frames == 300


def test_explicit_value_round_trips() -> None:
    # Bug caught: a validator that ignores the configured value.
    cfg = Config.model_validate(_cfg({"max_animation_frames": 12}))
    assert cfg.upscale is not None
    assert cfg.upscale.max_animation_frames == 12


@pytest.mark.parametrize("bad", [0, -3])
def test_below_one_is_refused(bad: int) -> None:
    # Bug caught: a 0 cap silently refuses every animated file as oversize.
    with pytest.raises(ConfigError, match="upscale.max_animation_frames"):
        Config.model_validate(_cfg({"max_animation_frames": bad}))
