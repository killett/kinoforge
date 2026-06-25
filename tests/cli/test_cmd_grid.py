"""Unit tests for the ``kinoforge grid`` CLI verb."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.cli._commands import _cmd_grid
from kinoforge.cli.context import SessionContext
from kinoforge.core.grid.executor import GridResult


@pytest.fixture
def ctx() -> SessionContext:
    return MagicMock(spec=SessionContext)


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "spec": "/tmp/outside.yaml",
        "out": None,
        "max_parallel_groups": 2,
        "dry_run": False,
        "ephemeral": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_grid_full_success_exit_0(
    monkeypatch: pytest.MonkeyPatch, ctx: SessionContext
) -> None:
    monkeypatch.setattr(
        "kinoforge.cli._commands.sys.stderr",
        MagicMock(),
    )
    fake_spec = MagicMock(cells=[MagicMock()] * 3, title="t", layout="1x3")
    fake_spec.budget_cap_usd = 1.0
    monkeypatch.setattr(
        "kinoforge.core.grid.spec.GridSpec.load",
        classmethod(lambda cls, p: fake_spec),
    )
    fake_result = GridResult(
        grid_id="g",
        status="full",
        cell_results=[],
        composed_mp4_path=Path("/tmp/grid.mp4"),
    )

    def fake_asyncio_run(coro: Any) -> Any:
        coro.close()
        return fake_result

    monkeypatch.setattr("asyncio.run", fake_asyncio_run)
    assert _cmd_grid(_args(), ctx) == 0


@pytest.mark.parametrize(
    "status,exit_code",
    [("full", 0), ("partial", 2), ("budget", 3), ("ffmpeg", 4), ("teardown", 5)],
)
def test_cmd_grid_status_to_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    ctx: SessionContext,
    status: str,
    exit_code: int,
) -> None:
    fake_spec = MagicMock(cells=[MagicMock()], title="t", layout="1x1")
    fake_spec.budget_cap_usd = 1.0
    monkeypatch.setattr(
        "kinoforge.core.grid.spec.GridSpec.load",
        classmethod(lambda cls, p: fake_spec),
    )
    fake_result = GridResult(
        grid_id="g",
        status=status,  # type: ignore[arg-type]
        cell_results=[],
        composed_mp4_path=(Path("/tmp/g.mp4") if status == "full" else None),
    )

    def fake_asyncio_run(coro: Any) -> Any:
        coro.close()
        return fake_result

    monkeypatch.setattr("asyncio.run", fake_asyncio_run)
    assert _cmd_grid(_args(), ctx) == exit_code


def test_cmd_grid_spec_validation_error_exits_1(
    monkeypatch: pytest.MonkeyPatch, ctx: SessionContext
) -> None:
    from kinoforge.core.grid.errors import GridSpecUnderRepoError

    def boom(cls: type, p: str) -> Any:
        raise GridSpecUnderRepoError("spec under repo")

    monkeypatch.setattr(
        "kinoforge.core.grid.spec.GridSpec.load",
        classmethod(boom),
    )
    assert _cmd_grid(_args(), ctx) == 1


def test_cmd_grid_dry_run_skips_compute(
    monkeypatch: pytest.MonkeyPatch,
    ctx: SessionContext,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_spec = MagicMock(cells=[MagicMock(), MagicMock()], title="t", layout="1x2")
    fake_spec.budget_cap_usd = 1.0
    monkeypatch.setattr(
        "kinoforge.core.grid.spec.GridSpec.load",
        classmethod(lambda cls, p: fake_spec),
    )
    called = {"run": False}

    def trip(coro: Any) -> None:
        coro.close()
        called["run"] = True

    monkeypatch.setattr("asyncio.run", trip)
    assert _cmd_grid(_args(dry_run=True), ctx) == 0
    assert called["run"] is False, (
        "dry-run must NOT invoke run_grid; passing this assertion means "
        "the operator can `--dry-run` a spec without spawning any pod"
    )
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()
    assert "cells" in out.lower() or "2" in out
