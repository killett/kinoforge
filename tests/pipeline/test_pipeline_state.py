"""Layer R T1: PipelineState dataclass + Stage Protocol structural check."""

from __future__ import annotations

import dataclasses

from kinoforge.core.interfaces import (
    GenerationRequest,
    PipelineState,
    Stage,
)


def test_pipeline_state_is_frozen() -> None:
    """PipelineState must be frozen so accidental mutation of `request` raises.
    Bug guard: a thawed state lets a stage silently swap request and break the next stage."""
    state = PipelineState(request=GenerationRequest(prompt="p", mode="t2v"))
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        state.request = GenerationRequest(prompt="q", mode="t2v")  # type: ignore[misc]


def test_pipeline_state_artifacts_default_empty_dict() -> None:
    state = PipelineState(request=GenerationRequest(prompt="p", mode="t2v"))
    assert state.artifacts == {}


def test_stage_protocol_matches_callable_with_state_signature() -> None:
    """Anything with `run(self, state) -> PipelineState` satisfies Stage Protocol.
    Bug guard: tightening the Protocol incorrectly would break runtime_checkable."""

    class _Concrete:
        def run(self, state: PipelineState) -> PipelineState:
            return state

    s: Stage = _Concrete()
    state = PipelineState(request=GenerationRequest(prompt="p", mode="t2v"))
    out = s.run(state)
    assert out is state
