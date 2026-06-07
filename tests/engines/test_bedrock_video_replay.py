"""Offline replay test for BedrockVideoEngine against a captured live-smoke fixture.

Layer 3 Task 8 (after Nova Reel → BedrockVideoEngine pivot).

The test skips cleanly until ``tests/engines/fixtures/luma_ray/last_smoke.json``
is written by the live smoke (``tests/live/test_luma_ray_live.py`` with
``KINOFORGE_SAVE_FIXTURES=1``).  Once the fixture lands, the test runs
deterministically in CI — no AWS environment needed.

Fixture schema::

    {
        "artifact_uri": "s3://bedrock-video-generation-us-west-2-<id>/kinoforge-output/<inv-id>/output.mp4",
        "invocation_arn": "arn:aws:bedrock:us-west-2:...",
        "model_id": "luma.ray-v2:0",
        "output_s3_uri": "s3://bedrock-video-generation-us-west-2-<id>/kinoforge-output/",
        "prompt": "...",
        "region_name": "us-west-2"
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "luma_ray" / "last_smoke.json"


@pytest.fixture()
def smoke_fixture() -> dict[str, Any]:
    """Load the live-smoke fixture JSON or skip if not yet captured.

    Returns:
        Parsed fixture dict from ``last_smoke.json``.
    """
    if not FIXTURE_PATH.exists():
        pytest.skip(
            f"fixture not yet captured at {FIXTURE_PATH}; "
            "awaits AWS Support approval to fire live smoke"
        )
    return json.loads(FIXTURE_PATH.read_text())


def test_bedrock_video_offline_replay_from_fixture(
    smoke_fixture: dict[str, Any],
) -> None:
    """Replay a captured live smoke against BedrockVideoBackend with a fake client.

    Constructs a ``_FixtureBedrockClient`` whose ``get_async_invoke`` returns
    ``{"status": "Completed", "invocationArn": ...}`` derived from the fixture's
    ``artifact_uri``.  The backend must reconstruct the same S3 URI.

    Args:
        smoke_fixture: Parsed JSON from ``tests/engines/fixtures/luma_ray/last_smoke.json``.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.bedrock_video import BedrockVideoBackend

    artifact_uri: str = smoke_fixture["artifact_uri"]
    invocation_arn: str = smoke_fixture["invocation_arn"]

    class _FixtureBedrockClient:
        """Minimal bedrock-runtime double that replays the captured fixture.

        ``start_async_invoke`` returns the ARN from the fixture; ``get_async_invoke``
        immediately returns ``Completed``.
        """

        def start_async_invoke(
            self,
            *,
            modelId: str,
            modelInput: dict[str, Any],
            outputDataConfig: dict[str, Any],
        ) -> dict[str, str]:
            return {"invocationArn": invocation_arn}

        def get_async_invoke(self, *, invocationArn: str) -> dict[str, Any]:
            return {"status": "Completed", "invocationArn": invocationArn}

    # Build a minimal config that matches the fixture's runtime state.
    cfg: dict[str, Any] = {
        "engine": {
            "bedrock_video": {
                "region_name": smoke_fixture.get("region_name", "us-west-2"),
                "model_id": smoke_fixture.get("model_id", "luma.ray-v2:0"),
                "output_s3_uri": smoke_fixture["output_s3_uri"],
                "model_input_template": {"prompt": "${PROMPT}"},
            }
        }
    }

    fake_client = _FixtureBedrockClient()
    backend = BedrockVideoBackend(
        client=fake_client,
        cfg=cfg,
        sleep=lambda s: None,
    )

    job = GenerationJob(
        segments=[Segment(prompt=smoke_fixture.get("prompt", "x"))],
        spec={},
        params={},
    )
    job_id = backend.submit(job)
    artifact = backend.result(job_id)

    assert artifact.uri == artifact_uri, (
        f"Replayed artifact URI {artifact.uri!r} != fixture URI {artifact_uri!r}"
    )
