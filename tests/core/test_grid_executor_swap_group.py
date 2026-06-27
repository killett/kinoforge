"""`_run_swap_group` — cold-boot cell-1, attach cells 2..N, destroy on exit."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.grid.cost_sidecar import CostSidecarBuilder
from kinoforge.core.grid.executor import _ResolvedCell, _run_swap_group


@dataclass
class _FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _build_swap_cell(idx: int, tmp_path: Path, strength: float = 1.0) -> _ResolvedCell:
    cfg = tmp_path / f"cell_{idx}.yaml"
    cfg.write_text(
        "engine:\n  kind: fake\n  precision: fp16\n"
        "prompt: hello\nmode: t2v\n"
        "models:\n  - ref: hf:org/base\n    kind: base\n    target: diffusion_models\n"
    )
    eff = MagicMock()
    eff.prompt = "hello"
    eff.mode = "t2v"
    from kinoforge.core.grid.spec import LoraStackEntry

    stack = [LoraStackEntry(ref="civitai:1@1", strength=strength, branch="auto")]
    return _ResolvedCell(
        idx=idx,
        caption=f"strength={strength}",
        cfg_path=cfg,
        effective_cfg=eff,
        mp4_path=None,
        is_lora_swap=True,
        lora_swap_stack=stack,
    )


def _new_sidecar(tmp_path: Path) -> CostSidecarBuilder:
    return CostSidecarBuilder(
        grid_id="g1",
        spec_path=tmp_path / "spec.yaml",
        out_mp4=tmp_path / "out.mp4",
        budget_cap_usd=2.0,
    )


def _fake_subprocess_factory(
    calls: list[list[str]],
    *,
    pod_id: str = "fake-pod-1",
    cell_2_returncode: int = 0,
    cell_2_stderr: str = "",
    provision_ts_back_dated: bool = False,
    cost_per_hr_usd: float = 0.0,
) -> Any:
    """Build a fake `subprocess.run` that emits provision-record + mp4.

    Defaults to a zero-cost rate so the budget cap NEVER trips during
    the argv-shape / teardown tests. Cap-trip tests override
    ``cost_per_hr_usd`` and ``provision_ts_back_dated``.
    """

    cell_n = {"i": 0}

    def _run(cmd, **_kw):
        calls.append(cmd)
        # Mimic the cold-boot side effects when --emit-provision-record fires.
        if "--emit-provision-record" in cmd:
            i = cmd.index("--emit-provision-record")
            rec = Path(cmd[i + 1])
            rec.parent.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timedelta

            if provision_ts_back_dated:
                ts = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(
                    timespec="seconds"
                )
            else:
                ts = datetime.now().astimezone().isoformat(timespec="seconds")
            rec.write_text(
                json.dumps(
                    {
                        "pod_id": pod_id,
                        "endpoint_url": "http://pod.example",
                        "provider": "runpod",
                        "warm_attach_key": "wak",
                        "provision_ts": ts,
                        "cost_per_hr_usd": cost_per_hr_usd,
                    }
                )
            )
        # Drop a fake mp4 into the cell's --output-dir.
        if "--output-dir" in cmd:
            j = cmd.index("--output-dir")
            cell_out = Path(cmd[j + 1])
            cell_out.mkdir(parents=True, exist_ok=True)
            # Unique bytes per call so sha256 differs.
            cell_n["i"] += 1
            (cell_out / f"out_{cell_n['i']}.mp4").write_bytes(b"\x00" * cell_n["i"])
        # Cell-2+: optionally force a non-zero exit code.
        if "--attach-pod" in cmd and cell_2_returncode != 0:
            return _FakeProc(returncode=cell_2_returncode, stderr=cell_2_stderr)
        return _FakeProc()

    return _run


def _stub_no_residual(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: residual probe says ledger is clean."""
    monkeypatch.setattr(
        "kinoforge.core.grid.executor._check_no_residual_pods",
        lambda **_kw: (True, ""),
    )


# ---------------------------------------------------------------------------
# Argv shape: cell-1 cold-boots, cells 2..N attach.
# ---------------------------------------------------------------------------


def test_cell_1_argv_carries_loras_and_emit_record_no_attach_no_no_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "kinoforge.core.grid.executor.subprocess.run",
        _fake_subprocess_factory(calls),
    )
    _stub_no_residual(monkeypatch)

    cells = [_build_swap_cell(0, tmp_path, 0.5)]
    asyncio.run(
        _run_swap_group(
            cells,
            on_swap_failure="classify",
            output_dir=tmp_path / "out",
            grid_id="g1",
            sidecar=_new_sidecar(tmp_path),
            budget_cap_usd=2.0,
        )
    )
    # First subprocess call is the cold-boot.
    cmd1 = calls[0]
    assert "--loras" in cmd1
    assert "--emit-provision-record" in cmd1
    assert "--attach-pod" not in cmd1
    assert "--no-reuse" not in cmd1


def test_cells_2_through_n_argv_carries_attach_pod_and_loras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "kinoforge.core.grid.executor.subprocess.run",
        _fake_subprocess_factory(calls),
    )
    _stub_no_residual(monkeypatch)

    cells = [_build_swap_cell(i, tmp_path, s) for i, s in enumerate([0.5, 1.0])]
    asyncio.run(
        _run_swap_group(
            cells,
            on_swap_failure="classify",
            output_dir=tmp_path / "out",
            grid_id="g1",
            sidecar=_new_sidecar(tmp_path),
            budget_cap_usd=2.0,
        )
    )
    # 2nd kinoforge-generate subprocess: the attach-mode call.
    attach_calls = [c for c in calls if "--attach-pod" in c]
    assert len(attach_calls) == 1
    cmd2 = attach_calls[0]
    assert "fake-pod-1" in cmd2
    assert "--loras" in cmd2
    assert "--emit-provision-record" not in cmd2
    assert "--no-reuse" not in cmd2


