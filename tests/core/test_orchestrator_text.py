"""generate(skip_clip_stage=True) with a text: cfg appends TextStage and returns 'text'."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import kinoforge._adapters  # noqa: F401
from kinoforge.core import orchestrator, registry
from kinoforge.core.config import Config
from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    PipelineState,
    TextEngine,
    TextHealth,
    TextResult,
)
from kinoforge.core.orchestrator import DeploySession, generate
from kinoforge.stores.local import LocalArtifactStore


class _FakeTextEngine(TextEngine):
    name = "fake-text"
    requires_compute = True

    def health(self, instance, cfg):  # noqa: ANN001, ANN201
        return TextHealth(ready=True, model="m", supported_modes=frozenset({"t2t"}))

    def upload_image(self, instance, local_path, cfg):  # noqa: ANN001, ANN201
        raise AssertionError("no images in this test")

    def complete(self, instance, job, cfg, *, cancel_token=None):  # noqa: ANN001, ANN201
        return TextResult(
            text="pong", finish_reason="stop", usage={}, model="m", elapsed_s=0.1
        )

    def validate_spec(self, job):  # noqa: ANN001, ANN201
        return None

    def model_identity(self, cfg):  # noqa: ANN001, ANN201
        return "m"


def _text_cfg() -> Config:
    return Config.model_validate(
        {
            "engine": {
                "kind": "diffusers",
                "precision": "bf16",
                "diffusers": {"capability": {"supported_modes": ["t2t"]}},
            },
            "models": [
                {"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}
            ],
            "text": {"engine": "fake-text"},
            "compute": {"provider": "fake", "image": "fake:latest"},
        }
    )


@pytest.fixture
def _fake_session(monkeypatch: pytest.MonkeyPatch) -> DeploySession:
    fake_engine = MagicMock(name="GenerationEngine")
    fake_engine.name = "diffusers"
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


@pytest.fixture
def _fake_text_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry,
        "_text_engines",
        {**registry._text_engines, "fake-text": _FakeTextEngine},
    )  # noqa: SLF001


@pytest.mark.usefixtures("_fake_session", "_fake_text_engine")
def test_text_cfg_returns_the_text_artifact_without_a_clip_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: artifact_key falling through to "clip" (KeyError on the
    skip path), or GenerateClipStage constructed for a text run."""
    constructed: list[Any] = []

    class _SpyClip:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        def run(self, state: PipelineState) -> PipelineState:
            raise AssertionError("clip stage must not run")

    monkeypatch.setattr(orchestrator, "GenerateClipStage", _SpyClip)
    artifact, instance = generate(
        _text_cfg(),
        request=GenerationRequest(prompt="ping", mode="t2t"),
        store=LocalArtifactStore(tmp_path / "store"),
        run_id="text-orch",
        state_dir=tmp_path / "state",
        sink=None,
        skip_clip_stage=True,
    )
    assert constructed == []
    assert isinstance(artifact, Artifact)
    assert artifact.meta["text"] == "pong"
    assert instance is None
