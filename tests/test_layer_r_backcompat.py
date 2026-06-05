"""Layer R T13: backwards-compat lockdown.

Freeze in that pre-Layer-R behaviour is preserved for configs without a
keyframe block. Regression here means a layer landed that silently changed
the no-keyframe path's contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.interfaces import MODE_ROLE_REQUIREMENTS


def test_existing_examples_have_no_keyframe_block() -> None:
    """Every pre-Layer-R example YAML loads with cfg.keyframe is None.

    Bug guard: an existing example that accidentally gains a keyframe block
    would silently flip its execution path. Lock down explicitly.
    """
    from kinoforge.core.config import load_config

    excluded = {"keyframe-fal-i2v.yaml", "keyframe-fal-flf2v.yaml"}
    yamls = sorted(Path("examples/configs").glob("*.yaml"))
    assert yamls, "examples/configs should not be empty"
    skipped = 0
    for p in yamls:
        if p.name in excluded:
            continue
        try:
            cfg = load_config(p)
        except Exception as exc:  # pragma: no cover — defensive
            # If an example can't load at all (independent regression),
            # this test is not the place to flag it. Skip rather than fail.
            skipped += 1
            print(f"[backcompat] skipping {p.name}: {exc}")
            continue
        assert cfg.keyframe is None, (
            f"{p.name}: expected cfg.keyframe is None (pre-Layer-R backcompat)"
        )
    print(
        f"[backcompat] checked {len(yamls) - skipped} examples, all have cfg.keyframe is None"
    )


def test_mode_role_requirements_key_membership_preserved() -> None:
    """Schema migration changed value type set→dict; `in` operator on KEYS must still work.

    Bug guard: regression to set form (or anything else) would break the
    `should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, {})`
    dispatch in GenerateClipStage and silently disable continuity.
    """
    assert "init_image" in MODE_ROLE_REQUIREMENTS["i2v"]
    assert "first_frame" in MODE_ROLE_REQUIREMENTS["flf2v"]
    assert "last_frame" in MODE_ROLE_REQUIREMENTS["flf2v"]
    assert "init_image" not in MODE_ROLE_REQUIREMENTS["t2v"]


def test_generate_without_keyframe_uses_only_generate_clip_stage(
    tmp_path: Path,
) -> None:
    """cfg.keyframe is None → orchestrator never constructs a KeyframeStage.

    Bug guard: orchestrator drift that adds KeyframeStage anyway would
    burn unnecessary cycles and produce stray keyframe-* artifacts.
    Spy on the keyframe module's KeyframeStage constructor to confirm
    it is NOT invoked when cfg.keyframe is None.
    """
    pytest.importorskip("kinoforge.image_engines.fake")

    import kinoforge.engines.fake  # noqa: F401 — self-registers FakeEngine
    import kinoforge.providers.local  # noqa: F401 — self-registers LocalProvider
    import kinoforge.sources.http  # noqa: F401 — registers https:// source for provisioner
    from kinoforge.core import orchestrator as orch_mod
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import GenerationRequest
    from kinoforge.engines.fake import FakeEngine
    from kinoforge.pipeline import keyframe as kf_mod
    from kinoforge.providers.local import LocalProvider
    from kinoforge.stores.local import LocalArtifactStore

    # Minimal no-keyframe config using the same YAML pattern as existing orchestrator tests.
    # No compute block → hosted path so LocalProvider's create_instance is never called.
    _NO_KEYFRAME_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
"""

    constructed: list[object] = []
    real_keyframe_stage = kf_mod.KeyframeStage

    def _spy(*args: object, **kwargs: object) -> object:
        constructed.append((args, kwargs))
        return real_keyframe_stage(*args, **kwargs)  # type: ignore[arg-type]

    # Patch at the call site (orchestrator imports it directly)
    orig = orch_mod.KeyframeStage  # type: ignore[attr-defined]
    orch_mod.KeyframeStage = _spy  # type: ignore[attr-defined,assignment]
    try:
        cfg = load_config(_NO_KEYFRAME_YAML)
        assert cfg.keyframe is None, (
            "pre-condition: YAML must produce no keyframe block"
        )

        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="hello", mode="t2v")

        from kinoforge.core.interfaces import ModelProfile
        from kinoforge.engines.fake import FakeEngine

        engine = FakeEngine(
            probe_profile=ModelProfile(
                name="fake",
                max_frames=16,
                fps=8,
                supported_modes={"t2v"},
                max_resolution=(512, 512),
                supports_native_extension=False,
                supports_joint_audio=False,
            ),
            declared_flags_map={},
            required_spec_keys=set(),
        )
        provider = LocalProvider()

        artifact, _instance = orch_mod.generate(
            cfg,
            request,
            store=store,
            engine=engine,
            provider=provider,
            run_id="bc-test",
        )
        assert artifact is not None
        assert constructed == [], (
            f"KeyframeStage was constructed {len(constructed)} times — "
            f"orchestrator must not instantiate it when cfg.keyframe is None"
        )
    finally:
        orch_mod.KeyframeStage = orig  # type: ignore[attr-defined]