# ---------------------------------------------------------------------------
# Teardown in finally.
# ---------------------------------------------------------------------------


def test_destroy_runs_in_finally_on_group_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "kinoforge.core.grid.executor.subprocess.run",
        _fake_subprocess_factory(calls),
    )
    _stub_no_residual(monkeypatch)

    cells = [_build_swap_cell(i, tmp_path, s) for i, s in enumerate([0.5, 1.0])]
    asyncio.run(
        _run_swap_group(
            cells,
            on_swap_failure="classify",
            output_dir=tmp_path / "out",
            grid_id="g1",
            sidecar=_new_sidecar(tmp_path),
            budget_cap_usd=2.0,
        )
    )
    destroy_calls = [
        c for c in calls if c[:4] == ["pixi", "run", "kinoforge", "destroy"]
    ]
    assert len(destroy_calls) == 1
    assert "fake-pod-1" in destroy_calls[0]


def test_destroy_runs_in_finally_on_abort_classified_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecoverable failure mid-group must STILL destroy the pod."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "kinoforge.core.grid.executor.subprocess.run",
        _fake_subprocess_factory(
            calls,
            cell_2_returncode=1,
            cell_2_stderr="VRAMRollbackFailure: pipe corrupted",
        ),
    )
    _stub_no_residual(monkeypatch)

    cells = [_build_swap_cell(i, tmp_path, s) for i, s in enumerate([0.5, 1.0, 1.5])]
    asyncio.run(
        _run_swap_group(
            cells,
            on_swap_failure="classify",
            output_dir=tmp_path / "out",
            grid_id="g1",
            sidecar=_new_sidecar(tmp_path),
            budget_cap_usd=2.0,
        )
    )
    destroy_calls = [
        c for c in calls if c[:4] == ["pixi", "run", "kinoforge", "destroy"]
    ]
    assert len(destroy_calls) == 1


# ---------------------------------------------------------------------------
# Residual probe + budget cap-trip surface in GridCellResult.
# ---------------------------------------------------------------------------


def test_residual_probe_marks_results_with_teardown_breadcrumb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "kinoforge.core.grid.executor.subprocess.run",
        _fake_subprocess_factory(calls),
    )
    monkeypatch.setattr(
        "kinoforge.core.grid.executor._check_no_residual_pods",
        lambda **_kw: (False, "POD: leaked-pod still running"),
    )
    cells = [_build_swap_cell(0, tmp_path, 0.5)]
    results = asyncio.run(
        _run_swap_group(
            cells,
            on_swap_failure="classify",
            output_dir=tmp_path / "out",
            grid_id="g1",
            sidecar=_new_sidecar(tmp_path),
            budget_cap_usd=2.0,
        )
    )
    breadcrumbs = [r.teardown_breadcrumb for r in results]
    assert any(b is not None and "leaked-pod" in b for b in breadcrumbs)


def test_budget_cap_trip_marks_remaining_cells_budget_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "kinoforge.core.grid.executor.subprocess.run",
        _fake_subprocess_factory(
            calls,
            provision_ts_back_dated=True,
            cost_per_hr_usd=100.0,
        ),
    )
    _stub_no_residual(monkeypatch)

    sidecar = CostSidecarBuilder(
        grid_id="g1",
        spec_path=tmp_path / "spec.yaml",
        out_mp4=tmp_path / "out.mp4",
        budget_cap_usd=0.0001,  # tiny cap so back-dated rate trips it
    )
    cells = [_build_swap_cell(i, tmp_path, s) for i, s in enumerate([0.5, 1.0, 1.5])]
    results = asyncio.run(
        _run_swap_group(
            cells,
            on_swap_failure="classify",
            output_dir=tmp_path / "out",
            grid_id="g1",
            sidecar=sidecar,
            budget_cap_usd=0.0001,
        )
    )
    # Cell-0 runs (start_group's first cost evaluation is at 0); after it
    # finishes, accumulated cost trips the cap; cells 1+2 marked killed.
    assert results[0].status == "success"
    assert results[1].status == "budget_killed"
    assert results[2].status == "budget_killed"


# ---------------------------------------------------------------------------
# Sidecar interaction.
# ---------------------------------------------------------------------------


def test_swap_group_calls_start_group_on_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "kinoforge.core.grid.executor.subprocess.run",
        _fake_subprocess_factory(calls),
    )
    _stub_no_residual(monkeypatch)

    sidecar = _new_sidecar(tmp_path)
    cells = [_build_swap_cell(i, tmp_path, s) for i, s in enumerate([0.5, 1.0])]
    asyncio.run(
        _run_swap_group(
            cells,
            on_swap_failure="classify",
            output_dir=tmp_path / "out",
            grid_id="g1",
            sidecar=sidecar,
            budget_cap_usd=2.0,
        )
    )
    # Sidecar recorded one group containing two cells.
    out = tmp_path / "out.cost.json"
    sidecar.write(out)
    data = json.loads(out.read_text())
    assert len(data["groups"]) == 1
    assert data["groups"][0]["pod_id"] == "fake-pod-1"
    assert len(data["groups"][0]["cells"]) == 2


def _swap_argv_assertions() -> None:
    """Module marker so collector finds the file; tests above carry the
    actual assertions."""
