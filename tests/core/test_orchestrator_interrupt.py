"""Stage-loop ``KeyboardInterrupt`` arm — no destroy, WARN with pod ID + reap text.

Bug context (Phase 50): a 2026-06-10 Wan 14B t2v live smoke hung silently after
``provisioner.provision`` returned, required two ``Ctrl-C`` presses to escape,
and left ``provider.destroy_instance`` unrun. The stage-loop except clause used
to only catch ``ValidationError``; a ``KeyboardInterrupt`` from the worker
propagated with no log line, no destroy, and the operator had no way to know
the pod was alive without checking RunPod.

These tests pin the new behavior: when a stage raises ``KeyboardInterrupt``,
``provider.destroy_instance`` is NOT called (warm-reuse intent preserved per
commit ``3bc6473``), and the orchestrator emits a WARN naming the surviving
pod ID + ``kinoforge reap`` recovery command.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import kinoforge.engines.fake  # noqa: F401 — self-register fake engine
import kinoforge.providers.local  # noqa: F401 — self-register local provider
import kinoforge.sources.http  # noqa: F401 — self-register https:// source
from kinoforge.core.config import Config, load_config
from kinoforge.core.interfaces import (
    CapabilityKey,
    GenerationRequest,
    ImageProfile,
    ImageProfileProvider,
    Instance,
    InstanceSpec,
    ModelProfile,
)
from kinoforge.core.orchestrator import generate
from kinoforge.engines.fake import FakeEngine
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

_COMPUTE_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
"""

_KEYFRAME_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
keyframe:
  engine: fake
  prompt: "a vivid keyframe"
  spec:
    model: "fake-model"
"""


def _probe_profile() -> ModelProfile:
    return ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def _make_engine() -> FakeEngine:
    return FakeEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys=set(),
    )


def _compute_cfg() -> Config:
    return load_config(_COMPUTE_YAML)


def _load_keyframe_cfg() -> Config:
    return load_config(_KEYFRAME_YAML)


class _DestroySpyProvider(LocalProvider):
    """LocalProvider that records every ``destroy_instance`` call + last-created id."""

    def __init__(self) -> None:
        super().__init__()
        self.destroy_calls: list[str] = []
        self.last_created_id: str | None = None

    def create_instance(self, spec: InstanceSpec) -> Instance:
        inst = super().create_instance(spec)
        self.last_created_id = inst.id
        return inst

    def destroy_instance(self, instance_id: str) -> None:
        self.destroy_calls.append(instance_id)
        super().destroy_instance(instance_id)


class _AlwaysCachedImageProfileProvider(ImageProfileProvider):
    """Minimal ImageProfileProvider stub; resolve() always hits the cache."""

    def __init__(self) -> None:
        self._profile = ImageProfile(
            name="fake-image",
            max_resolution=(1024, 1024),
            supported_modes={"t2i"},
        )

    def resolve(self, key: CapabilityKey) -> ImageProfile:
        return self._profile

    def discover(self, key: CapabilityKey, engine: Any, backend: Any) -> ImageProfile:
        return self._profile

    def verify(
        self,
        profile: ImageProfile,
        backend: Any,
        *,
        engine: Any = None,
        key: Any = None,
    ) -> None:
        return None


def test_keyboard_interrupt_during_stage_does_not_destroy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """KeyboardInterrupt mid-stage keeps the pod alive (warm-reuse intent).

    Bug: without the new ``(KeyboardInterrupt, Cancelled)`` arm on the
    stage loop, the interrupt propagates with no log line, no destroy,
    and the operator has no way to know the pod is alive.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="a sunset", mode="t2v")
    engine = _make_engine()
    provider = _DestroySpyProvider()

    # Force GenerateClipStage.run() to raise KeyboardInterrupt on first call.
    import kinoforge.pipeline.generate_clip as gc_mod

    def _raise_interrupt(_self: Any, _state: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(gc_mod.GenerateClipStage, "run", _raise_interrupt)

    caplog.set_level(logging.WARNING, logger="kinoforge.orchestrator")

    with pytest.raises(KeyboardInterrupt):
        generate(
            cfg,
            request,
            store=store,
            provider=provider,
            engine=engine,
            state_dir=tmp_path,
        )

    # AC: provider.destroy_instance is NOT called.
    assert provider.destroy_calls == [], (
        f"pod must NOT be destroyed on interrupt, "
        f"got destroys={provider.destroy_calls!r}"
    )

    # AC: exactly one WARN line containing the pod ID and `kinoforge reap`.
    warn_records = [
        r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING and "kinoforge reap" in r.message
    ]
    assert len(warn_records) == 1, (
        f"expected exactly one WARN with `kinoforge reap`, got: {warn_records}"
    )
    assert provider.last_created_id is not None
    assert provider.last_created_id in warn_records[0], (
        f"WARN must name the surviving pod id "
        f"{provider.last_created_id!r}: {warn_records[0]!r}"
    )


def test_keyboard_interrupt_during_keyframe_stage_does_not_destroy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """KeyboardInterrupt raised by KeyframeStage.run() → no destroy + WARN.

    The keyframe try/except is a separate code site from the main stage
    loop and needs its own coverage; both arms get the same
    ``(KeyboardInterrupt, Cancelled)`` clause.
    """
    import kinoforge.image_engines.fake  # noqa: F401 — registers "fake" image engine

    cfg = _load_keyframe_cfg()
    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="a sunset", mode="t2v")
    engine = _make_engine()
    provider = _DestroySpyProvider()

    # Force KeyframeStage.run() to raise KeyboardInterrupt on first call.
    import kinoforge.pipeline.keyframe as kf_mod

    def _raise_interrupt(_self: Any, _state: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(kf_mod.KeyframeStage, "run", _raise_interrupt)

    caplog.set_level(logging.WARNING, logger="kinoforge.orchestrator")

    with pytest.raises(KeyboardInterrupt):
        generate(
            cfg,
            request,
            store=store,
            provider=provider,
            engine=engine,
            image_profile_provider=_AlwaysCachedImageProfileProvider(),
            state_dir=tmp_path,
        )

    assert provider.destroy_calls == [], (
        f"pod must NOT be destroyed on interrupt in keyframe stage, "
        f"got destroys={provider.destroy_calls!r}"
    )

    warn_records = [
        r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING and "kinoforge reap" in r.message
    ]
    assert len(warn_records) == 1, (
        f"expected exactly one WARN with `kinoforge reap` from keyframe stage, "
        f"got: {warn_records}"
    )
    assert provider.last_created_id is not None
    assert provider.last_created_id in warn_records[0], (
        f"WARN must name the surviving pod id "
        f"{provider.last_created_id!r}: {warn_records[0]!r}"
    )
