"""`upscale.max_output_megapixels` — the controller-side output-pixel cap."""

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


def test_default_is_256() -> None:
    # Bug caught: no default → every existing config fails to load.
    cfg = Config.model_validate(_cfg())
    assert cfg.upscale is not None
    assert cfg.upscale.max_output_megapixels == 256


def test_explicit_value_round_trips() -> None:
    cfg = Config.model_validate(_cfg({"max_output_megapixels": 64}))
    assert cfg.upscale is not None
    assert cfg.upscale.max_output_megapixels == 64


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_is_refused(bad: int) -> None:
    # Bug caught: a 0 cap refuses every image; a negative one is nonsense
    # that would surface only as a confusing "oversize" on the first file.
    with pytest.raises(ConfigError, match="upscale.max_output_megapixels"):
        Config.model_validate(_cfg({"max_output_megapixels": bad}))
