"""Pure fps-target resolver for the frame-interpolation pipeline.

Engine-agnostic: consumes an engine's timestep capability and decides whether
to skip the GPU (decimate a downshift), synthesize an exact arbitrary-timestep
schedule, or drive a recursive-2x engine and then decimate to the exact target.
No I/O, no torch — a pure function so the whole decision table is unit-tested
with zero cloud spend.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class InterpCapability(Enum):
    """How an interpolation engine reaches an intermediate time.

    ARBITRARY_TIMESTEP: give it t in (0,1) between two frames (RIFE v4,
    GIMM-VFI) -> hit any target fps in one pass.
    RECURSIVE_2X: only halves intervals (FILM, GMFSS-classic) -> overshoot to
    a power-of-two multiple, then decimate to the exact target.
    """

    ARBITRARY_TIMESTEP = "arbitrary_timestep"
    RECURSIVE_2X = "recursive_2x"


@dataclass(frozen=True)
class FpsPlan:
    """Resolved plan for an fps-target interpolation.

    Attributes:
        schedule: For arbitrary-timestep engines, one ``(source_index,
            timestep)`` per OUTPUT frame; ``timestep == 0.0`` copies the source
            frame, else synthesize between ``source_index`` and the next frame.
            ``None`` for recursive/decimate/passthrough plans.
        recursion_depth: For recursive-2x engines, ``log2`` of the insertion
            factor (depth 2 -> x4). ``None`` otherwise.
        decimate_to: Exact fps to ffmpeg-decimate to after synthesis (recursive
            overshoot) or instead of it (downshift). ``None`` when the synthesis
            already lands exactly on target or on passthrough.
        skip_gpu: ``True`` when no GPU pod is needed (passthrough or pure
            decimation).
    """

    schedule: tuple[tuple[int, float], ...] | None
    recursion_depth: int | None
    decimate_to: float | None
    skip_gpu: bool


def _next_pow2(n: int) -> int:
    """Smallest power of two >= ``n`` (n >= 1)."""
    return 1 << (n - 1).bit_length()


def resolve_fps_target(
    source_fps: float,
    target_fps: float,
    cap: InterpCapability,
    *,
    source_frame_count: int,
) -> FpsPlan:
    """Resolve a target frame rate against an engine's timestep capability.

    Args:
        source_fps: Source frame rate (fps).
        target_fps: Requested output frame rate (fps); must be > 0.
        cap: The engine's :class:`InterpCapability`.
        source_frame_count: Number of frames in the source clip; sets the
            arbitrary-timestep output length.

    Returns:
        An :class:`FpsPlan`.

    Raises:
        ValueError: ``target_fps`` is not positive.
    """
    if target_fps <= 0:
        raise ValueError(f"target_fps must be > 0, got {target_fps}")

    if target_fps == source_fps:
        return FpsPlan(
            schedule=None, recursion_depth=None, decimate_to=None, skip_gpu=True
        )
    if target_fps < source_fps:
        return FpsPlan(
            schedule=None,
            recursion_depth=None,
            decimate_to=target_fps,
            skip_gpu=True,
        )

    if cap is InterpCapability.RECURSIVE_2X:
        ratio = math.ceil(target_fps / source_fps)
        k = _next_pow2(ratio)
        depth = k.bit_length() - 1
        reached = source_fps * k
        decimate_to = None if reached == target_fps else target_fps
        return FpsPlan(
            schedule=None,
            recursion_depth=depth,
            decimate_to=decimate_to,
            skip_gpu=False,
        )

    # ARBITRARY_TIMESTEP: exact constant-output-rate placement.
    duration = source_frame_count / source_fps
    out_count = round(duration * target_fps)
    last_src = source_frame_count - 1
    schedule: list[tuple[int, float]] = []
    for j in range(out_count):
        pos = (j / target_fps) * source_fps  # source-frame units
        i = min(int(pos), last_src)
        f = 0.0 if i >= last_src else pos - i
        schedule.append((i, round(f, 6)))
    return FpsPlan(
        schedule=tuple(schedule),
        recursion_depth=None,
        decimate_to=None,
        skip_gpu=False,
    )
