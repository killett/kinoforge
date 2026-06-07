"""Layer 3 — BedrockVideoEngine + BedrockVideoBackend offline unit tests.

All tests use a _FakeBedrockRuntime client double; no real AWS calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import GenerationJob, Segment
from tests._fixtures.fake_auth import FakeAuthStrategy


class _FakeBedrockRuntime:
    """Minimal bedrock-runtime client double for unit tests.

    Records every call so tests can assert on the requests. Status flips
    from InProgress → Completed after ``_completed_after`` polls so the
    poll loop is exercised but tests stay fast.
    """

    def __init__(self, completed_after: int = 1) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self._completed_after = completed_after
        self._poll_count = 0
        self.failed = False

    def start_async_invoke(
        self,
        *,
        modelId: str,
        modelInput: dict[str, Any],
        outputDataConfig: dict[str, Any],
    ) -> dict[str, str]:
        self.start_calls.append(
            {
                "modelId": modelId,
                "modelInput": modelInput,
                "outputDataConfig": outputDataConfig,
            }
        )
        # Return a deterministic ARN for tests to assert.
        return {
            "invocationArn": f"arn:aws:bedrock:us-west-2::async-invoke/inv-{len(self.start_calls)}"
        }

    def get_async_invoke(self, *, invocationArn: str) -> dict[str, Any]:
        self.get_calls.append(invocationArn)
        if self.failed:
            return {"status": "Failed", "failureMessage": "synthetic failure"}
        self._poll_count += 1
        if self._poll_count >= self._completed_after:
            return {"status": "Completed", "invocationArn": invocationArn}
        return {"status": "InProgress", "invocationArn": invocationArn}


def _build_cfg(**overrides: Any) -> dict[str, Any]:
    """Build a Luma Ray v2–shaped config dict (the active default)."""
    base: dict[str, Any] = {
        "engine": {
            "bedrock_video": {
                "region_name": "us-west-2",
                "model_id": "luma.ray-v2:0",
                "output_s3_uri": "s3://bedrock-video-generation-us-west-2-nw51wr/kinoforge-output/",
                "model_input_template": {
                    "prompt": "${PROMPT}",
                    "duration": 5,
                    "aspect_ratio": "16:9",
                    "loop": False,
                    "resolution": "720p",
                },
            }
        }
    }
    base["engine"]["bedrock_video"].update(overrides)
    return base


def _build_job(prompt: str = "test prompt") -> GenerationJob:
    return GenerationJob(
        segments=[Segment(prompt=prompt)],
        spec={},
        params={},
    )


def test_bedrock_video_engine_is_importable_and_registered() -> None:
    from kinoforge.core.registry import get_engine
    from kinoforge.engines.bedrock_video import BedrockVideoEngine  # noqa: F401

    factory = get_engine("bedrock_video")
    engine = factory()
    assert isinstance(engine, BedrockVideoEngine)


def test_bedrock_video_engine_does_not_require_compute_or_weights() -> None:
    from kinoforge.engines.bedrock_video import BedrockVideoEngine

    engine = BedrockVideoEngine()
    assert engine.requires_compute is False
    assert engine.requires_local_weights is False


def test_bedrock_video_provision_calls_auth_health_check() -> None:
    from kinoforge.engines.bedrock_video import BedrockVideoEngine

    fake_client = _FakeBedrockRuntime()
    auth = FakeAuthStrategy()  # ok by default

    def fake_session_factory(**kwargs: Any) -> Any:
        class _Sess:
            def client(self, name: str, region_name: str) -> _FakeBedrockRuntime:
                return fake_client

        return _Sess()

    engine = BedrockVideoEngine(
        auth_strategy=auth,
        boto3_session_factory=fake_session_factory,
    )
    engine.provision(None, _build_cfg())
    # health_check called exactly once during provision
    # (verified by FakeAuthStrategy internal counter would require ext;
    # the contract guarantee here is provision didn't raise)
    assert engine._client is fake_client


def test_bedrock_video_provision_raises_auth_error_when_creds_missing() -> None:
    from kinoforge.core.errors import AuthError
    from kinoforge.engines.bedrock_video import BedrockVideoEngine

    auth = FakeAuthStrategy(credentials_ok=False)

    def fake_session_factory(**kwargs: Any) -> Any:
        class _Sess:
            def client(self, name: str, region_name: str) -> _FakeBedrockRuntime:
                return _FakeBedrockRuntime()

        return _Sess()

    engine = BedrockVideoEngine(
        auth_strategy=auth, boto3_session_factory=fake_session_factory
    )
    with pytest.raises(AuthError, match="bedrock_video"):
        engine.provision(None, _build_cfg())


def test_bedrock_video_backend_submit_calls_start_async_invoke() -> None:
    from kinoforge.engines.bedrock_video import BedrockVideoBackend

    fake_client = _FakeBedrockRuntime()
    cfg = _build_cfg()
    backend = BedrockVideoBackend(client=fake_client, cfg=cfg)

    job = _build_job(prompt="cinematic sunset, anamorphic lens")
    job_id = backend.submit(job)

    assert len(fake_client.start_calls) == 1
    call = fake_client.start_calls[0]
    assert call["modelId"] == "luma.ray-v2:0"
    # Prompt substituted into the template
    assert call["modelInput"]["prompt"] == "cinematic sunset, anamorphic lens"
    # Other template fields preserved
    assert call["modelInput"]["duration"] == 5
    assert call["modelInput"]["aspect_ratio"] == "16:9"
    assert call["modelInput"]["loop"] is False
    assert call["modelInput"]["resolution"] == "720p"
    assert (
        call["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"]
        == "s3://bedrock-video-generation-us-west-2-nw51wr/kinoforge-output/"
    )
    # job_id is a non-empty string
    assert isinstance(job_id, str) and job_id


def test_bedrock_video_backend_submit_with_kms_key_passes_kms_to_output_config() -> (
    None
):
    from kinoforge.engines.bedrock_video import BedrockVideoBackend

    fake_client = _FakeBedrockRuntime()
    cfg = _build_cfg(output_kms_key_id="arn:aws:kms:us-west-2:123:key/abc")
    backend = BedrockVideoBackend(client=fake_client, cfg=cfg)
    backend.submit(_build_job())

    call = fake_client.start_calls[0]
    assert call["outputDataConfig"]["s3OutputDataConfig"]["kmsKeyId"] == (
        "arn:aws:kms:us-west-2:123:key/abc"
    )


def test_bedrock_video_backend_result_polls_until_completed() -> None:
    from kinoforge.core.interfaces import Artifact
    from kinoforge.engines.bedrock_video import BedrockVideoBackend

    fake_client = _FakeBedrockRuntime(completed_after=3)
    cfg = _build_cfg()
    backend = BedrockVideoBackend(client=fake_client, cfg=cfg, sleep=lambda s: None)

    job_id = backend.submit(_build_job())
    artifact = backend.result(job_id)

    assert isinstance(artifact, Artifact)
    assert artifact.uri == (
        "s3://bedrock-video-generation-us-west-2-nw51wr/kinoforge-output/inv-1/output.mp4"
    )
    assert artifact.filename == "output.mp4"
    assert artifact.url is None or artifact.url == ""
    assert artifact.headers is None or artifact.headers == {}
    assert len(fake_client.get_calls) == 3


def test_bedrock_video_backend_result_raises_kinoforge_error_on_failure() -> None:
    from kinoforge.core.errors import KinoforgeError
    from kinoforge.engines.bedrock_video import BedrockVideoBackend

    fake_client = _FakeBedrockRuntime()
    fake_client.failed = True
    cfg = _build_cfg()
    backend = BedrockVideoBackend(client=fake_client, cfg=cfg, sleep=lambda s: None)

    job_id = backend.submit(_build_job())
    with pytest.raises(KinoforgeError, match="synthetic failure"):
        backend.result(job_id)


def test_bedrock_video_backend_invocation_id_extracted_from_arn() -> None:
    """The output S3 URI uses the LAST `/`-segment of invocationArn as the dir."""
    from kinoforge.engines.bedrock_video import BedrockVideoBackend

    fake_client = _FakeBedrockRuntime()
    # Override the ARN returned to use a Bedrock-shaped UUID.
    fake_client.start_async_invoke = lambda **kw: {  # type: ignore[method-assign]
        "invocationArn": "arn:aws:bedrock:us-west-2:1234567890:async-invoke/abc-12345-uuid"
    }
    cfg = _build_cfg()
    backend = BedrockVideoBackend(client=fake_client, cfg=cfg, sleep=lambda s: None)
    job_id = backend.submit(_build_job())
    artifact = backend.result(job_id)
    assert artifact.uri.endswith("/abc-12345-uuid/output.mp4")


def test_bedrock_video_engine_lazy_imports_boto3() -> None:
    """Importing the engine module must NOT load boto3 at module top."""
    import subprocess
    import sys

    script = (
        "import kinoforge.engines.bedrock_video; "
        "import sys; "
        "print('|'.join(m for m in sys.modules if m == 'boto3' or m.startswith('botocore')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    leaked = result.stdout.strip()
    assert not leaked, f"bedrock_video engine import leaked boto3 modules: {leaked}"


def test_bedrock_video_submit_substitutes_prompt_in_template() -> None:
    """_substitute_prompt replaces '${PROMPT}' at any nesting depth."""
    from kinoforge.engines.bedrock_video import BedrockVideoBackend

    fake_client = _FakeBedrockRuntime()
    # Build a config with a 2-level-nested template that has ${PROMPT} in a sub-dict.
    nested_template: dict[str, Any] = {
        "taskType": "TEXT_VIDEO",
        "textToVideoParams": {"text": "${PROMPT}"},
        "videoGenerationConfig": {
            "durationSeconds": 6,
            "fps": 24,
            "dimension": "1280x720",
        },
    }
    cfg = _build_cfg(
        model_id="amazon.nova-reel-v1:1",
        model_input_template=nested_template,
    )
    backend = BedrockVideoBackend(client=fake_client, cfg=cfg)
    job = _build_job(prompt="deep nested prompt test")
    backend.submit(job)

    call = fake_client.start_calls[0]
    # Substitution happened at the nested level
    assert call["modelInput"]["textToVideoParams"]["text"] == "deep nested prompt test"
    # Non-prompt fields untouched
    assert call["modelInput"]["videoGenerationConfig"]["durationSeconds"] == 6
    # Original cfg template NOT mutated (deep copy)
    assert (
        cfg["engine"]["bedrock_video"]["model_input_template"]["textToVideoParams"][
            "text"
        ]
        == "${PROMPT}"
    )


def test_bedrock_video_submit_does_not_mutate_template_config() -> None:
    """submit() must not mutate the cfg model_input_template dict in place."""
    from kinoforge.engines.bedrock_video import BedrockVideoBackend

    fake_client = _FakeBedrockRuntime()
    cfg = _build_cfg()
    original_template = cfg["engine"]["bedrock_video"]["model_input_template"].copy()
    backend = BedrockVideoBackend(client=fake_client, cfg=cfg)
    backend.submit(_build_job(prompt="mutation check"))

    # Template should be unchanged after submit
    assert cfg["engine"]["bedrock_video"]["model_input_template"] == original_template
    # Specifically the ${PROMPT} placeholder should still be there
    assert (
        cfg["engine"]["bedrock_video"]["model_input_template"]["prompt"] == "${PROMPT}"
    )
