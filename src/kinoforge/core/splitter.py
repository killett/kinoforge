"""HeuristicSplitter: in-core default that splits prompts on blank-line markers.

Pure function. No I/O. Self-registers under the name ``"heuristic"`` at import
time. Future LLM-semantic or scene-detect splitters plug in as adapters under
``src/kinoforge/splitters/<name>/`` and register via ``_adapters.py``.
"""

from __future__ import annotations

import re

from kinoforge.core import registry
from kinoforge.core.interfaces import ModelProfile, Segment, Splitter

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")


class HeuristicSplitter(Splitter):
    """Split a prompt on blank-line boundaries.

    Each non-empty paragraph (after stripping whitespace) becomes one
    :class:`~kinoforge.core.interfaces.Segment`. Runs of newlines collapse
    rather than yielding empty middle segments. The ``profile`` and ``params``
    arguments are accepted for ABC compliance and reserved for future
    duration-aware strategies; the heuristic itself does not consult them.

    Single-paragraph prompts pass through as a 1-element list so existing
    single-segment callers see no behavioural change.
    """

    name = "heuristic"

    def split(
        self,
        prompt: str,
        profile: ModelProfile,
        params: dict,  # type: ignore[type-arg]
    ) -> list[Segment]:
        """Return ordered ``Segment``s carrying paragraph-sized prompt chunks.

        Args:
            prompt: The user-supplied prompt; paragraphs separated by blank lines.
            profile: The model's capability profile (unused by the heuristic).
            params: Engine-neutral params (unused by the heuristic; not mutated).

        Returns:
            An ordered list of ``Segment`` objects, length >= 1.

        Raises:
            ValueError: ``prompt`` yields zero non-empty segments after stripping.
        """
        chunks = [c.strip() for c in _PARAGRAPH_BREAK.split(prompt)]
        chunks = [c for c in chunks if c]
        if not chunks:
            raise ValueError("prompt yielded zero non-empty segments")
        return [Segment(prompt=c) for c in chunks]


registry.register_splitter("heuristic", lambda: HeuristicSplitter())
