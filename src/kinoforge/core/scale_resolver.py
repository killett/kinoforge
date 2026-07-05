"""Pure height-target -> factor resolver for the upscale pipeline.

Engine-agnostic: consumes an engine's declared factor menu and decides which
multiplier (if any) to run and whether a post-upscale downscale is needed to hit
the requested vertical resolution. No I/O, no torch -- a pure function so the
whole decision table is unit-tested with zero cloud spend.
"""

from __future__ import annotations

from dataclasses import dataclass

from kinoforge.core.errors import ScaleUnsatisfiableError


@dataclass(frozen=True)
class HeightPlan:
    """Resolved plan for a height-target upscale.

    Attributes:
        upscale_factor: Multiplier to run the engine at, or ``None`` when the
            source already meets/exceeds the target (skip the GPU upscale).
        downscale_to: Vertical resolution to downscale to after upscaling, or
            ``None`` when nothing needs shrinking (exact hit or passthrough).
    """

    upscale_factor: float | None
    downscale_to: int | None


def resolve_height_target(
    source_h: int,
    supported_factors: tuple[float, ...],
    requested_h: int,
) -> HeightPlan:
    """Resolve a requested vertical resolution against an engine's factor menu.

    Picks the smallest supported factor whose result meets or exceeds
    ``requested_h`` (least overshoot -> smallest intermediate + least downscale
    loss), and reports whether a post-upscale downscale is needed.

    Args:
        source_h: Source clip vertical resolution in pixels.
        supported_factors: The engine's declared upscale factors (e.g.
            ``(4.0,)`` for FlashVSR, ``(2.0, 4.0)`` for SeedVR2). Non-empty.
        requested_h: Requested output vertical resolution in pixels.

    Returns:
        A :class:`HeightPlan`.

    Raises:
        ValueError: ``supported_factors`` is empty.
        ScaleUnsatisfiableError: Even the largest factor cannot reach the target.
    """
    if not supported_factors:
        raise ValueError("supported_factors must be non-empty for a height target")
    if source_h >= requested_h:
        downscale_to = None if source_h == requested_h else requested_h
        return HeightPlan(upscale_factor=None, downscale_to=downscale_to)
    candidates = sorted(f for f in supported_factors if source_h * f >= requested_h)
    if not candidates:
        largest = max(supported_factors)
        raise ScaleUnsatisfiableError(
            source_h=source_h,
            largest_factor=largest,
            reached_h=int(source_h * largest),
            requested_h=requested_h,
        )
    factor = candidates[0]
    downscale_to = None if source_h * factor == requested_h else requested_h
    return HeightPlan(upscale_factor=factor, downscale_to=downscale_to)
