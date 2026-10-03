"""resolve_image_stack: the one place an image block becomes a live stack."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kinoforge.core.config import ImageConfig
from kinoforge.core.errors import ProfileNotCached, UnknownAdapter
from kinoforge.core.interfaces import CapabilityKey, ImageProfile, ImageProfileProvider

BLOCK = ImageConfig(engine="fake", prompt="a cat", spec={"model": "m"})

PROFILE = ImageProfile(
    name="fake-image", max_resolution=(1024, 1024), supported_modes={"t2i"}
)


@dataclass
class _RecordingProvider(ImageProfileProvider):
    """ImageProfileProvider double that records which path was taken."""

    cached: ImageProfile | None = None
    resolve_calls: list[CapabilityKey] = field(default_factory=list)
    discover_calls: list[CapabilityKey] = field(default_factory=list)

    def resolve(self, key: CapabilityKey) -> ImageProfile:
        self.resolve_calls.append(key)
        if self.cached is None:
            raise ProfileNotCached(str(key))
        return self.cached

    def discover(self, key: CapabilityKey, engine: Any, backend: Any) -> ImageProfile:
        self.discover_calls.append(key)
        return PROFILE

    def verify(self, *a: Any, **k: Any) -> None:  # pragma: no cover - unused here
        return None


def test_returns_the_triple_for_a_registered_engine(tmp_path: Any) -> None:
    """The happy path: registry name in, live stack out.

    Bug this catches: an extraction that returns the engine but drops the
    backend or profile, which would push the missing construction back out to
    all three call sites — exactly the duplication this removes.
    """
    import kinoforge._adapters  # noqa: F401  — registers the fake image engine
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    provider = _RecordingProvider(cached=PROFILE)
    engine, backend, profile = resolve_image_stack(
        BLOCK, store=store, image_profile_provider=provider
    )
    assert engine.name == "fake"
    assert backend is not None
    assert profile == PROFILE


def test_unknown_engine_raises_before_any_provisioning(tmp_path: Any) -> None:
    """Registry lookup happens FIRST, so a typo costs nothing.

    Bug this catches: resolving the profile (a cache write, potentially a live
    probe) before discovering the engine name is bogus — the whole reason the
    original orchestrator comment says "unknown engine names fail fast here
    before any compute spend".
    """
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    provider = _RecordingProvider(cached=PROFILE)
    bad = ImageConfig(engine="no-such-image-engine", prompt="x", spec={"model": "m"})
    with pytest.raises(UnknownAdapter, match="no-such-image-engine"):
        resolve_image_stack(bad, store=store, image_profile_provider=provider)
    assert provider.resolve_calls == [], "profile was resolved despite a bad engine"


def test_cached_profile_does_not_trigger_discover(tmp_path: Any) -> None:
    """A warm cache must not re-probe.

    Bug this catches: calling discover unconditionally, which for a live engine
    means an extra provider round trip on every single run.
    """
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    provider = _RecordingProvider(cached=PROFILE)
    resolve_image_stack(BLOCK, store=store, image_profile_provider=provider)
    assert len(provider.resolve_calls) == 1
    assert provider.discover_calls == []


def test_profile_not_cached_falls_through_to_discover(tmp_path: Any) -> None:
    """A cold cache discovers exactly once.

    Bug this catches: letting ProfileNotCached escape to the caller, which would
    make the first run of every new (engine, model) pair fail.
    """
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    provider = _RecordingProvider(cached=None)
    _, _, profile = resolve_image_stack(
        BLOCK, store=store, image_profile_provider=provider
    )
    assert profile == PROFILE
    assert len(provider.discover_calls) == 1


def test_injected_engine_overrides_the_registry(tmp_path: Any) -> None:
    """Test-injection seam, matching orchestrator.generate / batch.

    Bug this catches: ignoring the injected engine, which would make every
    offline test of the image path reach the real registry.
    """
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.image_engines.fake import FakeImageEngine
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    sentinel = FakeImageEngine()
    engine, _, _ = resolve_image_stack(
        BLOCK,
        store=store,
        image_engine=sentinel,
        image_profile_provider=_RecordingProvider(cached=PROFILE),
    )
    assert engine is sentinel


def test_both_call_sites_use_the_helper() -> None:
    """Structural guard: the extraction must DELETE both copies.

    Bug this catches: adding image_stack.py as a third implementation while the
    orchestrator and batch copies stay — the duplication the design set out to
    remove would then have gone UP, not down, and nothing else in this suite
    would notice.
    """
    from pathlib import Path

    import kinoforge.core.batch as batch_mod
    import kinoforge.core.orchestrator as orch_mod

    for mod in (orch_mod, batch_mod):
        assert mod.__file__ is not None
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "get_image_engine(" not in source, (
            f"{mod.__name__} still resolves an image engine directly; it must "
            f"call resolve_image_stack"
        )
        assert "resolve_image_stack" in source, (
            f"{mod.__name__} does not call resolve_image_stack"
        )
