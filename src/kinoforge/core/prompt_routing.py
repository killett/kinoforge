"""Cross-engine prompt-routing helper.

The orchestrator places the user's prompt on ``Segment.prompt``, not in
``GenerationJob.spec``. Backends that build their request body from
``job.spec`` alone silently drop the prompt — the same defect FalBackend
patched inline in Layer-I Task 13. This module hoists that pattern into
one pure function shared by every engine.

Pure / no I/O / no state — safe to call from ``submit`` and
``validate_spec`` without side effects.
"""

from __future__ import annotations

from kinoforge.core.interfaces import GenerationJob


def resolve_prompt(job: GenerationJob) -> str | None:
    """Return the prompt to route into the request body, or ``None``.

    Precedence: ``job.spec["prompt"]`` (explicit, config-supplied) wins
    over ``job.segments[0].prompt`` (orchestrator path). Empty strings
    and non-``str`` values do not count.

    Args:
        job: The :class:`~kinoforge.core.interfaces.GenerationJob` whose
            prompt to resolve.

    Returns:
        The prompt string, or ``None`` if neither location holds a
        non-empty ``str``.
    """
    spec_prompt = job.spec.get("prompt")
    if isinstance(spec_prompt, str) and spec_prompt:
        return spec_prompt
    if job.segments:
        seg_prompt = getattr(job.segments[0], "prompt", "")
        if isinstance(seg_prompt, str) and seg_prompt:
            return seg_prompt
    return None
