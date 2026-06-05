"""Layer R T15-T16: live smoke against fal-ai/flux/schnell.

Default-skip; runs only with KINOFORGE_LIVE_TESTS=1 + FAL_KEY in env.

Scope: exercises FalImageEngine + KeyframeStage end-to-end (the new Layer R
wire surface). The downstream wan i2v/flf2v step is NOT exercised here —
fal's wan endpoints require image_url as a public HTTPS URL or fal-CDN
upload, and Layer R does not ship the keyframe→fal-storage upload glue.
That glue is a separate follow-up (Layer S candidate). Wan video engine
live verification already shipped in Phase 19.

Spend ceiling per test: ~$0.003 (1 flux/schnell call). Both tests together
~$0.006.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

LIVE = os.environ.get("KINOFORGE_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE, reason="set KINOFORGE_LIVE_TESTS=1 to enable live smoke"
)

# PNG magic bytes: 0x89 50 4E 47
PNG_MAGIC = b"\x89PNG"


def _require_fal_key() -> str:
    key = os.environ.get("FAL_KEY")
    if not key:
        pytest.fail(
            "KINOFORGE_LIVE_TESTS=1 is set but FAL_KEY is missing — "
            "a misconfigured live run must fail loud, not no-op."
        )
    return key


def test_fal_image_engine_t2i_live(tmp_path: Path) -> None:
    """End-to-end: FalImageEngine submits a flux/schnell job, polls, fetches PNG.

    This is the smallest live test that exercises the full Layer R image-engine
    wire surface: submit → poll status → fetch response → resolve image URL →
    download bytes. Asserts PNG magic at the start of the downloaded bytes.

    Real spend: ~$0.003 (one flux/schnell inference).
    """
    _require_fal_key()

    import kinoforge.image_engines.fal  # noqa: F401  self-register
    from kinoforge.core import registry
    from kinoforge.core.interfaces import ImageJob
    from kinoforge.pipeline.artifact_bytes import artifact_bytes

    engine = registry.get_image_engine("fal")()
    engine.provision(None, {})
    backend = engine.backend(None, {"spec": {"model": "fal-ai/flux/schnell"}})

    job = ImageJob(
        spec={"model": "fal-ai/flux/schnell"},
        prompt="a small grey cat sitting in green grass, photorealistic",
    )
    engine.validate_spec(job)
    job_id = backend.submit(job)
    assert job_id, "submit must return a non-empty request_id"

    artifact = backend.result(job_id)
    assert artifact.url, "result must populate Artifact.url"
    assert artifact.filename, "result must populate Artifact.filename"

    png_bytes = artifact_bytes(artifact)
    assert png_bytes.startswith(PNG_MAGIC), (
        f"downloaded bytes are not a PNG (no PNG magic): {png_bytes[:8]!r}"
    )
    assert len(png_bytes) > 1000, (
        f"PNG suspiciously small ({len(png_bytes)} bytes) — likely an error page"
    )


def test_keyframe_stage_with_fal_image_engine_live(tmp_path: Path) -> None:
    """KeyframeStage + FalImageEngine end-to-end with persistence.

    Constructs a KeyframeStage backed by FalImageEngine, runs it against
    a mode=i2v request with empty assets, asserts that:
    - state.artifacts["keyframe-init_image"] is populated
    - the persisted file under run_id starts with PNG magic bytes
    - the request now carries a ConditioningAsset(role="init_image")

    Real spend: ~$0.003 (one flux/schnell inference).
    """
    _require_fal_key()

    import kinoforge.image_engines.fal  # noqa: F401  self-register
    from kinoforge.core import registry
    from kinoforge.core.config import KeyframeConfig
    from kinoforge.core.interfaces import (
        CapabilityKey,
        GenerationRequest,
        PipelineState,
    )
    from kinoforge.pipeline.keyframe import KeyframeStage
    from kinoforge.stores.local import LocalArtifactStore

    engine = registry.get_image_engine("fal")()
    engine.provision(None, {})
    backend = engine.backend(None, {"spec": {"model": "fal-ai/flux/schnell"}})
    profile = engine.profile_for(
        CapabilityKey(base_model="fal-ai/flux/schnell", engine="fal")
    )

    keyframe_cfg = KeyframeConfig(
        engine="fal",
        prompt="a small grey cat sitting in green grass, photorealistic",
        spec={"model": "fal-ai/flux/schnell"},
    )

    store = LocalArtifactStore(tmp_path)
    stage = KeyframeStage(
        keyframe_cfg=keyframe_cfg,
        image_engine=engine,
        image_backend=backend,
        image_profile=profile,
        store=store,
        run_id="live-keyframe",
    )

    request = GenerationRequest(prompt="ignored-clip-prompt", mode="i2v")
    state = PipelineState(request=request)
    out = stage.run(state)

    # Artifact recorded in PipelineState.artifacts
    assert "keyframe-init_image" in out.artifacts, (
        f"expected keyframe-init_image in artifacts; got {sorted(out.artifacts)}"
    )

    # Asset appended to request
    assert any(a.role == "init_image" for a in out.request.assets), (
        "expected ConditioningAsset(role='init_image') appended to request.assets"
    )

    # File persisted to disk under run_id with PNG magic
    kf_files = list(tmp_path.glob("**/keyframe-init_image.png"))
    assert len(kf_files) == 1, f"expected 1 keyframe-init_image.png, got {kf_files}"
    kf_bytes = kf_files[0].read_bytes()
    assert kf_bytes.startswith(PNG_MAGIC), f"keyframe is not a PNG: {kf_bytes[:8]!r}"
    assert len(kf_bytes) > 1000
