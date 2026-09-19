"""Tests for UpscaleStage's spatial-tiling path.

FlashVSR's attention canvas is its 4x output, so a 960x544 source cannot be
upscaled whole on any card. With ``tile_grid`` set the stage crops the source
into aligned tiles, runs each through the (possibly chunked) upscale on the
same pod, localises each result and feather-stitches them — all through
injectable seams, so no ffmpeg or pod is spawned here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    PipelineState,
    UpscaleJob,
    UpscalerEngine,
    UpscaleResult,
)
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.pipeline.tile import TileSpec
from kinoforge.pipeline.upscale import UpscaleStage

_X4 = ScaleTarget(kind="factor", value=4.0)


def _art(uri: str) -> Artifact:
    return Artifact(uri=uri, sha256="0" * 64, size=1)


class _FakeEngine(UpscalerEngine):
    """Records jobs; answers with a pod URL and a 4x output of a 512x288 tile."""

    name = "fake"
    requires_compute = False
    requires_local_weights = False
    supported_scales: tuple[ScaleTarget, ...] = (_X4,)

    def __init__(self) -> None:
        self.jobs: list[UpscaleJob] = []

    def provision(self, instance, cfg, *, cancel_token=None):
        return None

    def upscale(self, instance, job, cfg, *, cancel_token=None):
        self.jobs.append(job)
        n = len(self.jobs)
        return UpscaleResult(
            artifact=Artifact(
                uri=f"http://pod/artifacts/out{n}.mp4", sha256="a", size=9
            ),
            input_resolution=(512, 288),
            output_resolution=(2048, 1152),
            elapsed_s=1.0,
            engine_meta={"call": n},
        )

    def validate_spec(self, job):
        return None

    def model_identity(self, cfg):
        return "fake"


class _FakeFfmpeg:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str], stdin: bytes) -> bytes:
        self.argvs.append(argv)
        Path(argv[-1]).write_bytes(b"mp4")
        return b""


class _FakeFetch:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return f"bytes-of-{url}".encode()


class _FakeStitch:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        tile_paths: list[str],
        tiles: list[TileSpec],
        *,
        canvas_w: int,
        canvas_h: int,
        scale: int,
        fps: float,
        out_path: str,
    ) -> None:
        self.calls.append(
            {
                "tile_paths": list(tile_paths),
                "tiles": list(tiles),
                "canvas_w": canvas_w,
                "canvas_h": canvas_h,
                "scale": scale,
                "fps": fps,
                "out_path": out_path,
            }
        )
        Path(out_path).write_bytes(b"stitched")


def _state(src: Path) -> PipelineState:
    req = GenerationRequest(prompt="p", mode="t2v")
    return PipelineState(request=req, artifacts={"clip": _art(f"file://{src}")})


def _stage(
    tmp_path: Path,
    engine: _FakeEngine,
    ffmpeg: _FakeFfmpeg,
    fetch: _FakeFetch,
    stitch: _FakeStitch,
    *,
    tile_grid: tuple[int, int] | None,
    chunk_frames: int | None = None,
    frames: int = 100,
    scale: ScaleTarget = _X4,
) -> UpscaleStage:
    return UpscaleStage(
        engine=engine,
        scale=scale,
        instance=None,
        cfg={},
        tile_grid=tile_grid,
        tile_overlap=32,
        chunk_frames=chunk_frames,
        chunk_overlap=5,
        ffmpeg_run=ffmpeg,
        fetch=fetch,
        stitch=stitch,
        probe_frames=lambda p: frames,
        # the source is 960x544; every localised upscaled tile is 2048x1152
        probe_dims=lambda p: (960, 544) if Path(p).name == "in.mp4" else (2048, 1152),
        probe_fps=lambda p: 24.0,
        work_dir=tmp_path / "work",
    )


def test_tile_grid_none_leaves_the_single_call_path_alone(tmp_path: Path) -> None:
    # Bug caught: tiling switched on by default, cropping every proven
    # single-call upscale into four pod calls.
    engine, ffmpeg, fetch, stitch = (
        _FakeEngine(),
        _FakeFfmpeg(),
        _FakeFetch(),
        _FakeStitch(),
    )
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(tmp_path, engine, ffmpeg, fetch, stitch, tile_grid=None)

    out = stage.run(_state(src))

    assert [j.source.uri for j in engine.jobs] == [f"file://{src}"]
    assert ffmpeg.argvs == [] and stitch.calls == []
    assert out.artifacts["upscaled"].uri == "http://pod/artifacts/out1.mp4"


def test_two_by_two_grid_crops_upscales_fetches_and_stitches(tmp_path: Path) -> None:
    # Bug caught: wrong crop boxes, tiles submitted out of order, the stitch
    # fed pod URLs instead of local files, or the wrong scale/canvas.
    engine, ffmpeg, fetch, stitch = (
        _FakeEngine(),
        _FakeFfmpeg(),
        _FakeFetch(),
        _FakeStitch(),
    )
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(tmp_path, engine, ffmpeg, fetch, stitch, tile_grid=(2, 2))

    out = stage.run(_state(src))

    crops = [
        a
        for a in ffmpeg.argvs
        if "-vf" in a and a[a.index("-vf") + 1].startswith("crop=")
    ]
    assert [a[a.index("-vf") + 1] for a in crops] == [
        "crop=512:288:0:0",
        "crop=512:288:448:0",
        "crop=512:288:0:256",
        "crop=512:288:448:256",
    ]
    tile_paths = [a[-1] for a in crops]
    assert [j.source.uri for j in engine.jobs] == [f"file://{p}" for p in tile_paths]
    assert fetch.urls == [f"http://pod/artifacts/out{n}.mp4" for n in (1, 2, 3, 4)]

    (call,) = stitch.calls
    assert call["tiles"] == [
        TileSpec(x=0, y=0, w=512, h=288),
        TileSpec(x=448, y=0, w=512, h=288),
        TileSpec(x=0, y=256, w=512, h=288),
        TileSpec(x=448, y=256, w=512, h=288),
    ]
    assert len(call["tile_paths"]) == 4
    assert all(
        Path(p).read_bytes() == f"bytes-of-http://pod/artifacts/out{n}.mp4".encode()
        for n, p in enumerate(call["tile_paths"], start=1)
    )
    assert (call["canvas_w"], call["canvas_h"], call["scale"], call["fps"]) == (
        960,
        544,
        4,
        24.0,
    )

    joined = out.artifacts["upscaled"]
    assert joined.uri == f"file://{call['out_path']}"
    assert joined.meta["tiles"] == 4
    assert joined.meta["materialize"] is True


def test_tiling_composes_with_temporal_chunking(tmp_path: Path) -> None:
    # Bug caught: the tile path calling the engine once per tile with the
    # whole clip — re-introducing the frame-count OOM inside every tile.
    engine, ffmpeg, fetch, stitch = (
        _FakeEngine(),
        _FakeFfmpeg(),
        _FakeFetch(),
        _FakeStitch(),
    )
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(
        tmp_path,
        engine,
        ffmpeg,
        fetch,
        stitch,
        tile_grid=(2, 2),
        chunk_frames=40,
        frames=100,
    )

    stage.run(_state(src))

    # 4 tiles x 3 chunks (100 frames / 40 with overlap 5) = 12 engine calls
    assert len(engine.jobs) == 12
    joins = [a for a in ffmpeg.argvs if "-filter_complex" in a]
    assert len(joins) == 4
    (call,) = stitch.calls
    # each tile's stitched input is its chunk JOIN (a local file), not a pod URL
    assert [Path(p).name for p in call["tile_paths"]] == ["joined.mp4"] * 4
    assert len({Path(p).parent for p in call["tile_paths"]}) == 4


def test_tiled_result_reports_full_canvas_resolution_and_summed_elapsed(
    tmp_path: Path,
) -> None:
    # Bug caught: output_resolution left at one tile's 2048x1152, which makes
    # the height-target resolver believe 1080p was not reached.
    engine, ffmpeg, fetch, stitch = (
        _FakeEngine(),
        _FakeFfmpeg(),
        _FakeFetch(),
        _FakeStitch(),
    )
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(tmp_path, engine, ffmpeg, fetch, stitch, tile_grid=(2, 2))

    result = stage._run_engine(_art(f"file://{src}"), _X4)

    assert result.output_resolution == (3840, 2176)
    assert result.input_resolution == (960, 544)
    assert result.elapsed_s == pytest.approx(4.0)
    assert result.engine_meta["tiles"] == 4


def test_height_target_stashes_downscale_on_the_stitched_canvas(tmp_path: Path) -> None:
    # Bug caught: the tiled branch bypassing _stash, publishing 3840x2176.
    engine, ffmpeg, fetch, stitch = (
        _FakeEngine(),
        _FakeFfmpeg(),
        _FakeFetch(),
        _FakeStitch(),
    )
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(
        tmp_path,
        engine,
        ffmpeg,
        fetch,
        stitch,
        tile_grid=(2, 2),
        scale=ScaleTarget(kind="height", value=1080.0),
    )

    out = stage.run(_state(src))

    assert out.artifacts["upscaled"].meta["downscale_to"] == 1080
    assert out.artifacts["upscaled"].meta["tiles"] == 4
    assert all(j.scale == _X4 for j in engine.jobs)


def test_remote_source_cannot_be_tiled(tmp_path: Path) -> None:
    # Bug caught: an http(s) source handed to ffmpeg's crop, failing minutes
    # into the pod's life instead of at once.
    engine, ffmpeg, fetch, stitch = (
        _FakeEngine(),
        _FakeFfmpeg(),
        _FakeFetch(),
        _FakeStitch(),
    )
    stage = _stage(tmp_path, engine, ffmpeg, fetch, stitch, tile_grid=(2, 2))
    state = PipelineState(
        request=GenerationRequest(prompt="p", mode="t2v"),
        artifacts={"clip": _art("https://example.com/in.mp4")},
    )

    with pytest.raises(ValueError, match="tile"):
        stage.run(state)
    assert engine.jobs == []


def test_tile_scale_comes_from_the_localised_file_not_the_reported_resolution(
    tmp_path: Path,
) -> None:
    # Bug caught: the 2026-09-18 live failure — the pod reported
    # output_resolution (0, 0), the stage derived scale 0 from it and the
    # stitch encoder died on "video_size 0x0" (attempt 1 spun forever on
    # zero-byte frames). The upscaled tile FILE is the only trustworthy source.
    class _ZeroResEngine(_FakeEngine):
        def upscale(self, instance, job, cfg, *, cancel_token=None):
            r = super().upscale(instance, job, cfg, cancel_token=cancel_token)
            return UpscaleResult(
                artifact=r.artifact,
                input_resolution=(0, 0),
                output_resolution=(0, 0),
                elapsed_s=r.elapsed_s,
                engine_meta=r.engine_meta,
            )

    engine, ffmpeg, fetch, stitch = (
        _ZeroResEngine(),
        _FakeFfmpeg(),
        _FakeFetch(),
        _FakeStitch(),
    )
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")

    def probe_dims(p: str | Path) -> tuple[int, int]:
        # the source is 960x544; every localised upscaled tile is 2048x1152
        return (960, 544) if Path(p) == src else (2048, 1152)

    stage = _stage(tmp_path, engine, ffmpeg, fetch, stitch, tile_grid=(2, 2))
    stage.probe_dims = probe_dims

    result = stage._run_engine(_art(f"file://{src}"), _X4)

    (call,) = stitch.calls
    assert call["scale"] == 4
    assert result.output_resolution == (3840, 2176)
