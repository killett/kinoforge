"""interpolate: config block validation + capability-key wiring."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.config import (
    Config,
    InterpolateConfig,
    RifeEngineConfig,
)
from kinoforge.core.errors import ConfigError


def _minimal_generate_cfg() -> dict[str, Any]:
    """Smallest valid cfg dict (fake engine + stub base), for capability_key."""
    return {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {
                "ref": "https://example.com/models/fake-base.safetensors",
                "kind": "base",
                "target": "checkpoints",
            }
        ],
        "compute": {
            "provider": "runpod",
            "image": "kinoforge/runpod:latest",
            "lifecycle": {
                "idle_timeout": 600,
                "max_lifetime": 14400,
                "budget": 10.0,
            },
        },
        "store": {"kind": "local"},
    }


def test_fps_must_be_positive():
    with pytest.raises((ConfigError, ValueError)):
        InterpolateConfig(
            engine="rife", fps=0.0, rife=RifeEngineConfig(weights_ref="hf:x")
        )


def test_rife_engine_requires_block():
    with pytest.raises(ConfigError):
        InterpolateConfig(engine="rife", fps=60.0, rife=None)


def test_valid_rife_config():
    c = InterpolateConfig(
        engine="rife",
        fps=59.94,
        rife=RifeEngineConfig(weights_ref="hf:kinoforge/rife", model="rife49"),
    )
    assert c.fps == 59.94
    assert c.rife is not None
    assert c.rife.model == "rife49"


def test_precision_allowlist():
    with pytest.raises(ConfigError):
        RifeEngineConfig(weights_ref="hf:x", precision="int4")


def test_capability_key_includes_interpolate_stage():
    d = _minimal_generate_cfg()
    d["interpolate"] = {
        "engine": "rife",
        "fps": 60.0,
        "rife": {"weights_ref": "hf:kinoforge/rife", "model": "rife49"},
    }
    cfg = Config.model_validate(d)
    key = cfg.capability_key()
    assert "interpolate" in key.stages
    assert key.interpolator == "rife"
    assert key.interpolator_fps == 60.0


def test_capability_key_backward_compatible_without_interpolate():
    # Bug caught: adding interpolator fields to the hash payload for a
    # non-interp cfg would break every existing ledger entry's cache key.
    cfg = Config.model_validate(_minimal_generate_cfg())
    key = cfg.capability_key()
    assert key.interpolator == ""
    assert key.interpolator_fps == 0.0
    assert "interpolate" not in key.stages
