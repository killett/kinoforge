"""Tail-frame asset injection for non-native multi-segment runs.

Pure helper. The engine + extract + persist + wrap pipeline lives in
GenerateClipStage; this module is side-effect-free dataclass juggling.
"""

from __future__ import annotations

from dataclasses import replace

from kinoforge.core.interfaces import (
    ConditioningAsset,
    GenerationJob,
)


def inject_tail_frame(
    next_job: GenerationJob,
    tail_asset: ConditioningAsset,
) -> GenerationJob:
    """Return a copy of next_job with seg-0 assets replaced by [tail_asset].

    Splitter contract guarantees ``next_job.segments[0].assets == []``; this
    helper replaces that list with ``[tail_asset]``. Segments beyond index 0
    are unchanged. Original is not mutated.

    Args:
        next_job: The job that will be submitted next.
        tail_asset: The conditioning asset (typically built by the stage from
            ``engine.extract_last_frame`` bytes persisted into the store).

    Returns:
        New GenerationJob with the conditioning hand-off applied.
    """
    new_seg_0 = replace(next_job.segments[0], assets=[tail_asset])
    return replace(next_job, segments=[new_seg_0, *next_job.segments[1:]])
