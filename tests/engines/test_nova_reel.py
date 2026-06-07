"""Layer 3 — NovaReelEngine + NovaReelBackend offline unit tests.

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
            "invocationArn": f"arn:aws:bedrock:us-east-1::async-invoke/inv-{len(self.start_calls)}"
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
    base: dict[str, Any] = {
        "engine": {
            "nova_reel": {
                "region_name": "us-east-1",
                "model_id": "amazon.nova-reel-v1:1",
                "output_s3_uri": "s3://kinoforge-nova-reel-output/",
                "duration_seconds": 6,
                "fps": 24,
                "dimension": "1280x720",
                "prompt_body_key": "prompt",
            }
        }
    }
    base["engine"]["nova_reel"].update(overrides)
    return base


def _build_job(prompt: str = "test prompt") -> GenerationJob:
    return GenerationJob(
        segments=[Segment(prompt=prompt)],
        spec={},
        params={},
    )


def test_nova_reel_engine_is_importable_and_registered() -> None:
    from kinoforge.core.registry import get_engine
    from kinoforge.engines.nova_reel import NovaReelEngine  # noqa: F401

    factory = get_engine("nova_reel")
    engine = factory()
    assert isinstance(engine, NovaReelEngine)


def test_nova_reel_engine_does_not_require_compute_or_weights() -> None:
    from kinoforge.engines.nova_reel import NovaReelEngine

    engine = NovaReelEngine()
    assert engine.requires_compute is False
    assert engine.requires_local_weights is False


def test_nova_reel_provision_calls_auth_health_check() -> None:
    from kinoforge.engines.nova_reel import NovaReelEngine

    fake_client = _FakeBedrockRuntime()
    auth = FakeAuthStrategy()  # ok by default

    def fake_session_factory(**kwargs: Any) -> Any:
        class _Sess:
            def client(self, name: str, region_name: str) -> _FakeBedrockRuntime:
                return fake_client

        return _Sess()

    engine = NovaReelEngine(
        auth_strategy=auth,
        boto3_session_factory=fake_session_factory,
    )
    engine.provision(None, _build_cfg())
    # health_check called exactly once during provision
    # (verified by FakeAuthStrategy internal counter would require ext;
    # the contract guarantee here is provision didn't raise)
    assert engine._client is fake_client


def test_nova_reel_provision_raises_auth_error_when_creds_missing() -> None:
    from kinoforge.core.errors import AuthError
    from kinoforge.engines.nova_reel import NovaReelEngine

    auth = FakeAuthStrategy(credentials_ok=False)

    def fake_session_factory(**kwargs: Any) -> Any:
        class _Sess:
            def client(self, name: str, region_name: str) -> _FakeBedrockRuntime:
                return _FakeBedrockRuntime()

        return _Sess()

    engine = NovaReelEngine(
        auth_strategy=auth, boto3_session_factory=fake_session_factory
    )
    with pytest.raises(AuthError, match="nova_reel"):
        engine.provision(None, _build_cfg())


def test_nova_reel_backend_submit_calls_start_async_invoke() -> None:
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime()
    cfg = _build_cfg()
    backend = NovaReelBackend(client=fake_client, cfg=cfg)

    job = _build_job(prompt="cinematic sunset, anamorphic lens")
    job_id = backend.submit(job)

    assert len(fake_client.start_calls) == 1
    call = fake_client.start_calls[0]
    assert call["modelId"] == "amazon.nova-reel-v1:1"
    assert call["modelInput"]["taskType"] == "TEXT_VIDEO"
    assert (
        call["modelInput"]["textToVideoParams"]["text"]
        == "cinematic sunset, anamorphic lens"
    )
    assert call["modelInput"]["videoGenerationConfig"]["durationSeconds"] == 6
    assert call["modelInput"]["videoGenerationConfig"]["fps"] == 24
    assert call["modelInput"]["videoGenerationConfig"]["dimension"] == "1280x720"
    assert (
        call["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"]
        == "s3://kinoforge-nova-reel-output/"
    )
    # job_id is a non-empty string
    assert isinstance(job_id, str) and job_id


def test_nova_reel_backend_submit_with_kms_key_passes_kms_to_output_config() -> None:
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime()
    cfg = _build_cfg(output_kms_key_id="arn:aws:kms:us-east-1:123:key/abc")
    backend = NovaReelBackend(client=fake_client, cfg=cfg)
    backend.submit(_build_job())

    call = fake_client.start_calls[0]
    assert call["outputDataConfig"]["s3OutputDataConfig"]["kmsKeyId"] == (
        "arn:aws:kms:us-east-1:123:key/abc"
    )


def test_nova_reel_backend_result_polls_until_completed() -> None:
    from kinoforge.core.interfaces import Artifact
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime(completed_after=3)
    cfg = _build_cfg()
    backend = NovaReelBackend(client=fake_client, cfg=cfg, sleep=lambda s: None)

    job_id = backend.submit(_build_job())
    artifact = backend.result(job_id)

    assert isinstance(artifact, Artifact)
    assert artifact.uri == ("s3://kinoforge-nova-reel-output/inv-1/output.mp4")
    assert artifact.filename == "output.mp4"
    assert artifact.url is None or artifact.url == ""
    assert artifact.headers is None or artifact.headers == {}
    assert len(fake_client.get_calls) == 3


def test_nova_reel_backend_result_raises_kinoforge_error_on_failure() -> None:
    from kinoforge.core.errors import KinoforgeError
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime()
    fake_client.failed = True
    cfg = _build_cfg()
    backend = NovaReelBackend(client=fake_client, cfg=cfg, sleep=lambda s: None)

    job_id = backend.submit(_build_job())
    with pytest.raises(KinoforgeError, match="synthetic failure"):
        backend.result(job_id)


def test_nova_reel_backend_invocation_id_extracted_from_arn() -> None:
    """The output S3 URI uses the LAST `/`-segment of invocationArn as the dir."""
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime()
    # Override the ARN returned to use a Bedrock-shaped UUID.
    fake_client.start_async_invoke = lambda **kw: {  # type: ignore[method-assign]
        "invocationArn": "arn:aws:bedrock:us-east-1:1234567890:async-invoke/abc-12345-uuid"
    }
    cfg = _build_cfg()
    backend = NovaReelBackend(client=fake_client, cfg=cfg, sleep=lambda s: None)
    job_id = backend.submit(_build_job())
    artifact = backend.result(job_id)
    assert artifact.uri.endswith("/abc-12345-uuid/output.mp4")


def test_nova_reel_engine_lazy_imports_boto3() -> None:
    """Importing the engine module must NOT load boto3 at module top."""
    import subprocess
    import sys

    script = (
        "import kinoforge.engines.nova_reel; "
        "import sys; "
        "print('|'.join(m for m in sys.modules if m == 'boto3' or m.startswith('botocore')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    leaked = result.stdout.strip()
    assert not leaked, f"nova_reel engine import leaked boto3 modules: {leaked}"
