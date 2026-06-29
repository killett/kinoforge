"""Tests for UpscaleConfig / SeedVR2EngineConfig + capability_key wiring."""

from __future__ import annotations

from typing import Any

from kinoforge.core.config import (
    Config,
    SeedVR2EngineConfig,
    UpscaleConfig,
)


def _minimal_generate_cfg() -> dict[str, Any]:
    """Smallest valid cfg dict that produces a non-empty engine block.

    Shape lifted from examples/configs/cost.yaml — uses the ``fake``
    engine + a stub base model so Config.model_validate succeeds without
    a real registered engine implementation.
    """
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


class TestSeedVR2EngineConfig:
    def test_default_weights_ref_3b_fp8(self) -> None:
        c = SeedVR2EngineConfig()
        assert c.weights_ref == "hf:ByteDance-Seed/SeedVR2-3B"

    def test_default_weights_ref_7b_fp16(self) -> None:
        c = SeedVR2EngineConfig(variant="7B", precision="fp16")
        assert c.weights_ref == "hf:ByteDance-Seed/SeedVR2-7B"

    def test_explicit_override_preserved(self) -> None:
        c = SeedVR2EngineConfig(weights_ref="hf:fork/custom-seedvr2")
        assert c.weights_ref == "hf:fork/custom-seedvr2"


class TestUpscaleConfig:
    def test_round_trip(self) -> None:
        u = UpscaleConfig(
            engine="seedvr2",
            scale="2x",
            seedvr2=SeedVR2EngineConfig(),
        )
        assert u.engine == "seedvr2"
        assert u.scale == "2x"
        assert u.seedvr2 is not None


class TestConfigCapabilityKeyStages:
    def test_pure_generate_cfg_stages_empty(self) -> None:
        cfg = Config.model_validate(_minimal_generate_cfg())
        key = cfg.capability_key()
        assert key.stages == ()
        assert key.upscaler == ""

    def test_generate_with_upscale_stages(self) -> None:
        d = _minimal_generate_cfg()
        d["upscale"] = {
            "engine": "seedvr2",
            "scale": "2x",
            "seedvr2": {"variant": "3B", "precision": "fp8"},
        }
        cfg = Config.model_validate(d)
        key = cfg.capability_key()
        assert key.stages == ("t2v", "upscale")
        assert key.upscaler == "seedvr2"
        assert key.upscaler_precision == "3b-fp8"
