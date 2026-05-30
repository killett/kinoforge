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


# ---------------------------------------------------------------------------
# Task 2 — HeuristicSplitter tests
# ---------------------------------------------------------------------------

from kinoforge.core.splitter import HeuristicSplitter  # noqa: E402


def test_heuristic_single_paragraph_passthrough():
    # Bug: regex over-eagerly splits on single newlines or whitespace,
    # breaking every existing single-segment test in the suite.
    out = HeuristicSplitter().split("one paragraph", _profile(), {})
    assert len(out) == 1
    assert out[0].prompt == "one paragraph"


def test_heuristic_double_newline_splits():
    # Bug: regex fails to recognise the blank-line marker, so the splitter
    # silently collapses a multi-paragraph prompt into one Segment and the
    # whole feature does nothing for the simplest case it must handle.
    out = HeuristicSplitter().split("a\n\nb", _profile(), {})
    assert [s.prompt for s in out] == ["a", "b"]


def test_heuristic_collapses_runs_of_newlines():
    # Bug: `re.split(r"\n\n")` against `"a\n\n\n\nb"` yields ["a", "", "b"];
    # we must collapse so the middle empty segment never reaches downstream.
    out = HeuristicSplitter().split("a\n\n\n\nb", _profile(), {})
    assert [s.prompt for s in out] == ["a", "b"]


def test_heuristic_strips_whitespace_per_segment():
    # Bug: leading/trailing whitespace silently inflates the prompt sent
    # to backends; many engines treat "  cat" and "cat" as different prompts
    # (different tokenisation), so leaks cause spurious cache misses.
    out = HeuristicSplitter().split("  a  \n\n  b  ", _profile(), {})
    assert [s.prompt for s in out] == ["a", "b"]


def test_heuristic_preserves_inparagraph_single_newline():
    # Bug: someone over-aggressively normalises whitespace and destroys
    # intentional line breaks inside a paragraph (e.g. "a\nb" for line layout).
    out = HeuristicSplitter().split("a\nb\n\nc", _profile(), {})
    assert [s.prompt for s in out] == ["a\nb", "c"]


def test_heuristic_all_whitespace_raises_value_error():
    # Bug: all-whitespace prompt silently produces zero segments and the
    # downstream pool gets an empty job list — NPE or worse.
    with pytest.raises(ValueError):
        HeuristicSplitter().split("   \n\n   ", _profile(), {})


def test_heuristic_segments_have_empty_assets_and_params():
    # Bug: defaults accidentally share state across calls or splitter
    # writes back into Segment defaults via mutation.
    out = HeuristicSplitter().split("a\n\nb", _profile(), {})
    for seg in out:
        assert seg.assets == []
        assert seg.params == {}


def test_heuristic_does_not_mutate_caller_params():
    # Bug: splitter writes into the caller's params dict and downstream
    # callers see unexpected keys appear.
    caller_params = {"seed": 42, "steps": 30}
    snapshot = dict(caller_params)
    HeuristicSplitter().split("a\n\nb", _profile(), caller_params)
    assert caller_params == snapshot


def test_heuristic_self_registers_under_heuristic():
    # Bug: someone forgets the registry.register_splitter line at module
    # footer and the orchestrator default lookup fails at runtime.
    instance = registry.get_splitter("heuristic")()
    assert isinstance(instance, HeuristicSplitter)
