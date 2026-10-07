"""TextStage (design §7.1): health BEFORE upload BEFORE submit; store + publish."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationRequest,
    PipelineState,
    TextEngine,
    TextHealth,
    TextJob,
    TextResult,
)
from kinoforge.pipeline.text import TextStage
from kinoforge.stores.local import LocalArtifactStore


class _CountingEngine(TextEngine):
    """Records every call; the health modes are configurable per test."""

    name = "transformers"
    requires_compute = True

    def __init__(self, modes: frozenset[str], *, ready: bool = True) -> None:
        self.modes = modes
        self.ready = ready
        self.health_calls = 0
        self.uploads: list[Path] = []
        self.jobs: list[TextJob] = []

    def health(self, instance, cfg):  # noqa: ANN001, ANN201
        self.health_calls += 1
        return TextHealth(
            ready=self.ready, model="pod-model", supported_modes=self.modes
        )

    def upload_image(self, instance, local_path, cfg):  # noqa: ANN001, ANN201
        self.uploads.append(Path(local_path))
        return f"/tmp/kf-uploads/{Path(local_path).name}"

    def complete(self, instance, job, cfg, *, cancel_token=None):  # noqa: ANN001, ANN201
        self.jobs.append(job)
        return TextResult(
            text="the answer",
            finish_reason="stop",
            usage={"prompt_tokens": 4, "completion_tokens": 2},
            model="pod-model",
            elapsed_s=1.5,
        )

    def validate_spec(self, job):  # noqa: ANN001, ANN201
        return None

    def model_identity(self, cfg):  # noqa: ANN001, ANN201
        return "Qwen3-0.6B"


class _RecordingSink:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, Any]] = []

    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        kind: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "data": data,
                "prompt": prompt,
                "extension": extension,
                "provider": provider,
                "model": model,
                "kind": kind,
            }
        )
        path = self.root / f"out{extension}"
        path.write_bytes(data)
        return str(path)


def _cfg(system: str | None = "Be terse.") -> dict[str, Any]:
    return {
        "engine": {
            "kind": "diffusers",
            "precision": "bf16",
            "diffusers": {"capability": {"supported_modes": ["t2t", "it2t"]}},
        },
        "models": [
            {"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}
        ],
        "text": {
            "engine": "transformers",
            "system": system,
            "params": {"max_new_tokens": 8},
            "port": 8000,
        },
    }


def _image_asset(tmp_path: Path, name: str, role: str) -> ConditioningAsset:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG")
    return ConditioningAsset(
        kind="image",
        role=role,
        ref=Artifact(uri=f"file://{p}", sha256="s" * 64, size=4),
    )


def _stage(
    tmp_path: Path, engine: TextEngine, sink: _RecordingSink | None
) -> TextStage:
    return TextStage(
        engine=engine,
        instance=None,
        cfg=_cfg(),
        store=LocalArtifactStore(tmp_path / "store"),
        sink=sink,
        run_id="text-test",
        cancel_token=None,
    )


def test_a_pod_still_loading_is_refused_as_not_ready_not_as_a_bad_config(
    tmp_path: Path,
) -> None:
    """Behaviour: a ``/health`` with ``ready=false`` stops the stage with a
    message that says the pod is still loading, before any upload or complete.

    Bug caught: the stage reading only ``supported_modes`` and never
    ``health.ready``. A pre-ready pod reports an EMPTY mode set, so the mode
    gate fires instead and tells the operator to "fix
    capability.supported_modes" — blaming the config for a pod that is merely
    mid-load. Reachable on every ``--attach-pod`` run (that path skips
    ``wait_for_ready``), and the wrong advice invites an edit to a correct
    config.
    """
    eng = _CountingEngine(frozenset(), ready=False)
    stage = _stage(tmp_path, eng, None)
    req = GenerationRequest(prompt="describe", mode="t2t")

    with pytest.raises(ValidationError) as excinfo:
        stage.run(PipelineState(request=req, artifacts={}))

    message = str(excinfo.value)
    assert "still loading" in message
    assert "pod-model" in message
    assert "supported_modes" not in message
    assert eng.health_calls == 1
    assert eng.uploads == []
    assert eng.jobs == []


def test_mode_mismatch_stops_before_any_upload(tmp_path: Path) -> None:
    """Bug caught: a stage that uploads first and gates after — the §4.2 half
    of the gate would then cost the upload AND leak bytes to a pod that cannot use them."""
    eng = _CountingEngine(frozenset({"t2t"}))
    stage = _stage(tmp_path, eng, None)
    req = GenerationRequest(
        prompt="describe",
        mode="it2t",
        assets=[_image_asset(tmp_path, "a.png", "image_1")],
    )
    with pytest.raises(ValidationError, match=r"\['t2t'\].*'it2t'.*\['it2t', 't2t'\]"):
        stage.run(PipelineState(request=req, artifacts={}))
    assert eng.health_calls == 1
    assert eng.uploads == []
    assert eng.jobs == []


def test_happy_path_uploads_in_role_order_and_publishes_two_files(
    tmp_path: Path,
) -> None:
    """Bug caught: images uploaded out of order (the prompt refers to 'the
    first image'), params/system dropped from the job, or a single publish."""
    eng = _CountingEngine(frozenset({"t2t", "it2t"}))
    sink = _RecordingSink(tmp_path / "out")
    (tmp_path / "out").mkdir()
    stage = _stage(tmp_path, eng, sink)
    assets = [
        _image_asset(tmp_path, "z.png", "image_1"),
        _image_asset(tmp_path, "a.jpg", "image_2"),
    ]
    state = stage.run(
        PipelineState(
            request=GenerationRequest(prompt="compare", mode="it2t", assets=assets),
            artifacts={},
        )
    )

    assert [p.name for p in eng.uploads] == ["z.png", "a.jpg"]
    job = eng.jobs[0]
    assert job == TextJob(
        prompt="compare",
        system="Be terse.",
        images=("/tmp/kf-uploads/z.png", "/tmp/kf-uploads/a.jpg"),
        params={"max_new_tokens": 8},
    )

    art = state.artifacts["text"]
    assert art.meta["text"] == "the answer"
    assert Path(art.uri.removeprefix("file://")).read_text() == "the answer"
    sidecar = json.loads(Path(art.meta["json_uri"].removeprefix("file://")).read_text())
    assert sidecar["mode"] == "it2t" and sidecar["text"] == "the answer"
    assert sidecar["model"] == "hf:Qwen/Qwen3-0.6B" and sidecar["run_id"] == "text-test"
    assert [i["pod_path"] for i in sidecar["images"]] == [
        "/tmp/kf-uploads/z.png",
        "/tmp/kf-uploads/a.jpg",
    ]
    assert sidecar["usage"] == {"prompt_tokens": 4, "completion_tokens": 2}

    assert [c["extension"] for c in sink.calls] == [".txt", ".json"]
    assert {c["kind"] for c in sink.calls} == {"text"}
    assert {c["provider"] for c in sink.calls} == {"transformers"}
    assert {c["model"] for c in sink.calls} == {"Qwen3-0.6B"}
    assert sink.calls[0]["data"] == b"the answer"
    assert art.meta["published"] == str(tmp_path / "out" / "out.txt")


def test_no_sink_is_store_only(tmp_path: Path) -> None:
    """Bug caught: a publish attempted on None (--no-output-dir path)."""
    eng = _CountingEngine(frozenset({"t2t"}))
    state = _stage(tmp_path, eng, None).run(
        PipelineState(request=GenerationRequest(prompt="hi", mode="t2t"), artifacts={})
    )
    assert state.artifacts["text"].meta["published"] is None
    assert state.artifacts["text"].meta["text"] == "the answer"
    assert eng.jobs[0].images == ()


def test_url_images_are_refused_before_health(tmp_path: Path) -> None:
    """Bug caught: a URL handed to upload_image, which read_bytes()es a path."""
    eng = _CountingEngine(frozenset({"t2t", "it2t"}))
    asset = ConditioningAsset(
        kind="image",
        role="image_1",
        ref=Artifact(uri="https://x/y.png", sha256="", size=0),
    )
    with pytest.raises(ValidationError, match="local file"):
        _stage(tmp_path, eng, None).run(
            PipelineState(
                request=GenerationRequest(prompt="p", mode="it2t", assets=[asset]),
                artifacts={},
            )
        )
    assert eng.health_calls == 0
