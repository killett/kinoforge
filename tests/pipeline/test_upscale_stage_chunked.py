"""Tests for UpscaleStage's temporal-chunking path.

Long clips OOM FlashVSR (memory grows with frame count: 345 frames at 960x544
needed ~200 GB on 2026-09-18). With ``chunk_frames`` set the stage splits the
source losslessly, upscales each chunk on the same pod, fetches each result,
trims the overlap warm-up and concatenates — all through injectable seams so
no ffmpeg or pod is spawned here.
"""

from __future__ import annotations

from pathlib import Path

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
from kinoforge.pipeline.upscale import UpscaleStage

_X4 = ScaleTarget(kind="factor", value=4.0)


def _art(uri: str) -> Artifact:
    return Artifact(uri=uri, sha256="0" * 64, size=1)


class _FakeEngine(UpscalerEngine):
    """Records every job and answers with a distinct pod URL per call."""

    name = "fake"
    requires_compute = False
    requires_local_weights = False
    supported_scales: tuple[ScaleTarget, ...] = (ScaleTarget(kind="factor", value=4.0),)

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
            input_resolution=(960, 544),
            output_resolution=(3840, 2176),
            elapsed_s=float(n),
            engine_meta={"call": n},
        )

    def validate_spec(self, job):
        return None

    def model_identity(self, cfg):
        return "fake"


class _FakeFfmpeg:
    """Records argvs and creates each command's output file (its last token)."""

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


def _state(src: Path) -> PipelineState:
    req = GenerationRequest(prompt="p", mode="t2v")
    return PipelineState(request=req, artifacts={"clip": _art(f"file://{src}")})


def _stage(
    tmp_path: Path,
    engine: _FakeEngine,
    ffmpeg: _FakeFfmpeg,
    fetch: _FakeFetch,
    *,
    frames: int,
    chunk_frames: int | None,
    scale: ScaleTarget = _X4,
) -> UpscaleStage:
    return UpscaleStage(
        engine=engine,
        scale=scale,
        instance=None,
        cfg={},
        chunk_frames=chunk_frames,
        chunk_overlap=5,
        ffmpeg_run=ffmpeg,
        fetch=fetch,
        probe_frames=lambda p: frames,
        probe_dims=lambda p: (960, 544),
        work_dir=tmp_path / "work",
    )


def test_chunk_frames_none_calls_engine_once_with_the_original_clip(
    tmp_path: Path,
) -> None:
    # Bug caught: chunking switched on by default, changing every proven
    # single-call upscale into a split + re-encode.
    engine, ffmpeg, fetch = _FakeEngine(), _FakeFfmpeg(), _FakeFetch()
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(tmp_path, engine, ffmpeg, fetch, frames=1000, chunk_frames=None)

    out = stage.run(_state(src))

    assert [j.source.uri for j in engine.jobs] == [f"file://{src}"]
    assert ffmpeg.argvs == []
    assert fetch.urls == []
    assert out.artifacts["upscaled"].uri == "http://pod/artifacts/out1.mp4"


def test_clip_no_longer_than_a_chunk_is_not_split(tmp_path: Path) -> None:
    # Bug caught: a 30-frame clip under chunk_frames=40 is still split,
    # re-encoded and concatenated for nothing.
    engine, ffmpeg, fetch = _FakeEngine(), _FakeFfmpeg(), _FakeFetch()
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(tmp_path, engine, ffmpeg, fetch, frames=30, chunk_frames=40)

    out = stage.run(_state(src))

    assert [j.source.uri for j in engine.jobs] == [f"file://{src}"]
    assert ffmpeg.argvs == []
    assert out.artifacts["upscaled"].uri == "http://pod/artifacts/out1.mp4"


