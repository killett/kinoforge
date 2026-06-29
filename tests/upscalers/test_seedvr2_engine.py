"""Tests for SeedVR2Engine — HTTP-aware UpscalerEngine implementation."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import (
    NotYetImplementedError,
    UnsupportedScaleError,
)
from kinoforge.core.interfaces import Artifact, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.upscalers.seedvr2 import SeedVR2Engine


def _job(scale: ScaleTarget) -> UpscaleJob:
    return UpscaleJob(
        source=Artifact(uri="file:///tmp/in.mp4", sha256="0" * 64, size=1),
        scale=scale,
    )


class TestEngineMetadata:
    def test_name(self) -> None:
        assert SeedVR2Engine().name == "seedvr2"

    def test_requires_compute_and_local_weights(self) -> None:
        e = SeedVR2Engine()
        assert e.requires_compute is True
        assert e.requires_local_weights is True

    def test_supported_scales_contains_2x_and_4x(self) -> None:
        scales = SeedVR2Engine().supported_scales
        values = {s.value for s in scales if s.kind == "factor"}
        assert {2.0, 4.0}.issubset(values)


class TestValidateSpec:
    def test_accepts_2x(self) -> None:
        SeedVR2Engine().validate_spec(_job(ScaleTarget(kind="factor", value=2.0)))

    def test_accepts_4x(self) -> None:
        SeedVR2Engine().validate_spec(_job(ScaleTarget(kind="factor", value=4.0)))

    def test_refuses_3x(self) -> None:
        with pytest.raises(UnsupportedScaleError):
            SeedVR2Engine().validate_spec(_job(ScaleTarget(kind="factor", value=3.0)))

    def test_refuses_1_5x(self) -> None:
        with pytest.raises(UnsupportedScaleError):
            SeedVR2Engine().validate_spec(_job(ScaleTarget(kind="factor", value=1.5)))

    def test_refuses_height_target(self) -> None:
        with pytest.raises(NotYetImplementedError):
            SeedVR2Engine().validate_spec(_job(ScaleTarget(kind="height", value=1080)))


class TestModelIdentity:
    def test_default_3b_fp8(self) -> None:
        cfg: dict[str, Any] = {
            "upscale": {
                "engine": "seedvr2",
                "scale": "2x",
                "seedvr2": {"variant": "3B", "precision": "fp8"},
            }
        }
        assert SeedVR2Engine().model_identity(cfg) == "seedvr2-3b-fp8"

    def test_7b_fp16(self) -> None:
        cfg: dict[str, Any] = {
            "upscale": {
                "engine": "seedvr2",
                "scale": "2x",
                "seedvr2": {"variant": "7B", "precision": "fp16"},
            }
        }
        assert SeedVR2Engine().model_identity(cfg) == "seedvr2-7b-fp16"

    def test_empty_cfg_does_not_raise(self) -> None:
        assert SeedVR2Engine().model_identity({}) == ""

    def test_missing_seedvr2_block_does_not_raise(self) -> None:
        assert SeedVR2Engine().model_identity({"upscale": {}}) == ""


class TestRegistrySelfRegister:
    def test_registered_at_import(self) -> None:
        from kinoforge.core import registry

        eng = registry.get_upscaler("seedvr2")()
        assert eng.name == "seedvr2"


class TestRenderProvision:
    def test_emits_pip_install_and_fetch_weights(self) -> None:
        cfg: dict[str, Any] = {
            "upscale": {
                "engine": "seedvr2",
                "scale": "2x",
                "seedvr2": {"variant": "3B", "precision": "fp8"},
            }
        }
        rp = SeedVR2Engine().render_provision(cfg)
        assert "pip install" in rp.script
        assert "seedvr @ git+" in rp.script
        assert "_fetch_weights" in rp.script
        assert "--variant 3B" in rp.script
        assert "--precision fp8" in rp.script

    def test_inherits_image_and_run_cmd(self) -> None:
        rp = SeedVR2Engine().render_provision({})
        assert rp.run_cmd == []
        assert rp.image == ""
