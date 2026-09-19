"""The pre-downscale (full-resolution) upscale is kept, not just the 1080p.

A height-target upscale overshoots (FlashVSR renders 4x, e.g. 3840x2176 from a
tiled 960x544 source) and the materialize boundary lanczos-downscales it. The
overshoot used to be discarded; it is now published first, as ``kind="fullres"``
— a kind that the operator's ``find -name '*upscaled*.mp4'`` selection never
matches, so the downstream RIFE stage still receives the 1080p file.
"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import kinoforge._adapters  # noqa: F401 — self-register every engine + upscaler
from kinoforge.core import orchestrator
from kinoforge.core.config import Config
from kinoforge.core.interfaces import Artifact, PipelineState
from kinoforge.core.orchestrator import DeploySession, generate


def _upscale_cfg() -> Config:
    return Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [
                {
                    "kind": "base",
                    "ref": "hf:Wan-AI/Wan2.2-T2V",
                    "target": "diffusion_models",
                }
            ],
            "compute": {"provider": "fake", "image": "fake:latest"},
            "upscale": {
                "engine": "spandrel",
                "scale": "1080p",
                "spandrel": {
                    "model_url": "hf:foo/bar.pth",
                    "arch": "realesrgan",
                    "precision": "fp16",
                    "tile_size": 512,
                    "batch_size": 4,
                },
            },
        }
    )


@pytest.fixture
def _fake_session(monkeypatch: pytest.MonkeyPatch) -> DeploySession:
    fake_engine = MagicMock(name="GenerationEngine")
    fake_engine.name = "diffusers"
    fake_engine.model_identity = MagicMock(return_value="fake-model")
    fake_engine.accepted_kinds = {"image"}
    session = DeploySession(
        backend=MagicMock(name="backend"),
        profile=MagicMock(name="profile"),
        pool=MagicMock(name="pool"),
        instance=None,
        engine=fake_engine,
        provider=None,
    )

    @contextmanager
    def fake_deploy(*args: Any, **kwargs: Any) -> Any:
        yield session

    monkeypatch.setattr(orchestrator, "deploy_session", fake_deploy)
    return session


def _stub_stage(monkeypatch: pytest.MonkeyPatch, upscaled: Artifact) -> None:
    class _StubUpscaleStage:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, state: PipelineState) -> PipelineState:
            return dataclasses.replace(
                state, artifacts={**state.artifacts, "upscaled": upscaled}
            )

    import kinoforge.pipeline.upscale as upscale_mod

    monkeypatch.setattr(upscale_mod, "UpscaleStage", _StubUpscaleStage)


def _sink(tmp_path: Path) -> MagicMock:
    sink = MagicMock(name="sink")
    counter = {"n": 0}

    def publish(data: bytes, **kwargs: Any) -> Path:
        counter["n"] += 1
        return tmp_path / f"published{counter['n']}.mp4"

    sink.publish = MagicMock(side_effect=publish)
    return sink


def test_overshoot_is_published_as_fullres_before_the_downscaled_file(
    _fake_session: DeploySession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Bug caught: the 3840x2176 stitched canvas thrown away at the materialize
    # boundary, or published under a kind containing "upscaled" so the
    # operator's `find '*upscaled*' | sort | tail -1` hands RIFE the 4x canvas.
    canvas = tmp_path / "stitched.mp4"
    canvas.write_bytes(b"fullres-bytes")
    _stub_stage(
        monkeypatch,
        Artifact(
            uri=f"file://{canvas}",
            sha256="c",
            size=13,
            meta={"downscale_to": 1080, "tiles": 4, "materialize": True},
        ),
    )
    import kinoforge.pipeline.materialize as mat

    monkeypatch.setattr(
        mat, "finalize_upscaled_bytes", lambda body, h: b"downscaled-" + str(h).encode()
    )
    sink = _sink(tmp_path)

    artifact, _ = generate(
        _upscale_cfg(),
        request=None,
        store=MagicMock(),
        sink=sink,
        run_id="r",
        skip_clip_stage=True,
        initial_clip=Artifact(uri="file:///tmp/in.mp4", sha256="in", size=1),
    )

    calls = sink.publish.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == b"fullres-bytes"
    assert calls[0].kwargs["kind"] == "fullres"
    assert "upscaled" not in calls[0].kwargs["kind"]
    assert calls[1].args[0] == b"downscaled-1080"
    assert calls[1].kwargs["kind"] == "upscaled"
    for c in calls:
        assert (c.kwargs["provider"], c.kwargs["model"]) == ("spandrel", "unknown")
    # the pipeline continues with the 1080p file, not the full-res one
    assert artifact.uri == f"file://{tmp_path / 'published2.mp4'}"


def test_no_overshoot_publishes_exactly_once(
    _fake_session: DeploySession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Bug caught: a duplicate "fullres" copy of every ordinary upscale.
    joined = tmp_path / "joined.mp4"
    joined.write_bytes(b"native-bytes")
    _stub_stage(
        monkeypatch,
        Artifact(
            uri=f"file://{joined}", sha256="j", size=12, meta={"materialize": True}
        ),
    )
    sink = _sink(tmp_path)

    generate(
        _upscale_cfg(),
        request=None,
        store=MagicMock(),
        sink=sink,
        run_id="r",
        skip_clip_stage=True,
        initial_clip=Artifact(uri="file:///tmp/in.mp4", sha256="in", size=1),
    )

    calls = sink.publish.call_args_list
    assert len(calls) == 1
    assert calls[0].args[0] == b"native-bytes"
    assert calls[0].kwargs["kind"] == "upscaled"
