"""Tests for generate()'s skip_clip_stage + initial_clip parameters (Blocker C).

These tests exercise the new upscale-only entry through generate():
- GenerateClipStage MUST NOT be constructed.
- state.artifacts["clip"] is seeded from initial_clip before any stage runs.
- The return value is state.artifacts["upscaled"] when cfg.upscale is set.
- The default path (skip_clip_stage=False) is unchanged — covered by the
  existing orchestrator tests, not duplicated here.

Patches `deploy_session` to yield a fake DeploySession so the heavy
provider / profile-cache / pool machinery does not need to load just to
exercise the stage-list assembly + return-key branch.
"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

import kinoforge._adapters  # noqa: F401 — self-register every engine + upscaler
from kinoforge.core import orchestrator
from kinoforge.core.config import Config
from kinoforge.core.interfaces import Artifact, PipelineState
from kinoforge.core.orchestrator import DeploySession, generate


def _minimal_upscale_cfg() -> Config:
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
    """Patch deploy_session to yield a fully fake DeploySession."""
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
def _spy_clip_stage(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace GenerateClipStage with a spy that records constructions."""
    constructed: list[Any] = []

    class _SpyClipStage:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        def run(self, state: PipelineState) -> PipelineState:
            raise AssertionError("GenerateClipStage.run must not fire")

    monkeypatch.setattr(orchestrator, "GenerateClipStage", _SpyClipStage)
    return constructed


@pytest.fixture
def _stub_upscale_stage(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace UpscaleStage with a stub that captures state + sets upscaled."""
    capture: dict[str, Any] = {"pre_state": None}

    upscaled = Artifact(uri="file:///tmp/out.mp4", sha256="out", size=4096)

    class _StubUpscaleStage:
        def __init__(self, **kwargs: Any) -> None:
            capture["init_kwargs"] = kwargs

        def run(self, state: PipelineState) -> PipelineState:
            capture["pre_state"] = state
            return dataclasses.replace(
                state, artifacts={**state.artifacts, "upscaled": upscaled}
            )

    import kinoforge.pipeline.upscale as upscale_mod

    monkeypatch.setattr(upscale_mod, "UpscaleStage", _StubUpscaleStage)
    capture["upscaled_artifact"] = upscaled
    return capture


def test_skip_clip_stage_does_not_construct_generate_clip_stage(
    _fake_session: DeploySession,
    _spy_clip_stage: list[Any],
    _stub_upscale_stage: dict[str, Any],
) -> None:
    # Bug caught: skip_clip_stage flag is read but the stages list still
    # appends GenerateClipStage. The stage fires, the engine pool is
    # invoked, the orchestrator burns budget on a job the caller asked
    # to skip entirely.
    cfg = _minimal_upscale_cfg()
    initial = Artifact(uri="file:///tmp/in.mp4", sha256="in", size=1)

    generate(
        cfg,
        request=None,
        store=MagicMock(),
        sink=None,
        run_id="r",
        skip_clip_stage=True,
        initial_clip=initial,
    )

    assert _spy_clip_stage == []  # GenerateClipStage NEVER constructed


def test_skip_clip_stage_seeds_initial_clip_in_state(
    _fake_session: DeploySession,
    _spy_clip_stage: list[Any],
    _stub_upscale_stage: dict[str, Any],
) -> None:
    # Bug caught: initial_clip is ignored, UpscaleStage reads
    # state.artifacts["clip"] and hits a KeyError. Asserts the seam.
    cfg = _minimal_upscale_cfg()
    initial = Artifact(uri="file:///tmp/in.mp4", sha256="in", size=1)

    generate(
        cfg,
        request=None,
        store=MagicMock(),
        sink=None,
        run_id="r",
        skip_clip_stage=True,
        initial_clip=initial,
    )

    pre_state: PipelineState = _stub_upscale_stage["pre_state"]
    assert "clip" in pre_state.artifacts
    assert pre_state.artifacts["clip"] is initial


def test_skip_clip_stage_returns_upscaled_artifact(
    _fake_session: DeploySession,
    _spy_clip_stage: list[Any],
    _stub_upscale_stage: dict[str, Any],
) -> None:
    # Bug caught: return picks state.artifacts["clip"] unconditionally
    # and the caller gets back the INPUT video instead of the upscaled
    # output.
    cfg = _minimal_upscale_cfg()
    initial = Artifact(uri="file:///tmp/in.mp4", sha256="in", size=1)

    artifact, _ = generate(
        cfg,
        request=None,
        store=MagicMock(),
        sink=None,
        run_id="r",
        skip_clip_stage=True,
        initial_clip=initial,
    )

    assert artifact is _stub_upscale_stage["upscaled_artifact"]


def test_default_path_with_request_none_still_errors_at_validate(
    _fake_session: DeploySession,
) -> None:
    # Bug caught: the new branch silently synthesizes a placeholder
    # request on the DEFAULT path too, masking the operator's missing
    # arg with a "upscale" placeholder request that then fails
    # validation deep in the pipeline with a confusing error. The
    # default path MUST still surface request=None at validate_request
    # rather than turn it into a phantom request.
    cfg = _minimal_upscale_cfg()

    with pytest.raises((AttributeError, TypeError)):
        generate(
            cfg,
            request=None,  # default path, skip_clip_stage=False
            store=MagicMock(),
            sink=None,
            run_id="r",
        )
