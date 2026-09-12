"""A grid that does not finish must say WHY, not just that it didn't.

Found live 2026-09-11 running U24's owed swap-group proof: the 1x3 LoRA-swap
grid aborted on cell 0 after 30 s and the operator's entire output was::

    [grid summary] status=partial; partial mp4s → output/_grid_..._partial

That directory was EMPTY, and the run had created no pod, so there was nothing
anywhere to read. The cause was captured — ``_run_swap_cell`` builds a
``GridCellFailure`` holding the cell's stderr tail precisely for this — and then
never printed. A live run that produces no diagnostic is a live run spent twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.grid.errors import GridCellFailure
from kinoforge.core.grid.executor import GridCellResult, GridResult


def _failed_cell(idx: int, msg: str) -> GridCellResult:
    """Build a failed cell result carrying *msg* as its captured cause.

    Args:
        idx: Cell index.
        msg: The stderr tail the executor would have captured.

    Returns:
        A ``GridCellResult`` shaped like the executor's own failure path.
    """
    return GridCellResult(
        idx=idx,
        caption=f"cell {idx}",
        status="failed",
        mp4_path=None,
        sha256=None,
        cost_usd=None,
        error=GridCellFailure(
            idx=idx, cfg_repr=f"cfg=cell{idx}.yaml", exception_chain=RuntimeError(msg)
        ),
    )


def _run_summary(result: GridResult, capsys: pytest.CaptureFixture[str]) -> str:
    """Render *result* through the real CLI summary and return stderr.

    Args:
        result: The grid result to render.
        capsys: pytest's capture fixture.

    Returns:
        Everything the command wrote to stderr.
    """
    from kinoforge.cli import _commands

    _commands._report_grid_result(result)
    return capsys.readouterr().err


class TestGridSummaryReportsFailures:
    """A non-full grid must name each failed cell and its cause."""

    def test_failed_cell_cause_reaches_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The captured stderr tail is printed, not swallowed.

        Catches exactly what shipped: a summary that prints only ``status=`` and
        a partial directory, leaving the operator to re-run live — on real
        money — to discover a cause the process already had in hand.
        """
        result = GridResult(
            grid_id="g1",
            status="partial",
            cell_results=[_failed_cell(0, "civitai 401: token rejected")],
            partial_dir=Path("/tmp/partial"),
        )

        err = _run_summary(result, capsys)

        assert "civitai 401: token rejected" in err, err
        assert "cell 0" in err.lower(), err

    def test_every_failed_cell_is_reported_not_only_the_first(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """All failures appear, so an abort does not hide its siblings.

        Catches reporting ``failures[0]`` alone. A swap group aborts the
        remaining cells as a unit, so the second and third causes are how you
        tell "one bad LoRA ref" from "the whole config is wrong".
        """
        result = GridResult(
            grid_id="g1",
            status="partial",
            cell_results=[
                _failed_cell(0, "first cause AAA"),
                _failed_cell(1, "second cause BBB"),
                _failed_cell(2, "third cause CCC"),
            ],
            partial_dir=Path("/tmp/partial"),
        )

        err = _run_summary(result, capsys)

        for needle in ("AAA", "BBB", "CCC"):
            assert needle in err, f"{needle} missing from: {err}"

    def test_successful_grid_prints_no_failure_block(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """A full grid stays quiet about failures.

        Negative control: catches printing a failures header unconditionally,
        which would put an empty, alarming section under every clean run.
        """
        mp4 = tmp_path / "grid.mp4"
        mp4.write_bytes(b"x")
        result = GridResult(
            grid_id="g1",
            status="full",
            cell_results=[
                GridCellResult(
                    idx=0,
                    caption="ok",
                    status="success",
                    mp4_path=mp4,
                    sha256="deadbeef",
                    cost_usd=0.01,
                )
            ],
            composed_mp4_path=mp4,
        )

        capsys.readouterr()  # clear anything already buffered
        _run_summary(result, capsys)
        combined = capsys.readouterr()
        assert "failed cell" not in (combined.err + combined.out).lower(), combined

    def test_failure_without_a_captured_cause_still_names_the_cell(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed cell with no error object is still listed.

        Catches an implementation that iterates only cells whose ``error`` is
        set: a cell can be marked failed by a path that never attached one, and
        silently dropping it from the report reproduces the original bug for
        that case.
        """
        result = GridResult(
            grid_id="g1",
            status="partial",
            cell_results=[
                GridCellResult(
                    idx=7,
                    caption="no-error-object",
                    status="failed",
                    mp4_path=None,
                    sha256=None,
                    cost_usd=None,
                    error=None,
                )
            ],
            partial_dir=Path("/tmp/partial"),
        )

        err = _run_summary(result, capsys)

        assert "7" in err, err
