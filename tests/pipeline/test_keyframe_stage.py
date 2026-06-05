"""Layer R T9: KeyframeStage role-loop tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kinoforge.core.config import KeyframeConfig, KeyframeRoleOverride
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationRequest,
    PipelineState,
)
from kinoforge.image_engines.fake import FakeImageEngine
from kinoforge.pipeline.keyframe import KeyframeStage
from kinoforge.stores.local import LocalArtifactStore


def _make_stage(cfg: KeyframeConfig, tmp_path: Path) -> KeyframeStage:
    eng = FakeImageEngine()
    backend = eng.backend(None, cfg.model_dump())
    profile = eng.profile_for(cfg.capability_key())
    return KeyframeStage(
        keyframe_cfg=cfg,
        image_engine=eng,
        image_backend=backend,
        image_profile=profile,
        store=LocalArtifactStore(tmp_path),
        run_id="r1",
    )


def test_i2v_empty_assets_fills_init_image(tmp_path: Path) -> None:
    cfg = KeyframeConfig(engine="fake", prompt="cat", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    req = GenerationRequest(prompt="ignored-clip-prompt", mode="i2v")
    state = PipelineState(request=req)
    out = stage.run(state)
    assert len(out.request.assets) == 1
    assert out.request.assets[0].role == "init_image"
    assert out.request.assets[0].kind == "image"
    assert "keyframe-init_image" in out.artifacts


def test_flf2v_empty_assets_fills_both(tmp_path: Path) -> None:
    cfg = KeyframeConfig(
        engine="fake",
        spec={"model": "m"},
        roles={
            "first_frame": KeyframeRoleOverride(prompt="a"),
            "last_frame": KeyframeRoleOverride(prompt="b"),
        },
    )
    stage = _make_stage(cfg, tmp_path)
    req = GenerationRequest(prompt="x", mode="flf2v")
    out = stage.run(PipelineState(request=req))
    roles = {a.role for a in out.request.assets}
    assert roles == {"first_frame", "last_frame"}
    assert "keyframe-first_frame" in out.artifacts
    assert "keyframe-last_frame" in out.artifacts


def test_partial_fill_preserves_user_supplied(tmp_path: Path) -> None:
    """Bug guard: a user-supplied bookend MUST survive; overwriting wastes spend."""
    cfg = KeyframeConfig(
        engine="fake",
        prompt="x",
        spec={"model": "m"},
    )
    stage = _make_stage(cfg, tmp_path)
    user_first = ConditioningAsset(
        kind="image",
        role="first_frame",
        ref=Artifact(filename="user.png", uri="file:///does/not/exist"),
    )
    req = GenerationRequest(prompt="x", mode="flf2v", assets=[user_first])
    out = stage.run(PipelineState(request=req))
    # User asset preserved bit-identical
    survivors = [a for a in out.request.assets if a.role == "first_frame"]
    assert len(survivors) == 1
    assert survivors[0] is user_first
    # last_frame was generated
    generated = [a for a in out.request.assets if a.role == "last_frame"]
    assert len(generated) == 1
    assert "keyframe-last_frame" in out.artifacts
    # NO keyframe-first_frame in artifacts (we didn't generate it)
    assert "keyframe-first_frame" not in out.artifacts


def test_per_role_prompt_overrides_top_level(tmp_path: Path) -> None:
    cfg = KeyframeConfig(
        engine="fake",
        prompt="default",
        spec={"model": "m"},
        roles={"init_image": KeyframeRoleOverride(prompt="specific")},
    )
    stage = _make_stage(cfg, tmp_path)
    # Resolution helpers are private but observable: capture the prompt
    # via FakeImageBackend submit-id determinism — same prompt → same id.
    state = stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="i2v")))
    # The submit id encodes the prompt; verify by independent recomputation.
    expected_seed = json.dumps(
        ["specific", sorted({"model": "m"}.items())],
        sort_keys=True,
        ensure_ascii=False,
    )
    expected_id = hashlib.sha256(expected_seed.encode("utf-8")).hexdigest()[:16]
    assert (
        f"fake-image-{expected_id}.png"
        in {
            Path(state.artifacts["keyframe-init_image"].filename).name,
            state.artifacts["keyframe-init_image"].meta.get("_kf_job_id", ""),
        }
        or state.artifacts["keyframe-init_image"].meta["_kf_job_id"] == expected_id
    )


def test_missing_prompt_raises_validation(tmp_path: Path) -> None:
    """Bug guard: stage-level defence even though Config-load validator usually catches this."""
    # Construct a KeyframeConfig that passes the load-time validator BUT
    # then strip the prompt on the dataclass — simulates a bug in cfg
    # mutation. Stage must still refuse.
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    cfg = cfg.model_copy(update={"prompt": None})  # bypass validator
    stage = _make_stage(cfg, tmp_path)
    with pytest.raises(ValidationError, match="no prompt"):
        stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="i2v")))


def test_skips_non_image_roles(tmp_path: Path) -> None:
    """Forward-compat: if a future mode adds a non-image role, stage MUST skip it."""
    from kinoforge.core.interfaces import MODE_ROLE_REQUIREMENTS

    MODE_ROLE_REQUIREMENTS["audio_mode"] = {"input_audio": "audio"}
    try:
        cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
        stage = _make_stage(cfg, tmp_path)
        out = stage.run(
            PipelineState(request=GenerationRequest(prompt="x", mode="audio_mode"))
        )
        # No assets added (audio role skipped)
        assert out.request.assets == []
        assert out.artifacts == {}
    finally:
        del MODE_ROLE_REQUIREMENTS["audio_mode"]


def test_t2v_no_required_roles_is_no_op(tmp_path: Path) -> None:
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    out = stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="t2v")))
    assert out.request.assets == []
    assert out.artifacts == {}


def test_original_state_not_mutated(tmp_path: Path) -> None:
    """Bug guard: PipelineState must be frozen; in-place mutation of request.assets is illegal."""
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    req = GenerationRequest(prompt="x", mode="i2v")
    state = PipelineState(request=req)
    out = stage.run(state)
    assert state.request.assets == []  # original untouched
    assert len(out.request.assets) == 1
    assert out is not state


def test_persisted_filename_pattern(tmp_path: Path) -> None:
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    out = stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="i2v")))
    # Filename of the persisted Artifact = keyframe-init_image.png
    art = out.artifacts["keyframe-init_image"]
    assert art.filename == "keyframe-init_image.png"
    # File exists on disk under run_id
    saved = list(tmp_path.glob("**/keyframe-init_image.png"))
    assert len(saved) == 1


def test_per_role_spec_overrides_top_level(tmp_path: Path) -> None:
    """Bug guard: top-level spec MUST be the base; per-role spec layers on top."""
    cfg = KeyframeConfig(
        engine="fake",
        prompt="x",
        spec={"model": "m", "size": "small"},
        roles={"init_image": KeyframeRoleOverride(spec={"size": "large"})},
    )
    stage = _make_stage(cfg, tmp_path)
    # FakeImageBackend submit id depends on spec. Verify resolved spec = {model: m, size: large}.
    out = stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="i2v")))
    expected_spec = sorted({"model": "m", "size": "large"}.items())
    expected_seed = json.dumps(["x", expected_spec], sort_keys=True, ensure_ascii=False)
    expected_id = hashlib.sha256(expected_seed.encode("utf-8")).hexdigest()[:16]
    art = out.artifacts["keyframe-init_image"]
    assert art.meta["_kf_job_id"] == expected_id


def test_appends_asset_at_end_of_request_assets(tmp_path: Path) -> None:
    """Bug guard: preserve insertion order of user assets."""
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    user = ConditioningAsset(
        kind="image",
        role="first_frame",
        ref=Artifact(filename="u.png"),
    )
    req = GenerationRequest(prompt="x", mode="flf2v", assets=[user])
    out = stage.run(PipelineState(request=req))
    assert out.request.assets[0] is user
    assert out.request.assets[1].role == "last_frame"


def test_artifacts_dict_carries_existing_entries(tmp_path: Path) -> None:
    """Bug guard: stage must not drop pre-existing artifacts from upstream stages."""
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    pre = Artifact(filename="pre.png", uri="file:///pre")
    state = PipelineState(
        request=GenerationRequest(prompt="x", mode="i2v"),
        artifacts={"upstream": pre},
    )
    out = stage.run(state)
    assert "upstream" in out.artifacts
    assert out.artifacts["upstream"] is pre
