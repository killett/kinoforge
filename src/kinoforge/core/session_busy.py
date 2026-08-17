"""Pure session-busy predicate shared by the warm-attach and reaper paths.

``is_session_busy`` answers one question — "is another live session
claiming this ledger row?" — from the row's own fields, with no I/O.

It was originally defined in :mod:`kinoforge.core.lifecycle` (B3), which
:mod:`kinoforge.core.reaper` may not import: ``core/reaper.py`` is held
pure by ``tests/test_core_invariant.py::test_core_reaper_module_is_pure``,
and ``lifecycle`` carries the Ledger and provider-facing machinery that
invariant exists to keep out of ``classify``. The 2026-08-16
provider-capability-declaration design (§8) needs the same predicate
inside ``classify`` as the liveness precondition on the row-7
fall-through, so it moved to this leaf module — which imports nothing
from the package — and ``lifecycle`` re-exports it for its existing
callers. Both call sites therefore share ONE notion of "busy"; a second
implementation is exactly the drift worth avoiding here.

Distinct from :mod:`kinoforge.core.session_claim`, which is the B7
cross-process *lock* helper (``provision:<id>``) and does perform I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["is_session_busy"]


def is_session_busy(
    entry: Mapping[str, Any],
    *,
    now: float,
    heartbeat_interval_s: float | None,
) -> bool:
    """Whether a ledger entry has an active in-flight session.

    B3 — cross-CLI session-busy gate. Busy iff ``session_start`` is more
    recent than ``session_end`` (or ``session_end`` absent) AND the
    heartbeat sentinel is fresh per the Layer V
    ``3 * heartbeat_interval_s`` window. Stale-busy (writer process
    crashed) auto-clears via the sentinel-freshness gate — no separate
    timeout knob.

    Args:
        entry: A ledger-shaped dict. May carry ``session_start``,
            ``session_end``, ``heartbeat_thread_tick``.
        now: Wall-clock seconds.
        heartbeat_interval_s: Cfg heartbeat cadence; ``None`` means HB
            feature disabled this invocation — fall back to trusting
            the marker (treat as busy).

    Returns:
        True iff entry should be skipped as a warm-attach candidate
        because another live session is claiming it — or, for Layer V
        ``classify``, treated as too alive to call an orphan on age
        evidence alone.
    """
    s_start = entry.get("session_start")
    s_end = entry.get("session_end")
    if s_start is None:
        return False
    if s_end is not None and float(s_end) >= float(s_start):
        return False  # cleanly closed
    if heartbeat_interval_s is None:
        return True  # no HB → trust the marker
    tick = entry.get("heartbeat_thread_tick")
    if tick is None:
        return False  # claimant never started ticking; treat as crashed
    sentinel_window = 3.0 * heartbeat_interval_s
    return (now - float(tick)) <= sentinel_window
