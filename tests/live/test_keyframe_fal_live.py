"""Layer R T15-T16: live smoke against fal-ai/flux-schnell + wan-i2v/flf2v.

Default-skip; runs only with KINOFORGE_LIVE_TESTS=1 + FAL_KEY in env.
Spend ceiling per test: ~$0.05 (1 flux-schnell + 1 wan).
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
# MP4 ftyp box magic offset 4
MP4_FTYP = b"ftyp"


def _require_fal_key() -> str:
    key = os.environ.get("FAL_KEY")
    if not key:
        pytest.fail(
            "KINOFORGE_LIVE_TESTS=1 is set but FAL_KEY is missing — "
            "a misconfigured live run must fail loud, not no-op."
        )
    return key


def test_keyframe_fal_i2v_live(tmp_path: Path) -> None:
    """End-to-end: cfg.keyframe + mode=i2v → fal generates init_image →
    wan-i2v consumes it → MP4 output exists.

    Real spend: ~$0.003 (keyframe) + ~$0.02 (clip) ≈ $0.025.
    """
    _require_fal_key()

    import kinoforge.engines.fal  # noqa: F401

    # self-register adapters needed by the example
    import kinoforge.image_engines.fal  # noqa: F401
    import kinoforge.sources.http  # noqa: F401
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import GenerationRequest
    from kinoforge.core.orchestrator import generate
    from kinoforge.stores.local import LocalArtifactStore

    cfg = load_config("examples/configs/keyframe-fal-i2v.yaml")
    store = LocalArtifactStore(tmp_path)
    prompt = cfg.prompt or "a cat walking through a sunlit meadow"
    mode = cfg.mode or "i2v"
    request = GenerationRequest(prompt=prompt, mode=mode)
    artifact, _instance = generate(cfg, request, store=store, run_id="live-r-i2v")

    # Clip artifact materialised
    clip_path = Path(artifact.uri.replace("file://", ""))
    assert clip_path.exists(), f"clip not persisted: {artifact.uri}"
    clip_bytes = clip_path.read_bytes()
    assert MP4_FTYP in clip_bytes[:32], (
        f"clip is not an MP4 (no ftyp box in header): {clip_bytes[:32]!r}"
    )

    # Keyframe artifact also persisted under the run_id
    kf_files = list(tmp_path.glob("**/keyframe-init_image.png"))
    assert len(kf_files) == 1, f"expected 1 keyframe-init_image.png, got {kf_files}"
    kf_bytes = kf_files[0].read_bytes()
    assert kf_bytes.startswith(PNG_MAGIC), (
        f"keyframe is not a PNG (no PNG magic): {kf_bytes[:8]!r}"
    )


def test_keyframe_fal_flf2v_live(tmp_path: Path) -> None:
    """flf2v variant — fal generates both bookends with differentiated prompts,
    wan-flf2v morphs between them.

    Real spend: ~$0.006 (2 keyframes) + ~$0.025 (clip) ≈ $0.031.
    """
    _require_fal_key()

    import kinoforge.engines.fal  # noqa: F401

    # self-register adapters needed by the example
    import kinoforge.image_engines.fal  # noqa: F401
    import kinoforge.sources.http  # noqa: F401
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import GenerationRequest
    from kinoforge.core.orchestrator import generate
    from kinoforge.stores.local import LocalArtifactStore

    cfg = load_config("examples/configs/keyframe-fal-flf2v.yaml")
    store = LocalArtifactStore(tmp_path)
    prompt = cfg.prompt or "a cat morphing into a tiger"
    mode = cfg.mode or "flf2v"
    request = GenerationRequest(prompt=prompt, mode=mode)
    artifact, _instance = generate(cfg, request, store=store, run_id="live-r-flf")

    clip_path = Path(artifact.uri.replace("file://", ""))
    assert clip_path.exists()
    clip_bytes = clip_path.read_bytes()
    assert MP4_FTYP in clip_bytes[:32]

    first = list(tmp_path.glob("**/keyframe-first_frame.png"))
    last = list(tmp_path.glob("**/keyframe-last_frame.png"))
    assert len(first) == 1
    assert len(last) == 1
    first_bytes = first[0].read_bytes()
    last_bytes = last[0].read_bytes()
    assert first_bytes.startswith(PNG_MAGIC)
    assert last_bytes.startswith(PNG_MAGIC)
    assert first_bytes != last_bytes, (
        "first_frame and last_frame must differ (distinct prompts → distinct images)"
    )
