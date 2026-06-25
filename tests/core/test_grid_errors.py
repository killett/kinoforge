"""Unit tests for kinoforge.core.grid.errors."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import KinoforgeError
from kinoforge.core.grid.errors import (
    DottedPathError,
    FfmpegInvocationError,
    FfmpegNotFoundError,
    GridBudgetExceeded,
    GridCellFailure,
    GridCellPathMissing,
    GridSpecUnderRepoError,
)


@pytest.mark.parametrize(
    "cls",
    [
        GridSpecUnderRepoError,
        GridCellPathMissing,
        GridCellFailure,
        GridBudgetExceeded,
        FfmpegInvocationError,
        FfmpegNotFoundError,
        DottedPathError,
    ],
)
def test_error_subclasses_kinoforge_error(cls: type[Exception]) -> None:
    assert issubclass(cls, KinoforgeError), (
        f"{cls.__name__} must subclass KinoforgeError so existing "
        f"broad-except sites in cli/_commands catch grid errors uniformly"
    )


def test_grid_cell_failure_stores_breadcrumb() -> None:
    inner = RuntimeError("boom")
    err = GridCellFailure(
        idx=2, cfg_repr="cfg=wan22-arcane.yaml", exception_chain=inner
    )
    assert err.idx == 2
    assert err.cfg_repr == "cfg=wan22-arcane.yaml"
    assert err.exception_chain is inner
    assert "cell 2" in str(err)
    assert "boom" in str(err)
