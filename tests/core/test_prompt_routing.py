"""Unit tests for the shared prompt-routing helper.

Bug catch: orchestrator places the user prompt on Segment.prompt, never in
job.spec. Backends that build body=dict(job.spec) without consulting segments
silently submit empty-prompt jobs (fal.ai shipped the inline fix in Layer-I
Task 13; this helper hoists that pattern for hosted/diffusers/comfyui).
"""

from __future__ import annotations

from kinoforge.core.interfaces import GenerationJob, Segment
from kinoforge.core.prompt_routing import resolve_prompt


def _job(spec: dict, segments: list[Segment]) -> GenerationJob:  # type: ignore[type-arg]
    return GenerationJob(spec=spec, segments=segments, params={})


def test_resolve_returns_spec_prompt_when_set() -> None:
    """Explicit spec.prompt is the canonical source — return as-is."""
    job = _job({"prompt": "explicit"}, [])
    assert resolve_prompt(job) == "explicit"


def test_resolve_returns_segment_prompt_when_spec_lacks_key() -> None:
    """Orchestrator path: spec carries no prompt; fall back to segments[0]."""
    job = _job({}, [Segment(prompt="from-seg", params={}, assets=[])])
    assert resolve_prompt(job) == "from-seg"


def test_resolve_spec_wins_over_segment() -> None:
    """Bug catch: a permissive over-eager helper would clobber an explicit
    config-supplied prompt with the raw segment text."""
    job = _job(
        {"prompt": "explicit"},
        [Segment(prompt="from-seg", params={}, assets=[])],
    )
    assert resolve_prompt(job) == "explicit"


def test_resolve_returns_none_when_neither_present() -> None:
    """No prompt in spec, no segments — helper signals 'nothing to route'."""
    job = _job({}, [])
    assert resolve_prompt(job) is None


def test_resolve_returns_none_when_spec_prompt_empty_and_no_segments() -> None:
    """Empty string does not count as a prompt; with no segments → None."""
    job = _job({"prompt": ""}, [])
    assert resolve_prompt(job) is None


def test_resolve_returns_none_when_spec_prompt_is_non_str() -> None:
    """Bug catch: dict.get('prompt') may return any type; helper must guard
    so the caller never receives e.g. ``42`` and writes it into a JSON body."""
    job = _job({"prompt": 42}, [])
    assert resolve_prompt(job) is None


def test_resolve_returns_none_when_segments_empty() -> None:
    """Empty segment list must not IndexError — return None cleanly."""
    job = _job({}, [])
    assert resolve_prompt(job) is None


def test_resolve_returns_segment_when_spec_prompt_empty_string() -> None:
    """Empty spec.prompt should NOT shadow a valid segment prompt — the
    orchestrator path treats spec absence and spec=='' identically."""
    job = _job(
        {"prompt": ""},
        [Segment(prompt="from-seg", params={}, assets=[])],
    )
    assert resolve_prompt(job) == "from-seg"