def test_long_clip_is_split_upscaled_per_chunk_fetched_and_joined(
    tmp_path: Path,
) -> None:
    # Bug caught: chunks submitted out of order, the engine called once with
    # the whole clip (the OOM), or the concat fed the wrong files / trims.
    engine, ffmpeg, fetch = _FakeEngine(), _FakeFfmpeg(), _FakeFetch()
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(tmp_path, engine, ffmpeg, fetch, frames=100, chunk_frames=40)

    out = stage.run(_state(src))

    # 100 frames / 40 with overlap 5 -> (0,40,0) (35,45,5) (75,25,5)
    split = [a for a in ffmpeg.argvs if "-vf" in a]
    assert [a[a.index("-vf") + 1] for a in split] == [
        "trim=start_frame=0:end_frame=40,setpts=PTS-STARTPTS",
        "trim=start_frame=35:end_frame=80,setpts=PTS-STARTPTS",
        "trim=start_frame=75:end_frame=100,setpts=PTS-STARTPTS",
    ]
    chunk_paths = [a[-1] for a in split]
    assert [j.source.uri for j in engine.jobs] == [f"file://{p}" for p in chunk_paths]
    assert all(Path(p).parent == tmp_path / "work" for p in chunk_paths)

    assert fetch.urls == [f"http://pod/artifacts/out{n}.mp4" for n in (1, 2, 3)]
    fetched = [p for p in (tmp_path / "work").iterdir() if p.name.startswith("up")]
    assert len(fetched) == 3
    assert {p.read_bytes() for p in fetched} == {
        f"bytes-of-http://pod/artifacts/out{n}.mp4".encode() for n in (1, 2, 3)
    }

    (concat,) = [a for a in ffmpeg.argvs if "-filter_complex" in a]
    fc = concat[concat.index("-filter_complex") + 1]
    assert fc.endswith("[v0][v1][v2]concat=n=3:v=1:a=0[v]")
    assert "[0:v]trim=start_frame=0," in fc
    assert "[1:v]trim=start_frame=5," in fc
    assert "[2:v]trim=start_frame=5," in fc

    joined = out.artifacts["upscaled"]
    assert joined.uri == f"file://{concat[-1]}"
    assert Path(concat[-1]).exists()
    assert joined.meta["chunks"] == 3
    assert joined.meta["materialize"] is True


def test_chunked_result_carries_summed_elapsed_and_output_dims(
    tmp_path: Path,
) -> None:
    # Bug caught: elapsed reported for the last chunk only (1/3 of the real
    # pod time) or resolution left at the placeholder.
    engine, ffmpeg, fetch = _FakeEngine(), _FakeFfmpeg(), _FakeFetch()
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(tmp_path, engine, ffmpeg, fetch, frames=100, chunk_frames=40)

    result = stage._run_engine(
        _art(f"file://{src}"), ScaleTarget(kind="factor", value=4.0)
    )

    assert result.elapsed_s == pytest.approx(1.0 + 2.0 + 3.0)
    assert result.output_resolution == (3840, 2176)
    assert result.input_resolution == (960, 544)
    assert result.engine_meta["chunks"] == 3


def test_height_target_still_stashes_downscale_on_the_joined_clip(
    tmp_path: Path,
) -> None:
    # Bug caught: the chunked branch returns early and skips _stash, so the
    # 3840x2176 join is published as-is instead of lanczos'd to 1080p.
    engine, ffmpeg, fetch = _FakeEngine(), _FakeFfmpeg(), _FakeFetch()
    src = tmp_path / "in.mp4"
    src.write_bytes(b"src")
    stage = _stage(
        tmp_path,
        engine,
        ffmpeg,
        fetch,
        frames=100,
        chunk_frames=40,
        scale=ScaleTarget(kind="height", value=1080.0),
    )

    out = stage.run(_state(src))

    joined = out.artifacts["upscaled"]
    assert joined.meta["downscale_to"] == 1080
    assert joined.meta["chunks"] == 3
    assert len(engine.jobs) == 3
    assert all(j.scale == ScaleTarget(kind="factor", value=4.0) for j in engine.jobs)


def test_remote_source_cannot_be_chunked(tmp_path: Path) -> None:
    # Bug caught: an http(s) source is handed to ffmpeg's trim as if it were
    # a local path, failing minutes into the pod's life instead of at once.
    engine, ffmpeg, fetch = _FakeEngine(), _FakeFfmpeg(), _FakeFetch()
    stage = _stage(tmp_path, engine, ffmpeg, fetch, frames=100, chunk_frames=40)
    req = GenerationRequest(prompt="p", mode="t2v")
    state = PipelineState(
        request=req, artifacts={"clip": _art("https://example.com/in.mp4")}
    )

    with pytest.raises(ValueError, match="chunk"):
        stage.run(state)
    assert engine.jobs == []
