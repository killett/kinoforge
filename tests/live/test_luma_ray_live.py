"""Layer 3 live smoke — Luma Ray v2 on real AWS Bedrock (us-west-2).

Gated by KINOFORGE_LIVE_TESTS=1 + an AWS credential chain that resolves
(env vars, ~/.aws/credentials, or instance profile). Reads the prompt
from /workspace/prompt-field-realistic.txt per project directive.

Cost: ~$0.50 per cold run; budget cap $1.50 across iterations (Layer 3
plan). Skipped silently in CI.

Note: AWS Model access page is RETIRED for serverless foundation models —
they auto-activate on first invoke. No console step needed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

if os.getenv("KINOFORGE_LIVE_TESTS") != "1":
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + AWS credentials",
        allow_module_level=True,
    )

_log = logging.getLogger(__name__)

_MP4_FTYP_PREFIXES: tuple[bytes, ...] = (
    b"ftypisom",
    b"ftypiso5",
    b"ftypiso6",
    b"ftypmp42",
)


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def test_luma_ray_live_e2e_smoke(tmp_path: Path) -> None:
    """End-to-end: load cfg → submit → MP4 in S3 → bytes start with ftyp."""

    # _adapters import first so bedrock_video registers itself
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.core.registry import get_engine

    # Load config
    cfg = load_config("examples/configs/luma-ray.yaml")
    # Verify shape
    assert cfg.engine.kind == "bedrock_video"

    # Load prompt from the canonical file (project directive).
    prompt = Path("/workspace/prompt-field-realistic.txt").read_text().strip()
    assert len(prompt) > 100, "prompt-field-realistic.txt unexpectedly short"

    # Build engine + backend (via raw cfg dict — BedrockVideoEngine adapter consumes dict).
    import yaml

    cfg_dict = yaml.safe_load(Path("examples/configs/luma-ray.yaml").read_text())
    engine_factory = get_engine("bedrock_video")
    engine = engine_factory()
    engine.provision(None, cfg_dict)
    backend = engine.backend(None, cfg_dict)

    # Submit the job.
    job = GenerationJob(
        segments=[Segment(prompt=prompt)],
        spec={},
        params={},
    )
    submitted = backend.submit(job)
    _log.info("luma ray submitted: job_id=%s", submitted)

    # Wait for result (Luma Ray typically completes in 1-3 minutes for 5s clips).
    artifact = backend.result(submitted)
    _log.info("luma ray artifact: %s", artifact.uri)
    assert artifact.uri.startswith("s3://bedrock-video-generation-us-west-2-nw51wr/")
    assert artifact.filename == "output.mp4"

    # Download + verify MP4 ftyp signature.
    import boto3  # noqa: PLC0415 — lazy

    bucket, key = artifact.uri.removeprefix("s3://").split("/", 1)
    s3 = boto3.client("s3", region_name="us-west-2")
    resp = s3.get_object(Bucket=bucket, Key=key)
    body_head = resp["Body"].read(64)
    assert any(prefix in body_head for prefix in _MP4_FTYP_PREFIXES), (
        f"output does not start with MP4 ftyp prefix; head={body_head!r}"
    )
    _log.info("luma ray MP4 verified: %d bytes head", len(body_head))

    # Capture fixtures + metadata if requested
    if os.getenv("KINOFORGE_SAVE_FIXTURES") == "1":
        fixtures_dir = Path("tests/engines/fixtures/luma_ray")
        fixtures_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "git_sha": _git_sha(),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "artifact_uri": artifact.uri,
            "filename": artifact.filename,
            "model_id": "luma.ray-v2:0",
            "region": "us-west-2",
        }
        (fixtures_dir / "last_smoke.json").write_text(
            __import__("json").dumps(meta, indent=2) + "\n"
        )
        _log.info("fixtures captured to %s", fixtures_dir)
