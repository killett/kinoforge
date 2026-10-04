"""The materialize boundary publishes an upscaled still as .png, a clip as .mp4."""

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


def _cfg() -> Config:
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
                "scale": "2x",
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
        backend=MagicMock(),
        profile=MagicMock(),
        pool=MagicMock(),
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
    class _Stub:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, state: PipelineState) -> PipelineState:
            return dataclasses.replace(
                state, artifacts={**state.artifacts, "upscaled": upscaled}
            )

    import kinoforge.pipeline.upscale as upscale_mod

    monkeypatch.setattr(upscale_mod, "UpscaleStage", _Stub)


def _sink(tmp_path: Path) -> MagicMock:
    sink = MagicMock(name="sink")
    sink.publish = MagicMock(
        side_effect=lambda data, **kw: tmp_path / f"published{kw['extension']}"
    )
    return sink


def _run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, meta: dict[str, Any]
) -> MagicMock:
    body = tmp_path / "up.bin"
    body.write_bytes(b"bytes")
    _stub_stage(
        monkeypatch, Artifact(uri=f"file://{body}", sha256="s", size=5, meta=meta)
    )
    sink = _sink(tmp_path)
    generate(
        _cfg(),
        request=None,
        store=MagicMock(),
        sink=sink,
        run_id="r",
        skip_clip_stage=True,
        initial_clip=Artifact(uri="file:///tmp/in", sha256="i", size=1),
    )
    return sink


def test_image_publishes_as_png(
    _fake_session: DeploySession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Bug caught: the four hardcoded extension=".mp4" calls — a PNG lands on
    # disk named .mp4 and every downstream `find '*.mp4'` picks it up.
    sink = _run(monkeypatch, tmp_path, {"media": "image", "materialize": True})
    assert sink.publish.call_count == 1
    assert sink.publish.call_args.kwargs["extension"] == ".png"
    assert sink.publish.call_args.kwargs["kind"] == "upscaled"


def test_video_publishes_as_mp4(
    _fake_session: DeploySession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sink = _run(monkeypatch, tmp_path, {"materialize": True})
    assert sink.publish.call_args.kwargs["extension"] == ".mp4"


def test_image_with_downscale_to_raises_before_publish(
    _fake_session: DeploySession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Bug caught: PNG bytes fed through finalize_upscaled_bytes, an ffmpeg
    # mp4 pipeline, producing garbage or an opaque ffmpeg error.
    body = tmp_path / "up.bin"
    body.write_bytes(b"bytes")
    _stub_stage(
        monkeypatch,
        Artifact(
            uri=f"file://{body}",
            sha256="s",
            size=5,
            meta={"media": "image", "downscale_to": 1080, "materialize": True},
        ),
    )
    sink = _sink(tmp_path)
    with pytest.raises(RuntimeError, match="image"):
        generate(
            _cfg(),
            request=None,
            store=MagicMock(),
            sink=sink,
            run_id="r",
            skip_clip_stage=True,
            initial_clip=Artifact(uri="file:///tmp/in", sha256="i", size=1),
        )
    sink.publish.assert_not_called()
