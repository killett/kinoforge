"""Unit tests for kinoforge.core.grid.executor.

Subprocess interactions mocked end-to-end — no real ``kinoforge generate``
or ``kinoforge list`` invoked. Live coverage lives in the smoke tests.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kinoforge.core.grid.executor import run_grid
from kinoforge.core.grid.spec import GridSpec


class _SubprocessLog:
    """Captured calls for assertion (avoids union-dict mypy noise)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.list_calls: int = 0


def _stub_generate_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failures: dict[int, str] | None = None,
    list_stdout: str = (
        "[instance overview] No running instances.\nNo instances recorded in ledger."
    ),
) -> _SubprocessLog:
    """Stub subprocess.run for `kinoforge generate` and `kinoforge list`."""
    log = _SubprocessLog()
    failures = failures or {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "list" in cmd:
            log.list_calls += 1
            return subprocess.CompletedProcess(cmd, 0, stdout=list_stdout, stderr="")
        log.calls.append(cmd)
        rid = cmd[cmd.index("--run-id") + 1]
        cell_idx = int(rid.split("__cell")[1])
        if cell_idx in failures:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr=failures[cell_idx]
            )
        out_dir = Path(cmd[cmd.index("--output-dir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{rid}.mp4").write_bytes(b"\x00" * 1024 + str(cell_idx).encode())
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("kinoforge.core.grid.executor.subprocess.run", fake_run)
    return log


def _make_spec(tmp_path: Path, n_cells: int = 3) -> GridSpec:
    cfg = tmp_path / "base.yaml"
    cfg.write_text("model: fake\nprompt: hi\nloras: []\n")
    raw = {
        "title": "test-grid-spec",
        "layout": f"1x{n_cells}",
        "budget_cap_usd": 1.0,
        "cells": [
            {
                "generate": {"config": str(cfg), "overrides": {}},
                "caption": f"cell={i}",
            }
            for i in range(n_cells)
        ],
    }
    return GridSpec.model_validate(raw)


def _stub_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `_check_ffmpeg`/`probe_inputs`/`compose_grid_mp4` no-ops."""
    monkeypatch.setattr("kinoforge.core.grid.executor._check_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "kinoforge.core.grid.executor.probe_inputs",
        lambda paths: [
            MagicMock(width=512, height=512, fps=16.0, duration=2.0) for _ in paths
        ],
    )
    monkeypatch.setattr(
        "kinoforge.core.grid.executor.compose_grid_mp4",
        lambda **kw: kw["out_path"].write_bytes(b"composed"),
    )


def test_run_grid_all_success_composes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = _stub_generate_subprocess(monkeypatch)
    _stub_compose(monkeypatch)
    monkeypatch.setattr(
        "kinoforge.core.grid.executor._cell_capability_key",
        lambda cell: "K-same",
    )
    spec = _make_spec(tmp_path)

    result = asyncio.run(
        run_grid(spec=spec, output_dir=tmp_path / "out", max_parallel_groups=2)
    )
    assert result.status == "full", f"expected status='full', got {result.status!r}"
    assert result.composed_mp4_path is not None
    assert result.composed_mp4_path.exists()
    no_reuse_count = sum("--no-reuse" in cmd for cmd in log.calls)
    assert no_reuse_count == 1, (
        f"warm-reuse: exactly 1 --no-reuse on last cell of group, got "
        f"{no_reuse_count} across {len(log.calls)} cells"
    )
    assert "--no-reuse" in log.calls[-1]


def test_run_grid_one_cell_fails_aborts_group_other_groups_continue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_generate_subprocess(monkeypatch, failures={1: "engine boom"})
    _stub_compose(monkeypatch)

    keymap = {0: "K-a", 1: "K-a", 2: "K-a", 3: "K-b"}
    monkeypatch.setattr(
        "kinoforge.core.grid.executor._cell_capability_key",
        lambda cell: keymap[cell.idx],
    )
    spec = _make_spec(tmp_path, n_cells=4)
    result = asyncio.run(
        run_grid(spec=spec, output_dir=tmp_path / "out", max_parallel_groups=2)
    )

    assert result.status == "partial", (
        f"one cell failure → partial, got {result.status!r}"
    )
    statuses = {r.idx: r.status for r in result.cell_results}
    assert statuses[0] == "success"
    assert statuses[1] == "failed"
    assert statuses[2] == "aborted", "sibling in same group as failing cell must abort"
    assert statuses[3] == "success", "cell 3 in other group keeps going"
    assert result.partial_dir is not None
    assert (result.partial_dir / "cell_0_cell-0.mp4").exists()
    assert (result.partial_dir / "cell_3_cell-3.mp4").exists()
    assert result.composed_mp4_path is None


def test_run_grid_residual_pod_after_groups_yields_teardown_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_generate_subprocess(
        monkeypatch,
        list_stdout="POD: 2k0gonzmeqw7xj running A100-SXM4-80GB",
    )
    _stub_compose(monkeypatch)
    monkeypatch.setattr(
        "kinoforge.core.grid.executor._cell_capability_key", lambda cell: "K"
    )
    spec = _make_spec(tmp_path)
    result = asyncio.run(
        run_grid(spec=spec, output_dir=tmp_path / "out", max_parallel_groups=2)
    )
    assert result.status == "teardown"
    assert "2k0gonzmeqw7xj" in (result.teardown_breadcrumb or "")
