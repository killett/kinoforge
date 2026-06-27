"""End-to-end: 3-cell `lora_swap:` grid → run_grid → 3 sha-distinct mp4s + sidecar.

Drives :func:`run_grid` against a stubbed ``kinoforge generate``
subprocess (no compute, no provider). Validates the full
``lora_swap:`` pipeline shape:
- spec loads with the swap-mode cell variant
- run_grid routes the group through `_run_swap_group`
- one cold-boot (cell-1 `--emit-provision-record`) + N-1 attaches
- pod destroyed at end; residual probe clean
- 3 mp4s produced with distinct SHAs
- ``<out>.cost.json`` written with the documented schema
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
import yaml

import kinoforge.core.grid.spec as spec_mod
from kinoforge.core.grid.executor import run_grid
from kinoforge.core.grid.spec import GridSpec
from tests._smoke_harness.lora_swap_grid import write_lora_swap_grid_spec

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)


@dataclass
class _FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _make_color_mp4(out: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=64x64:r=10:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
    )


def _write_base_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "base.yaml"
    p.write_text(
        "engine:\n  kind: fake\n  precision: fp16\n"
        "prompt: hello\nmode: t2v\n"
        "models:\n  - ref: hf:org/base\n    kind: base\n    target: diffusion_models\n"
    )
    return p


def _install_fake_kinoforge_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    colors: list[str],
    pod_id: str = "fake-pod-1",
) -> list[list[str]]:
    """Replace ``subprocess.run`` so the swap-mode executor sees a
    realistic cold-boot + N-1 attach loop without touching compute.

    Only intercepts ``pixi run kinoforge ...`` commands; ffmpeg / ffprobe
    pass through unchanged so the composer can still produce a real
    grid mp4 from the per-cell color samples.
    """
    real_run = subprocess.run
    calls: list[list[str]] = []
    color_iter = iter(colors)

    def _run(cmd, **kw):
        if not (isinstance(cmd, list) and cmd[:3] == ["pixi", "run", "kinoforge"]):
            return real_run(cmd, **kw)
        calls.append(list(cmd))
        if cmd[:4] == ["pixi", "run", "kinoforge", "destroy"]:
            return _FakeProc()
        if "--emit-provision-record" in cmd:
            i = cmd.index("--emit-provision-record")
            rec = Path(cmd[i + 1])
            rec.parent.mkdir(parents=True, exist_ok=True)
            rec.write_text(
                json.dumps(
                    {
                        "pod_id": pod_id,
                        "endpoint_url": "http://pod.example",
                        "provider": "runpod",
                        "warm_attach_key": "wak",
                        "provision_ts": datetime.now()
                        .astimezone()
                        .isoformat(timespec="seconds"),
                        "cost_per_hr_usd": 0.0,
                    }
                )
            )
        if "--output-dir" in cmd:
            j = cmd.index("--output-dir")
            cell_out = Path(cmd[j + 1])
            cell_out.mkdir(parents=True, exist_ok=True)
            color = next(color_iter, "white")
            _make_color_mp4(cell_out / "out.mp4", color)
        return _FakeProc()

    monkeypatch.setattr(
        "kinoforge.core.grid.executor.subprocess.run",
        _run,
    )
    return calls


def _stub_residual_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kinoforge.core.grid.executor._check_no_residual_pods",
        lambda **_kw: (True, ""),
    )


def test_three_cell_swap_grid_runs_one_pod_writes_sidecar_and_three_distinct_mp4s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: None)
    base = _write_base_cfg(tmp_path)
    spec_path = tmp_path / "grid.yaml"
    write_lora_swap_grid_spec(
        tier="tier3",
        strengths=[0.5, 1.0, 1.5],
        out_path=spec_path,
        base_cfg_path=base,
    )

    calls = _install_fake_kinoforge_subprocess(
        monkeypatch, colors=["red", "green", "blue"]
    )
    _stub_residual_clean(monkeypatch)

    spec = GridSpec.load(spec_path)
    out_dir = tmp_path / "out"
    composed = out_dir / "grid.mp4"
    result = asyncio.run(run_grid(spec=spec, output_dir=out_dir, out_path=composed))

    # One cold-boot (--emit-provision-record), two attach (--attach-pod),
    # one destroy.
    cold_boot = [c for c in calls if "--emit-provision-record" in c]
    attach = [c for c in calls if "--attach-pod" in c]
    destroy = [c for c in calls if c[:4] == ["pixi", "run", "kinoforge", "destroy"]]
    assert len(cold_boot) == 1
    assert len(attach) == 2
    assert len(destroy) == 1

    # Three mp4 results, three distinct shas.
    shas = [r.sha256 for r in result.cell_results if r.sha256 is not None]
    assert len(shas) == 3
    assert len(set(shas)) == 3


def test_three_cell_swap_grid_emits_cost_sidecar_with_documented_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: None)
    base = _write_base_cfg(tmp_path)
    spec_path = tmp_path / "grid.yaml"
    write_lora_swap_grid_spec(
        tier="tier3",
        strengths=[0.5, 1.0, 1.5],
        out_path=spec_path,
        base_cfg_path=base,
    )

    _install_fake_kinoforge_subprocess(monkeypatch, colors=["red", "green", "blue"])
    _stub_residual_clean(monkeypatch)

    spec = GridSpec.load(spec_path)
    out_dir = tmp_path / "out"
    composed = out_dir / "grid.mp4"
    asyncio.run(run_grid(spec=spec, output_dir=out_dir, out_path=composed))

    sidecar = composed.with_suffix(".cost.json")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    # Spec §6 top-level keys.
    assert {
        "grid_id",
        "spec_path",
        "out_mp4",
        "total_cost_usd",
        "budget_cap_usd",
        "wall_time_s",
        "groups",
    } <= set(data.keys())
    assert data["budget_cap_usd"] == 0.5
    assert len(data["groups"]) == 1
    group = data["groups"][0]
    assert group["pod_id"] == "fake-pod-1"
    assert len(group["cells"]) == 3
    statuses = {c["status"] for c in group["cells"]}
    assert statuses == {"success"}


def test_swap_grid_yaml_is_loadable_without_loader_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches schema drift between the harness helper and the loader."""
    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: None)
    base = _write_base_cfg(tmp_path)
    spec_path = tmp_path / "grid.yaml"
    write_lora_swap_grid_spec(
        tier="tier4",
        strengths=[1.0, 1.5],
        out_path=spec_path,
        base_cfg_path=base,
    )

    raw = yaml.safe_load(spec_path.read_text())
    assert raw["layout"] == "1x2"
    assert raw["on_swap_failure"] == "classify"
    spec = GridSpec.load(spec_path)
    assert spec.on_swap_failure == "classify"
    assert spec.cells[0].lora_swap is not None
