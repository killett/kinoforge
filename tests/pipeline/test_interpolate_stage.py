"""InterpolateStage routing: engine vs local-decimate vs passthrough."""

from kinoforge.core.fps_resolver import InterpCapability
from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    InterpolateResult,
    PipelineState,
)
from kinoforge.pipeline.interpolate import InterpolateStage


class _Engine:
    name = "rife"
    capability = InterpCapability.ARBITRARY_TIMESTEP

    def __init__(self):
        self.calls = 0

    def interpolate(self, instance, job, cfg, *, cancel_token=None):
        self.calls += 1
        return InterpolateResult(
            artifact=Artifact(uri="http://pod/artifacts/out.mp4"),
            input_fps=16.0,
            output_fps=job.target_fps,
            input_frame_count=16,
            output_frame_count=60,
            elapsed_s=1.0,
        )


def _state(uri: str) -> PipelineState:
    return PipelineState(
        request=GenerationRequest(prompt="", mode="upscale"),
        artifacts={"clip": Artifact(uri=uri)},
    )


def _stage(engine, target_fps, **kw):
    return InterpolateStage(
        engine=engine,
        target_fps=target_fps,
        instance=None,
        cfg={},
        probe_fps=kw.get("probe_fps", lambda p: 16.0),
        probe_count=kw.get("probe_count", lambda p: 16),
        decimate=kw.get("decimate", lambda b, f: b"DECIMATED"),
        read_bytes=kw.get("read_bytes", lambda p: b"CLIP"),
        publish=kw.get("publish", lambda b: "file:///out/deci.mp4"),
    )


def test_arbitrary_upshift_calls_engine():
    eng = _Engine()
    out = _stage(eng, 60.0).run(_state("file:///in.mp4"))
    assert eng.calls == 1
    assert out.artifacts["interpolated"].uri == "http://pod/artifacts/out.mp4"


def test_downshift_decimates_locally_no_engine():
    eng = _Engine()
    out = _stage(eng, 12.0).run(_state("file:///in.mp4"))
    assert eng.calls == 0
    assert out.artifacts["interpolated"].uri == "file:///out/deci.mp4"


def test_equal_fps_passthrough():
    eng = _Engine()
    st = _state("file:///in.mp4")
    out = _stage(eng, 16.0).run(st)
    assert eng.calls == 0
    assert out.artifacts["interpolated"].uri == "file:///in.mp4"


def test_remote_source_always_calls_engine():
    eng = _Engine()

    # probe_fps must NOT be consulted for http; force it to blow up if used.
    def boom(p):
        raise AssertionError("probed a remote source locally")

    _stage(eng, 60.0, probe_fps=boom, probe_count=boom).run(_state("http://pod/in.mp4"))
    assert eng.calls == 1
