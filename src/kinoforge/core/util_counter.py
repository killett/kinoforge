"""Consecutive-low-util counter state machine (C26 Task 5).

Pure function — no I/O, no side effects. Called from HeartbeatLoop._tick_once
each tick. Counter resets on container restart (uptime decrease) and on
high-util reads; preserved on transport hiccup (snap=None).
"""

from __future__ import annotations

from kinoforge.core.util_endpoints import UtilSnapshot

__all__ = ["_update_counter"]


def _update_counter(
    prev_counter: int,
    *,
    prev_uptime_s: int | None,
    snap: UtilSnapshot | None,
    gpu_threshold: float,
    cpu_threshold: float,
) -> int:
    """Tick the consecutive-low-util counter.

    Semantics (per spec §6):
      - ``snap is None`` (transport hiccup): preserve ``prev_counter``.
      - Uptime decreased vs. ``prev_uptime_s``: reset to 0 (container restart).
      - gpu < gpu_threshold AND cpu < cpu_threshold (both numeric): increment.
      - Either axis ≥ threshold OR None: reset to 0.

    Args:
        prev_counter: The previous tick's counter value.
        prev_uptime_s: Container uptime from the previous tick (None if first tick).
        snap: This tick's util snapshot, or None on transport failure.
        gpu_threshold: GPU util % strictly below which counts as 'low'.
        cpu_threshold: CPU % strictly below which counts as 'low'.

    Returns:
        The new counter value.
    """
    if snap is None:
        return prev_counter
    if (
        prev_uptime_s is not None
        and snap.uptime_seconds is not None
        and snap.uptime_seconds < prev_uptime_s
    ):
        return 0
    gpu = snap.gpu_util_percent
    cpu = snap.cpu_percent
    if gpu is None or cpu is None:
        return 0
    if gpu < gpu_threshold and cpu < cpu_threshold:
        return prev_counter + 1
    return 0
