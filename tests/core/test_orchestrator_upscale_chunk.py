"""Orchestrator wiring for chunked upscales.

Two seams: cfg.upscale.chunk_frames / chunk_overlap must reach the
UpscaleStage constructor, and a chunked result (a controller-local ``file://``
join carrying ``meta["materialize"]``) must be published through the sink even
when no height-target downscale is pending — the pre-existing materialize gate
only fired for pod URLs or ``downscale_to``.
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


def _chunked_upscale_cfg() -> Config:
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
                "chunk_frames": 69,
                "chunk_overlap": 8,
                "tile_grid": [2, 2],
                "tile_overlap": 32,
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


@pytest.fixture
def _stub_upscale_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Any]:
    """UpscaleStage stub returning a chunked, controller-local join."""
    joined = tmp_path / "joined.mp4"
    joined.write_bytes(b"joined-bytes")
    capture: dict[str, Any] = {}
    upscaled = Artifact(
        uri=f"file://{joined}",
        sha256="j",
        size=12,
        meta={"chunks": 3, "materialize": True},
    )

    class _StubUpscaleStage:
        def __init__(self, **kwargs: Any) -> None:
            capture["init_kwargs"] = kwargs

        def run(self, state: PipelineState) -> PipelineState:
            return dataclasses.replace(
                state, artifacts={**state.artifacts, "upscaled": upscaled}
            )

    import kinoforge.pipeline.upscale as upscale_mod

    monkeypatch.setattr(upscale_mod, "UpscaleStage", _StubUpscaleStage)
    return capture


def test_chunk_knobs_reach_the_stage(
    _fake_session: DeploySession, _stub_upscale_stage: dict[str, Any]
) -> None:
    # Bug caught: the cfg fields validate fine and are never threaded into
    # the stage, so chunking silently stays off and the pod OOMs anyway.
    generate(
        _chunked_upscale_cfg(),
        request=None,
        store=MagicMock(),
        sink=None,
        run_id="r",
        skip_clip_stage=True,
        initial_clip=Artifact(uri="file:///tmp/in.mp4", sha256="in", size=1),
    )
    kwargs = _stub_upscale_stage["init_kwargs"]
    assert kwargs["chunk_frames"] == 69
    assert kwargs["chunk_overlap"] == 8
    assert kwargs["tile_grid"] == (2, 2)
    assert kwargs["tile_overlap"] == 32


def test_chunked_local_join_is_published_without_a_downscale(
    _fake_session: DeploySession, _stub_upscale_stage: dict[str, Any], tmp_path: Path
) -> None:
    # Bug caught: the materialize gate ignores a file:// result with no
    # downscale_to, so a factor-scale chunked upscale never lands in output/.
    published = tmp_path / "out" / "published.mp4"
    sink = MagicMock(name="sink")
    sink.publish = MagicMock(return_value=published)

    artifact, _ = generate(
        _chunked_upscale_cfg(),
        request=None,
        store=MagicMock(),
        sink=sink,
        run_id="r",
        skip_clip_stage=True,
        initial_clip=Artifact(uri="file:///tmp/in.mp4", sha256="in", size=1),
    )

    sink.publish.assert_called_once()
    assert sink.publish.call_args.args[0] == b"joined-bytes"
    assert sink.publish.call_args.kwargs["kind"] == "upscaled"
    assert artifact.uri == f"file://{published}"
