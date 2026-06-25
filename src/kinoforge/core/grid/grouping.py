"""Group cells by capability_key for warm-reuse-aware scheduling.

A group is a set of cells that can share one warm pod (same
``CapabilityKey``). Sequential execution within a group rides the
existing matcher's warm-reuse; groups run in parallel up to the
caller's concurrency cap.

``path:`` cells are degenerate — no compute, no provisioning — and
land under one shared sentinel key so the executor's iteration loop
can dispatch them through the no-compute path uniformly.
"""

from __future__ import annotations

from typing import Protocol

_PATH_GROUP_KEY = "__path_cells__"


class _Groupable(Protocol):
    """Structural subtype the executor's ResolvedCell satisfies."""

    idx: int

    def capability_key(self) -> str | None:
        """``None`` for path: cells, hashed key for generate: cells."""
        ...


def group_cells_by_capability_key[G: _Groupable](cells: list[G]) -> dict[str, list[G]]:
    """Group ``cells`` by ``capability_key()``; preserve insertion order.

    Args:
        cells: Resolved cells in spec order.

    Returns:
        Ordered mapping from capability key (string) to the list of
        cells that share it. ``path:`` cells share the
        :data:`_PATH_GROUP_KEY` sentinel.
    """
    groups: dict[str, list[G]] = {}
    for c in cells:
        key = c.capability_key()
        if key is None:
            key = _PATH_GROUP_KEY
        groups.setdefault(key, []).append(c)
    return groups
