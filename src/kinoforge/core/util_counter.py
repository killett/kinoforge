"""Consecutive-low-util counter state machine (C26 Task 5).

Pure function — no I/O, no side effects. Called from HeartbeatLoop._tick_once
each tick. Counter resets on container restart (uptime decrease) and on
high-util reads; preserved on transport hiccup (snap=None).
"""

from __future__ import annotations

from kinoforge.core.util_endpoints import UtilSnapshot

__all__ = ["_update_counter", "_update_uptime_counter"]


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


def _update_uptime_counter(
    prev_counter: int,
    *,
    snap: UtilSnapshot | None,
    uptime_threshold_s: float,
) -> int:
    """Tick the consecutive-low-uptime counter (C27).

    Pure function. No I/O, no side effects. Called from
    HeartbeatLoop._tick_once each tick alongside _update_counter.

    Semantics (spec §6):
      - snap is None (transport hiccup): preserve prev_counter.
      - snap.uptime_seconds is None: reset to 0 (provider not surfacing).
      - snap.uptime_seconds < uptime_threshold_s: increment.
      - else: reset to 0.

    Differs from _update_counter (C26):
      - No prev_uptime_s parameter — chronic restart loop IS the signal,
        not a restart-blip the predicate is trying to filter out.
      - Single-axis read of uptime_seconds (no gpu/cpu AND-clause).
      - uptime_seconds=None resets (silence the predicate if the provider
        stops surfacing uptime mid-loop) rather than preserve.

    Args:
        prev_counter: The previous tick's counter value.
        snap: This tick's util snapshot, or None on transport failure.
        uptime_threshold_s: Strictly-< threshold below which the tick
            counts as 'low uptime'.

    Returns:
        The new counter value.
    """
    if snap is None:
        return prev_counter
    if snap.uptime_seconds is None:
        return 0
    if snap.uptime_seconds < uptime_threshold_s:
        return prev_counter + 1
    return 0
