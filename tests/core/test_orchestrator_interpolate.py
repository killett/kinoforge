"""generate() wiring for the standalone interpolate path (plan Task 7).

Mirrors tests/core/test_orchestrator_skip_clip_stage.py: patches deploy_session
to yield a fake DeploySession, stubs InterpolateStage, and drives generate() in
the standalone (skip_clip_stage=True) shape a `kinoforge interpolate` call uses.
"""

from __future__ import annotations

import dataclasses
import urllib.request
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

import kinoforge._adapters  # noqa: F401 — self-register engines + upscalers
from kinoforge.core import orchestrator, registry
from kinoforge.core.config import Config
from kinoforge.core.fps_resolver import InterpCapability
from kinoforge.core.interfaces import (
    Artifact,
    InterpolatorEngine,
    PipelineState,
)
from kinoforge.core.orchestrator import DeploySession, generate


class _FakeInterp(InterpolatorEngine):
    name = "fake-interp"
    requires_compute = True
    requires_local_weights = True
    capability = InterpCapability.ARBITRARY_TIMESTEP

    def provision(self, instance, cfg, *, cancel_token=None): ...

    def interpolate(self, instance, job, cfg, *, cancel_token=None):
        raise NotImplementedError

    def validate_spec(self, job): ...

    def model_identity(self, cfg):
        return "fake-interp"


@pytest.fixture(autouse=True)
def _register_fake_interp() -> None:
    if "fake-interp" not in registry.interpolator_names():
        registry.register_interpolator("fake-interp", _FakeInterp)


@pytest.fixture(autouse=True)
def _fresh_event_loop() -> Any:
    """Hand the next test a fresh asyncio loop.

    ``import kinoforge._adapters`` pulls in ``wan_t2v_server`` (module-level
    ``asyncio.Lock`` instances + ``app``); exercising ``generate()`` here can
    leave the process's default event loop closed/bound, which deterministically
    hangs a later subprocess smoke (the vram-rollback lora test) even though the
    server itself is fine. Install a fresh loop on teardown so no dead loop
    survives this module.
    """
    import asyncio

    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def _interp_cfg() -> Config:
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
            "interpolate": {"engine": "fake-interp", "fps": 60.0},
        }
    )


def _upscale_cfg_no_interp() -> Config:
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
    constructed: list[Any] = []

    class _SpyClipStage:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        def run(self, state: PipelineState) -> PipelineState:
            raise AssertionError("GenerateClipStage.run must not fire")

    monkeypatch.setattr(orchestrator, "GenerateClipStage", _SpyClipStage)
    return constructed


class _FakeSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish(self, data: bytes, **kwargs: Any) -> str:
        self.calls.append({"data": data, **kwargs})
        return f"/out/{kwargs.get('kind', 'x')}.mp4"


def test_interpolate_stage_appended_and_materialized(
    _fake_session: DeploySession,
    _spy_clip_stage: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug caught: cfg.interpolate set but no InterpolateStage appended -> the
    # standalone `kinoforge interpolate` call returns the input clip untouched.
    capture: dict[str, Any] = {}
    pod_uri = "http://pod-8000.proxy.runpod.net/artifacts/out.mp4"

    class _StubInterpStage:
        def __init__(self, **kwargs: Any) -> None:
            capture["init_kwargs"] = kwargs

        def run(self, state: PipelineState) -> PipelineState:
            return dataclasses.replace(
                state,
                artifacts={**state.artifacts, "interpolated": Artifact(uri=pod_uri)},
            )

    import kinoforge.pipeline.interpolate as interp_mod

    monkeypatch.setattr(interp_mod, "InterpolateStage", _StubInterpStage)

    # Stub urlopen so materialize fetches pod bytes without a real network hop.
    @contextmanager
    def fake_urlopen(req: Any, timeout: int = 0) -> Any:
        yield MagicMock(read=MagicMock(return_value=b"INTERP_BYTES"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    sink = _FakeSink()
    cfg = _interp_cfg()
    initial = Artifact(uri="file:///tmp/in.mp4", sha256="in", size=1)

    artifact, _ = generate(
        cfg,
        request=None,
        store=MagicMock(),
        sink=sink,
        run_id="r",
        skip_clip_stage=True,
        initial_clip=initial,
    )

    # Stage was constructed with the resolved engine + fps.
    assert capture["init_kwargs"]["target_fps"] == 60.0
    assert isinstance(capture["init_kwargs"]["engine"], _FakeInterp)
    # Interpolated artifact materialized + published under kind="interpolated".
    published = [c for c in sink.calls if c.get("kind") == "interpolated"]
    assert len(published) == 1
    assert published[0]["data"] == b"INTERP_BYTES"
    # Standalone entry returns the interpolated artifact, now a local file uri.
    assert artifact.uri == "file:///out/interpolated.mp4"


def test_no_interpolate_cfg_appends_no_stage(
    _fake_session: DeploySession,
    _spy_clip_stage: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug caught: an unconditional InterpolateStage append would fire for every
    # cfg, breaking plain upscale/generate runs.
    constructed: list[Any] = []

    class _SpyInterpStage:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        def run(self, state: PipelineState) -> PipelineState:
            raise AssertionError("InterpolateStage must not run for a no-interp cfg")

    class _StubUpscaleStage:
        def __init__(self, **kwargs: Any) -> None: ...

        def run(self, state: PipelineState) -> PipelineState:
            return dataclasses.replace(
                state,
                artifacts={
                    **state.artifacts,
                    "upscaled": Artifact(uri="file:///tmp/up.mp4"),
                },
            )

    import kinoforge.pipeline.interpolate as interp_mod
    import kinoforge.pipeline.upscale as upscale_mod

    monkeypatch.setattr(interp_mod, "InterpolateStage", _SpyInterpStage)
    monkeypatch.setattr(upscale_mod, "UpscaleStage", _StubUpscaleStage)

    cfg = _upscale_cfg_no_interp()
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

    assert constructed == []
