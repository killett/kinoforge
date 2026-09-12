"""Boot-liveness classification — decide if a booting pod is dead.

Pure decision logic (no network) plus the probe protocol. A live-but-dead
server (bootstrap crashed under its trap, or a hung download) otherwise burns
the full boot_timeout (900s); this lets wait_for_ready bail in ~2-3min.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from kinoforge.core.util_endpoints import UtilSnapshot

#: Percent-point epsilon below which a mem/disk delta counts as "flat".
_FLAT_EPS: float = 0.5

_TRAP_RE = re.compile(r"\[bootstrap-trap\]\s+rc=(\d+)")


class BootVerdict(StrEnum):
    """Verdict on a booting pod's liveness (see module docstring)."""

    ALIVE = "ALIVE"  # progressing or indeterminate-but-present → keep waiting
    GONE = "GONE"  # pod reclaimed → abort
    STALLED = "STALLED"  # provision script died / util flatline → abort
    UNKNOWN = "UNKNOWN"  # probe error → treat as ALIVE (never a false abort)


@dataclass(frozen=True)
class BootLivenessResult:
    """A boot verdict plus the updated flatline counter to carry forward."""

    verdict: BootVerdict
    consecutive_flat: int


class BootLivenessProbe(Protocol):
    """Stateful per-provision probe consulted by wait_for_ready."""

    def check(self, instance_id: str) -> BootVerdict:  # noqa: D102
        ...


def _last_trap_rc(log_tail: str | None) -> int | None:
    """Return the rc of the last ``[bootstrap-trap] rc=N`` line, or None."""
    if not log_tail:
        return None
    matches = _TRAP_RE.findall(log_tail)
    return int(matches[-1]) if matches else None


def _is_flat(snap: UtilSnapshot, prev: UtilSnapshot) -> bool:
    """True iff CPU is 0 AND mem is flat AND disk is flat/unknown."""
    if (snap.cpu_percent or 0.0) > 0.0:
        return False
    if snap.memory_percent is not None and prev.memory_percent is not None:
        if abs(snap.memory_percent - prev.memory_percent) >= _FLAT_EPS:
            return False
    if snap.disk_percent is not None and prev.disk_percent is not None:
        if abs(snap.disk_percent - prev.disk_percent) >= _FLAT_EPS:
            return False
    return True


def classify_boot_liveness(
    *,
    exists: bool,
    log_tail: str | None,
    snap: UtilSnapshot | None,
    prev_log_tail: str | None = None,
    prev_snap: UtilSnapshot | None,
    consecutive_flat: int,
    elapsed_s: float,
    grace_s: float,
    consecutive_needed: int,
) -> BootLivenessResult:
    """Decide the boot verdict from raw signals. See module docstring.

    Precedence: GONE (unambiguous) > trap-rc!=0 (ground truth) > grace window
    (suppress) > LOG PROGRESS (reset) > util flatline (counted) > util progress
    (reset) > unknown.

    Log progress sits ABOVE the flatline count because on RunPod the util
    signals cannot distinguish a model download from a corpse: an HF fetch is
    CPU-idle (network-bound) and memory-flat (it streams to disk), and
    ``disk_percent`` is always None there by documented provider limitation, so
    ``_is_flat`` degenerates to "CPU 0 and memory flat" — which a healthy
    download satisfies exactly. It sits BELOW the trap-rc check because a
    crashing provision GROWS its log at the moment it dies (the trap appends
    ``[bootstrap-trap] rc=<N>``), so growth alone must never outrank ground
    truth. Cost of not having this: pod rohjrsmre9obsp, killed 2026-09-12 while
    its log was actively fetching 19 model files.

    Args:
        exists: Whether the provider still knows the pod.
        log_tail: Tail of the pod's bootstrap.log (or None if unavailable).
        prev_log_tail: The previous probe's tail, for progress comparison.
            None on the first probe, which makes no progress claim either way.
        snap: Latest util snapshot (or None on probe error).
        prev_snap: Prior util snapshot for delta comparison (or None).
        consecutive_flat: Flatline count accumulated so far.
        elapsed_s: Seconds since boot started.
        grace_s: Grace window before flatline can count.
        consecutive_needed: Flatline count that trips STALLED.

    Returns:
        The verdict plus the updated flatline counter.
    """
    if not exists:
        return BootLivenessResult(BootVerdict.GONE, 0)

    rc = _last_trap_rc(log_tail)
    if rc is not None and rc != 0:
        return BootLivenessResult(BootVerdict.STALLED, consecutive_flat)

    if elapsed_s < grace_s:
        return BootLivenessResult(BootVerdict.ALIVE, 0)

    if snap is None:
        return BootLivenessResult(BootVerdict.UNKNOWN, consecutive_flat)

    # A log that gained bytes since the last probe is direct evidence that the
    # provision is still executing, whatever the util numbers say. Absence of
    # growth is NOT evidence of death (output buffers, quiet phases), so it only
    # falls through to the util logic rather than deciding anything itself.
    if prev_log_tail is not None and log_tail is not None and log_tail != prev_log_tail:
        return BootLivenessResult(BootVerdict.ALIVE, 0)

    if prev_snap is not None and _is_flat(snap, prev_snap):
        n = consecutive_flat + 1
        if n >= consecutive_needed:
            return BootLivenessResult(BootVerdict.STALLED, n)
        return BootLivenessResult(BootVerdict.ALIVE, n)

    return BootLivenessResult(BootVerdict.ALIVE, 0)
