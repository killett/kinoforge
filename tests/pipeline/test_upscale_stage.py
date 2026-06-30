"""Tests for UpscaleStage."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    PipelineState,
    UpscaleJob,
    UpscalerEngine,
    UpscaleResult,
)
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.pipeline.upscale import UpscaleStage


def _art(uri: str) -> Artifact:
    return Artifact(uri=uri, sha256="0" * 64, size=1)


class _FakeEngine(UpscalerEngine):
    """Tiny stand-in honouring the UpscalerEngine surface for stage tests."""

    name = "fake"
    requires_compute = False
    requires_local_weights = False
    supported_scales: tuple[ScaleTarget, ...] = ()

    def __init__(self) -> None:
        self.called_with: list[UpscaleJob] = []

    def provision(self, instance, cfg, *, cancel_token=None):
        return None

    def upscale(self, instance, job, cfg, *, cancel_token=None):
        self.called_with.append(job)
        return UpscaleResult(
            artifact=_art("file:///tmp/out.mp4"),
            input_resolution=(640, 480),
            output_resolution=(1280, 960),
            elapsed_s=1.0,
        )

    def validate_spec(self, job):
        return None

    def model_identity(self, cfg):
        return "fake"


def _state(with_clip: bool = True) -> PipelineState:
    req = GenerationRequest(prompt="p", mode="t2v")
    artifacts = {"clip": _art("file:///tmp/in.mp4")} if with_clip else {}
    return PipelineState(request=req, artifacts=artifacts)


class TestUpscaleStageHappyPath:
    def test_writes_upscaled(self) -> None:
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng,
            scale=ScaleTarget(kind="factor", value=2.0),
            instance=None,
            cfg={},
        )
        out = stage.run(_state())
        assert "upscaled" in out.artifacts
        assert out.artifacts["upscaled"].uri == "file:///tmp/out.mp4"

    def test_preserves_clip(self) -> None:
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng,
            scale=ScaleTarget(kind="factor", value=2.0),
            instance=None,
            cfg={},
        )
        out = stage.run(_state())
        assert out.artifacts["clip"].uri == "file:///tmp/in.mp4"

    def test_passes_scale_to_engine(self) -> None:
        eng = _FakeEngine()
        scale = ScaleTarget(kind="factor", value=4.0)
        stage = UpscaleStage(engine=eng, scale=scale, instance=None, cfg={})
        stage.run(_state())
        assert eng.called_with[0].scale == scale


class TestUpscaleStageFailureModes:
    def test_missing_clip_raises_keyerror(self) -> None:
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng,
            scale=ScaleTarget(kind="factor", value=2.0),
            instance=None,
            cfg={},
        )
        with pytest.raises(KeyError, match="clip"):
            stage.run(_state(with_clip=False))

    def test_height_scale_refused(self) -> None:
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng,
            scale=ScaleTarget(kind="height", value=1080.0),
            instance=None,
            cfg={},
        )
        with pytest.raises(NotYetImplementedError, match="1080p deferred"):
            stage.run(_state())
