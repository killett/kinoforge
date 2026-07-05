"""UpscaleStage height-target behaviour."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import ScaleUnsatisfiableError
from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    PipelineState,
    UpscaleResult,
)
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.pipeline.upscale import UpscaleStage


class FakeEngine:
    """Records the scale it was asked to run and returns a canned result."""

    def __init__(self, factors: tuple[float, ...], out_res: tuple[int, int]) -> None:
        self.supported_scales = tuple(
            ScaleTarget(kind="factor", value=f) for f in factors
        )
        self._out_res = out_res
        self.calls: list[ScaleTarget] = []

    def upscale(self, instance, job, cfg, *, cancel_token=None):  # noqa: ANN001
        self.calls.append(job.scale)
        return UpscaleResult(
            artifact=Artifact(uri="https://pod-8000.proxy.runpod.net/artifacts/x"),
            input_resolution=(480, 480),
            output_resolution=self._out_res,
            elapsed_s=1.0,
        )


def _state(uri: str = "https://pod/clip.mp4") -> PipelineState:
    return PipelineState(
        request=GenerationRequest(prompt="", mode="upscale"),
        artifacts={"clip": Artifact(uri=uri)},
    )


def _stage(engine, scale, *, probe_dims=None):  # noqa: ANN001, ANN202
    return UpscaleStage(
        engine=engine,
        scale=scale,
        instance=None,
        cfg={},
        probe_dims=probe_dims or (lambda p: (480, 480)),
    )


def test_factor_target_unchanged() -> None:
    # Behaviour: a plain 4x factor still runs the engine with that scale and sets
    # no downscale meta. Bug caught: height logic leaking into the factor path.
    eng = FakeEngine((4.0,), (1920, 1920))
    out = _stage(eng, ScaleTarget(kind="factor", value=4.0)).run(_state())
    assert eng.calls == [ScaleTarget(kind="factor", value=4.0)]
    assert "downscale_to" not in out.artifacts["upscaled"].meta


def test_single_factor_overshoot_stashes_downscale() -> None:
    # Behaviour: 1080p on a 4x engine -> run 4x, output 1920 > 1080 -> stash 1080.
    eng = FakeEngine((4.0,), (1920, 1920))
    out = _stage(eng, ScaleTarget(kind="height", value=1080)).run(_state())
    assert eng.calls == [ScaleTarget(kind="factor", value=4.0)]
    assert out.artifacts["upscaled"].meta["downscale_to"] == 1080


def test_single_factor_exact_output_no_downscale() -> None:
    # Behaviour: output height already == target -> no downscale meta.
    eng = FakeEngine((4.0,), (1920, 1080))
    out = _stage(eng, ScaleTarget(kind="height", value=1080)).run(_state())
    assert "downscale_to" not in out.artifacts["upscaled"].meta


def test_single_factor_undershoot_raises() -> None:
    # Behaviour: 4x output still below target -> ScaleUnsatisfiableError.
    eng = FakeEngine((4.0,), (960, 960))
    with pytest.raises(ScaleUnsatisfiableError):
        _stage(eng, ScaleTarget(kind="height", value=1080)).run(_state())


def test_downscale_only_skips_engine() -> None:
    # Behaviour: local source 1080p, want 720p -> engine untouched, stash 720.
    eng = FakeEngine((2.0, 4.0), (0, 0))
    stage = _stage(
        eng,
        ScaleTarget(kind="height", value=720),
        probe_dims=lambda p: (1920, 1080),
    )
    out = stage.run(_state(uri="file:///tmp/clip.mp4"))
    assert eng.calls == []
    assert out.artifacts["upscaled"].meta["downscale_to"] == 720
