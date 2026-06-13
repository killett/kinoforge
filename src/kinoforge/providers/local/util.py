"""LocalProvider util-snapshot test seam (C26 Task 4).

Programmable scripted-snapshot endpoint for HeartbeatLoop integration
tests and for the 'local' provider lifecycle path (so
provider_util_supported('local') returns True without a real wire path).
"""

from __future__ import annotations

from collections.abc import Sequence

from kinoforge.core.util_endpoints import UtilSnapshot

__all__ = ["LocalUtilEndpoint"]


class LocalUtilEndpoint:
    """Returns snapshots from a programmable script in order.

    Attributes:
        _script: Snapshots to return in order. None entries permitted
            (mimic 'instance gone' or 'data unavailable'). Exhausting the
            script returns None on subsequent calls.
        _cursor: Position into ``_script``.
    """

    def __init__(self, *, script: Sequence[UtilSnapshot | None] | None = None) -> None:
        """Build the endpoint.

        Args:
            script: Snapshots to return in order; None permitted; exhausting
                the script returns None.
        """
        self._script = list(script) if script else []
        self._cursor = 0

    def read_util(self, instance_id: str) -> UtilSnapshot | None:
        """Return the next scripted snapshot, or None if exhausted."""
        if self._cursor >= len(self._script):
            return None
        snap = self._script[self._cursor]
        self._cursor += 1
        return snap
