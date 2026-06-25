"""Unit tests for kinoforge.core.grid.grouping."""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.grid.grouping import _PATH_GROUP_KEY, group_cells_by_capability_key


class _FakeResolvedCell:
    """Minimal stand-in: real cells carry a cfg + caption + idx."""

    def __init__(
        self, idx: int, *, cap_key: str | None, mp4_path: Path | None = None
    ) -> None:
        self.idx = idx
        self._cap_key = cap_key
        self.mp4_path = mp4_path

    def capability_key(self) -> str | None:
        return self._cap_key


def test_strength_sweep_collapses_to_one_group() -> None:
    cells = [
        _FakeResolvedCell(0, cap_key="K-arcane"),
        _FakeResolvedCell(1, cap_key="K-arcane"),
        _FakeResolvedCell(2, cap_key="K-arcane"),
    ]
    groups = group_cells_by_capability_key(cells)
    assert list(groups.keys()) == ["K-arcane"]
    assert [c.idx for c in groups["K-arcane"]] == [0, 1, 2]


def test_distinct_configs_form_distinct_groups() -> None:
    cells = [
        _FakeResolvedCell(0, cap_key="K-wan21"),
        _FakeResolvedCell(1, cap_key="K-wan22"),
        _FakeResolvedCell(2, cap_key="K-flux"),
    ]
    groups = group_cells_by_capability_key(cells)
    assert list(groups.keys()) == ["K-wan21", "K-wan22", "K-flux"]
    for k in groups:
        assert len(groups[k]) == 1


def test_path_cells_form_degenerate_group() -> None:
    cells = [
        _FakeResolvedCell(0, cap_key="K-arcane"),
        _FakeResolvedCell(1, cap_key="K-arcane"),
        _FakeResolvedCell(2, cap_key=None, mp4_path=Path("/tmp/x.mp4")),
    ]
    groups = group_cells_by_capability_key(cells)
    assert set(groups) == {"K-arcane", _PATH_GROUP_KEY}
    assert [c.idx for c in groups["K-arcane"]] == [0, 1]
    assert [c.idx for c in groups[_PATH_GROUP_KEY]] == [2]


def test_empty_input() -> None:
    assert group_cells_by_capability_key([]) == {}


def test_insertion_order_preserved() -> None:
    cells = [
        _FakeResolvedCell(0, cap_key="K-b"),
        _FakeResolvedCell(1, cap_key="K-a"),
        _FakeResolvedCell(2, cap_key="K-b"),
    ]
    groups = group_cells_by_capability_key(cells)
    assert list(groups.keys()) == ["K-b", "K-a"], (
        "iteration order must follow first-occurrence to keep group "
        "scheduling deterministic across runs"
    )
