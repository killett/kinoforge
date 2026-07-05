"""Finalize upscaled bytes at the orchestrator materialize boundary.

The upscaled artifact's bytes are only local once the orchestrator fetches them
after the stage walk. If UpscaleStage stashed a ``downscale_to`` (height-target
overshoot), shrink here before the sink publishes the deliverable.
"""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.pipeline.downscale import downscale_video_bytes


def finalize_upscaled_bytes(
    body: bytes,
    downscale_to: int | None,
    *,
    downscale: Callable[[bytes, int], bytes] = downscale_video_bytes,
) -> bytes:
    """Return *body*, lanczos-downscaled to ``downscale_to`` when set.

    Args:
        body: Local upscaled video bytes.
        downscale_to: Target vertical resolution, or ``None`` for passthrough.
        downscale: Injectable downscale seam ``(body, target_h) -> bytes``.

    Returns:
        The (possibly downscaled) video bytes.
    """
    if downscale_to is not None:
        return downscale(body, downscale_to)
    return body
