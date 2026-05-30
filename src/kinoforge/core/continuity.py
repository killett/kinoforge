"""Tail-frame conditioning for non-native multi-segment runs.

Pure helper. The interleaved render -> extract -> inject -> render loop lives
in GenerateClipStage; this module is side-effect-free.
"""

from __future__ import annotations

from dataclasses import replace

from kinoforge.core.interfaces import (
    Artifact,
    GenerationEngine,
    GenerationJob,
)


def inject_tail_frame(
    next_job: GenerationJob,
    prev_artifact: Artifact,
    engine: GenerationEngine,
) -> GenerationJob:
    """Return a copy of next_job with prev's tail as seg-0 init_image.

    Splitter contract guarantees next_job.segments[0].assets == []; the helper
    replaces that list with [tail_asset]. Other segments in next_job (if any)
    are unchanged. Original is not mutated.

    Args:
        next_job: The job that will be submitted next.
        prev_artifact: The artifact returned by the previous job's render.
        engine: Engine that knows how to extract a frame.

    Returns:
        New GenerationJob with the conditioning hand-off applied.

    Raises:
        NotImplementedError: engine.extract_last_frame raises.
    """
    tail_asset = engine.extract_last_frame(prev_artifact)
    new_seg_0 = replace(next_job.segments[0], assets=[tail_asset])
    return replace(next_job, segments=[new_seg_0, *next_job.segments[1:]])
