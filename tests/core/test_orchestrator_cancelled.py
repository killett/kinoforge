"""Stage-loop ``Cancelled`` arm — no destroy, WARN with pod ID + reap text.

Sibling test to ``test_orchestrator_interrupt.py``. Same assertions; the only
difference is that the stage raises :class:`kinoforge.core.Cancelled` instead
of ``KeyboardInterrupt``. The orchestrator's new
``(KeyboardInterrupt, Cancelled)`` arm must catch both cooperatively.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import kinoforge.engines.fake  # noqa: F401 — self-register fake engine
import kinoforge.providers.local  # noqa: F401 — self-register local provider
import kinoforge.sources.http  # noqa: F401 — self-register https:// source
from kinoforge.core.cancel import CancelToken  # noqa: F401 — symbol import sanity check
from kinoforge.core.config import Config, load_config
from kinoforge.core.errors import Cancelled
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


def test_cancelled_during_stage_does_not_destroy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``Cancelled`` raised by GenerateClipStage.run keeps the pod alive.

    Bug: the existing stage-loop except clause caught ``ValidationError``
    only, so a worker that cooperatively raises ``Cancelled`` (from a
    backend honoring its ``cancel_token``) would propagate with no log
    and no destroy — leaving the operator unaware of the warm pod.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="a sunset", mode="t2v")
    engine = _make_engine()
    provider = _DestroySpyProvider()

    import kinoforge.pipeline.generate_clip as gc_mod

    def _raise_cancelled(_self: Any, _state: Any) -> Any:
        raise Cancelled("worker cancelled by token")

    monkeypatch.setattr(gc_mod.GenerateClipStage, "run", _raise_cancelled)

    caplog.set_level(logging.WARNING, logger="kinoforge.orchestrator")

    with pytest.raises(Cancelled):
        generate(
            cfg,
            request,
            store=store,
            provider=provider,
            engine=engine,
            state_dir=tmp_path,
        )

    assert provider.destroy_calls == [], (
        f"pod must NOT be destroyed on Cancelled, "
        f"got destroys={provider.destroy_calls!r}"
    )

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


def test_cancelled_during_keyframe_stage_does_not_destroy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``Cancelled`` raised by KeyframeStage.run keeps the pod alive."""
    import kinoforge.image_engines.fake  # noqa: F401

    cfg = _load_keyframe_cfg()
    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="a sunset", mode="t2v")
    engine = _make_engine()
    provider = _DestroySpyProvider()

    import kinoforge.pipeline.keyframe as kf_mod

    def _raise_cancelled(_self: Any, _state: Any) -> Any:
        raise Cancelled("worker cancelled by token")

    monkeypatch.setattr(kf_mod.KeyframeStage, "run", _raise_cancelled)

    caplog.set_level(logging.WARNING, logger="kinoforge.orchestrator")

    with pytest.raises(Cancelled):
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
        f"pod must NOT be destroyed on Cancelled in keyframe stage, "
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


def test_generate_accepts_cancel_token_kwarg(tmp_path: Path) -> None:
    """``generate(..., cancel_token=CancelToken())`` must be accepted as a kwarg.

    Bug guard: without the new ``cancel_token`` kwarg on ``generate``,
    callers (CLI handler, Task 5) cannot thread the token down. This
    test pins the public signature.
    """
    from kinoforge.core.cancel import CancelToken as _CT

    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="a sunset", mode="t2v")
    engine = _make_engine()
    provider = LocalProvider()

    artifact, _ = generate(
        cfg,
        request,
        store=store,
        provider=provider,
        engine=engine,
        state_dir=tmp_path,
        cancel_token=_CT(),
    )
    assert artifact is not None
