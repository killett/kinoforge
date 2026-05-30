"""Tests for the Splitter ABC + registry plumbing (Task 1)."""

from __future__ import annotations

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import (
    ModelProfile,
    Segment,
    Splitter,  # The ABC under test.
)


def _profile() -> ModelProfile:
    """Build a minimal ModelProfile fixture for splitter calls."""
    return ModelProfile(
        name="fake",
        max_frames=24,
        fps=12,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


class _IncompleteSplitter(Splitter):
    """Subclass that does not implement split — should not be instantiable."""

    name = "incomplete"


def test_splitter_is_abstract_method_required():
    # Bug: someone removes @abstractmethod and downstream plugins
    # silently skip the contract.
    with pytest.raises(TypeError):
        _IncompleteSplitter()  # type: ignore[abstract]


class _ConcreteSplitter(Splitter):
    """Minimal concrete subclass used to prove the contract is implementable."""

    name = "concrete"

    def split(self, prompt: str, profile: ModelProfile, params: dict) -> list[Segment]:  # type: ignore[type-arg]
        return [Segment(prompt=prompt)]


def test_concrete_splitter_returns_segments():
    # Bug: ABC signature drifts (e.g., wrong arg order) and subclasses break.
    s = _ConcreteSplitter()
    out = s.split("hello", _profile(), {})
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0].prompt == "hello"


def test_register_and_get_splitter_round_trip():
    # Bug: registry stores instances instead of factories, or loses entries
    # under the wrong key.
    registry.register_splitter("rt_test", lambda: _ConcreteSplitter())
    factory = registry.get_splitter("rt_test")
    instance = factory()
    assert isinstance(instance, _ConcreteSplitter)


def test_get_splitter_unknown_raises_unknown_adapter():
    # Bug: silent fallthrough on bad config produces opaque runtime error;
    # caller should see a clear "not registered" message.
    with pytest.raises(UnknownAdapter) as exc_info:
        registry.get_splitter("definitely_not_registered_xyz")
    assert "definitely_not_registered_xyz" in str(exc_info.value)


def test_register_splitter_reregistration_overwrites():
    # Bug: duplicate registrations stack instead of replace, leaking memory
    # and creating subtle behaviour differences between fresh and re-imported
    # modules.
    registry.register_splitter("dup", lambda: _ConcreteSplitter())

    class _Other(_ConcreteSplitter):
        name = "other"

    registry.register_splitter("dup", lambda: _Other())
    instance = registry.get_splitter("dup")()
    assert isinstance(instance, _Other)
