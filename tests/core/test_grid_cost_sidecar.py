"""Per-grid cost sidecar — `<grid_out>.cost.json` per spec §6."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from kinoforge.core.grid.cost_sidecar import CostSidecarBuilder
from kinoforge.core.redaction import RedactionRegistry


def _fresh_registry() -> RedactionRegistry:
    RedactionRegistry._singleton = None
    return RedactionRegistry.instance()


def test_write_emits_documented_top_level_schema(tmp_path: Path) -> None:
    b = CostSidecarBuilder(
        grid_id="grid-1",
        spec_path=Path("/outside/spec.yaml"),
        out_mp4=Path("/outside/grid.mp4"),
        budget_cap_usd=2.0,
    )
    b.start_group(
        group_key="abc",
        pod_id="pod-1",
        provider="runpod",
        cost_per_hr=1.0,
        provision_ts=datetime(2026, 6, 26, 12, 0, 0),
    )
    b.record_cell(
        "abc",
        cell_idx=0,
        gen_wall_time_s=10.0,
        swap_wall_time_s=0.0,
        status="ok",
        mp4_sha256="abc123",
        size_bytes=12345,
    )
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 30, 0))

    data = json.loads(out.read_text())
    assert data["grid_id"] == "grid-1"
    assert data["budget_cap_usd"] == 2.0
    assert data["out_mp4"] == "/outside/grid.mp4"
    # 30 min @ $1/hr → $0.50 group cost; $0.50 grid total.
    assert data["groups"][0]["cost_per_hr_usd"] == 1.0
    assert data["groups"][0]["cost_usd"] == pytest.approx(0.5)
    assert data["total_cost_usd"] == pytest.approx(0.5)
    cell = data["groups"][0]["cells"][0]
    assert cell["idx"] == 0
    assert cell["status"] == "ok"
    assert cell["mp4_sha256"] == "abc123"
    assert cell["size_bytes"] == 12345


def test_total_cost_sums_across_groups(tmp_path: Path) -> None:
    b = CostSidecarBuilder("g", Path("/o/spec.yaml"), Path("/o/out.mp4"), 5.0)
    b.start_group("a", "p1", "runpod", 1.0, provision_ts=datetime(2026, 6, 26, 12, 0))
    b.start_group("b", "p2", "runpod", 2.0, provision_ts=datetime(2026, 6, 26, 12, 30))
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 13, 0))
    data = json.loads(out.read_text())
    assert data["groups"][0]["cost_usd"] == pytest.approx(1.0)
    assert data["groups"][1]["cost_usd"] == pytest.approx(1.0)
    assert data["total_cost_usd"] == pytest.approx(2.0)


def test_mark_cell_error_records_status_and_error(tmp_path: Path) -> None:
    b = CostSidecarBuilder("g", Path("/o/spec.yaml"), Path("/o/out.mp4"), 1.0)
    b.start_group("a", "p1", "runpod", 1.0, provision_ts=datetime(2026, 6, 26, 12, 0))
    b.mark_cell_error("a", cell_idx=2, status="budget_killed", error="cap reached")
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 0))
    cell = json.loads(out.read_text())["groups"][0]["cells"][0]
    assert cell["status"] == "budget_killed"
    assert cell["error"] == "cap reached"
    assert cell["idx"] == 2


def test_spec_path_is_redacted_via_registry(tmp_path: Path) -> None:
    reg = _fresh_registry()
    reg.add("/outside/secret/spec.yaml", kind="grid:spec_path")
    b = CostSidecarBuilder(
        "g", Path("/outside/secret/spec.yaml"), Path("/o/out.mp4"), 1.0
    )
    b.start_group("a", "p1", "runpod", 1.0, provision_ts=datetime(2026, 6, 26, 12, 0))
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 0))
    data = json.loads(out.read_text())
    assert "/outside/secret/spec.yaml" not in data["spec_path"]


def test_writes_even_with_no_groups(tmp_path: Path) -> None:
    """Sidecar must always be written, even on a pre-first-cell abort,
    so the operator's post-mortem has at least the spec_path + budget."""
    b = CostSidecarBuilder("g", Path("/o/spec.yaml"), Path("/o/out.mp4"), 1.0)
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 0))
    data = json.loads(out.read_text())
    assert data["groups"] == []
    assert data["total_cost_usd"] == 0.0
    assert data["budget_cap_usd"] == 1.0


def test_group_records_cells_in_insertion_order(tmp_path: Path) -> None:
    b = CostSidecarBuilder("g", Path("/o/spec.yaml"), Path("/o/out.mp4"), 5.0)
    b.start_group("a", "p1", "runpod", 1.0, provision_ts=datetime(2026, 6, 26, 12, 0))
    b.record_cell("a", cell_idx=3, gen_wall_time_s=1, swap_wall_time_s=0)
    b.record_cell("a", cell_idx=0, gen_wall_time_s=2, swap_wall_time_s=0)
    b.record_cell("a", cell_idx=7, gen_wall_time_s=3, swap_wall_time_s=0)
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 0))
    cells = json.loads(out.read_text())["groups"][0]["cells"]
    assert [c["idx"] for c in cells] == [3, 0, 7]


def test_group_cost_clamps_at_zero_for_inverted_clock(tmp_path: Path) -> None:
    """If `now` is earlier than provision_ts (test clock or wall-clock
    jitter), cost must not go negative — that would silently mask a
    cap-trip computation upstream."""
    b = CostSidecarBuilder("g", Path("/o/spec.yaml"), Path("/o/out.mp4"), 1.0)
    b.start_group("a", "p1", "runpod", 1.0, provision_ts=datetime(2026, 6, 26, 12, 30))
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 0))
    data = json.loads(out.read_text())
    assert data["groups"][0]["cost_usd"] == pytest.approx(0.0)
    assert data["total_cost_usd"] == pytest.approx(0.0)
