"""C27 Phase A1 helper: FakeUtilEndpoint with a fixed UtilSnapshot.

Used by ``tests/live/test_c27_phase_a1_uptime_streak_live.py`` to drive
the C27 predicate end-to-end without depending on a real container that
genuinely restart-loops. The endpoint hands back the same snapshot on
every ``read_util`` call so the heartbeat loop observes a steady
``uptime_seconds < restart_loop_uptime_threshold_s`` and the counter
ticks up monotonically.

Lives under ``tests/live/`` (alongside the smoke that uses it) because
no production code needs it — keeping it out of ``src/`` preserves the
core-import invariant scanned by ``test_core_invariant.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from kinoforge.core.util_endpoints import UtilSnapshot


@dataclass
class FakeUtilEndpoint:
    """Return a fixed snapshot on every read.

    Satisfies the :class:`kinoforge.core.util_endpoints.UtilSnapshotEndpoint`
    Protocol structurally (no runtime ``isinstance`` check is required —
    Protocols only enforce shape).

    Attributes:
        snap: The snapshot returned by every ``read_util`` call.
    """

    snap: UtilSnapshot

    def read_util(self, instance_id: str) -> UtilSnapshot | None:
        """Return the configured snapshot regardless of ``instance_id``."""
        return self.snap
