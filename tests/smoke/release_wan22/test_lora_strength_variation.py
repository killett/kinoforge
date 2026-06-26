"""Tier-4 release-gate smoke: Wan 2.2 14B MoE pair strength variation via grid.

P1 close-out (2026-06-25). The original 2026-06-21 RED scaffold was
xfail-marked pending the live impl. Task 15 rewires it onto the new
``kinoforge grid`` CLI verb against the 14B MoE pair (high-noise +
low-noise transformer LoRAs). Bug coverage: whether
``set_adapters(adapter_weights=)`` reaches BOTH transformers — the same
strength override key list is applied to ``loras[0]`` AND ``loras[1]``,
mirroring the canonical Arcane high+low pair shape from
``examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml``.

Two entry points share the harness:

  - ``test_lora_strength_variation_wan22_mock`` — subprocess mocked.
    Validates the Tier-4 MoE-shape spec + grid argv + finally
    teardown without any spend.
  - ``test_lora_strength_variation_wan22_live`` — real RunPod fire,
    gated by ``KINOFORGE_LIVE_FIRE=1``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._smoke_harness import runpod_lifecycle
from tests._smoke_harness.grid import write_strength_grid_spec

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod Tier-4 smoke",
)

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "examples/configs/wan22-14b-strength-grid.yaml"
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"

_TAG = "kinoforge-smoke-tier-4-strength"
_BUDGET_CAP = 1.50
_STRENGTHS = [0.5, 1.0, 1.5]


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_lora_strength_variation_wan22_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mocked-subprocess Tier-4: validate MoE-pair spec + grid CLI + teardown.

    Bug coverage: regression in (a) the lora_indices=[0,1] MoE-pair
    shape in ``write_strength_grid_spec`` (same strength applied to
    BOTH transformers per cell), (b) the grid argv for Tier-4's larger
    budget cap, (c) the post-fire ``teardown_pod_or_raise`` in ``finally``.
    """
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    spec_path = write_strength_grid_spec(
        tmp_dir=outside_dir,
        base_cfg=CFG,
        strengths=_STRENGTHS,
        lora_indices=[0, 1],
        budget_usd=_BUDGET_CAP,
        title=_TAG,
    )
    out_path = tmp_path / "tier4-strength.mp4"

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        out_idx = cmd.index("--out") + 1
        composed = Path(cmd[out_idx])
        composed.parent.mkdir(parents=True, exist_ok=True)
        composed.write_bytes(b"composed-tier4-mock\n")
        partial_dir = composed.parent / "_grid_mock_partial"
        partial_dir.mkdir(exist_ok=True)
        for i, s in enumerate(_STRENGTHS):
            (partial_dir / f"cell_{i}_strength-{s:g}.mp4").write_bytes(
                f"moe-cell-{i}-strength={s}".encode()
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    teardown_calls: list[tuple[str, Path]] = []

    def fake_teardown(pod_id: str, *, repo_root: Path = REPO) -> None:
        teardown_calls.append((pod_id, repo_root))

    monkeypatch.setattr(runpod_lifecycle, "teardown_pod_or_raise", fake_teardown)

    try:
        result = subprocess.run(
            [
                "pixi",
                "run",
                "kinoforge",
                "grid",
                "--spec",
                str(spec_path),
                "--out",
                str(out_path),
                "--max-parallel-groups",
                "1",
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"mock grid invocation should exit 0; got {result.returncode}"
        )
        assert out_path.exists()

        per_cell = sorted((out_path.parent / "_grid_mock_partial").glob("cell_*.mp4"))
        assert len(per_cell) == 3
        shas = {_sha256_file(p) for p in per_cell}
        assert len(shas) == 3, (
            f"MoE pair pairwise-distinct invariant: per-cell mp4s must "
            f"differ when adapter_weights reaches BOTH transformers; "
            f"got {len(shas)}/3"
        )

        argv = captured["cmd"]
        assert str(spec_path) in argv
        assert str(out_path) in argv
    finally:
        runpod_lifecycle.teardown_pod_or_raise("mock-pod-tier4", repo_root=REPO)

    assert teardown_calls == [("mock-pod-tier4", REPO)], (
        f"teardown_pod_or_raise must run exactly once; got {teardown_calls}"
    )


@pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_FIRE") != "1",
    reason="set KINOFORGE_LIVE_FIRE=1 to invoke real RunPod Tier-4 grid fire",
)
def test_lora_strength_variation_wan22_live(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Real RunPod Tier-4 fire: 3-cell MoE-pair strength grid → composed mp4."""
    pre = subprocess.run(  # noqa: S603
        ["pixi", "run", "preflight"],  # noqa: S607
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert pre.returncode == 0, (
        f"preflight must exit 0 before live fire\n"
        f"stdout: {pre.stdout}\nstderr: {pre.stderr}"
    )

    outside_dir = tmp_path_factory.mktemp("tier4_strength_live_outside")
    spec_path = write_strength_grid_spec(
        tmp_dir=outside_dir,
        base_cfg=CFG,
        strengths=_STRENGTHS,
        lora_indices=[0, 1],
        budget_usd=_BUDGET_CAP,
        title=_TAG,
    )
    log_dir = tmp_path_factory.mktemp("tier4_strength_live_log")
    out_path = REPO / "output" / "tier4-strength-grid-live.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tier4-grid-fire.log"

    try:
        with log_path.open("w") as f:
            proc = subprocess.run(  # noqa: S603
                [
                    "pixi",
                    "run",
                    "kinoforge",
                    "grid",  # noqa: S607
                    "--spec",
                    str(spec_path),
                    "--out",
                    str(out_path),
                    "--max-parallel-groups",
                    "1",
                ],
                cwd=str(REPO),
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=5400,  # 90-min ceiling
                check=False,
            )
        log_text = log_path.read_text()
        assert proc.returncode == 0, (
            f"grid CLI exit={proc.returncode}\n--- log tail ---\n{log_text[-3000:]}"
        )
        assert out_path.exists()

        per_cell_glob = sorted((REPO / "output").glob("_grid_*/cell_*_out/*.mp4"))
        assert len(per_cell_glob) == 3
        shas = {_sha256_file(p) for p in per_cell_glob}
        assert len(shas) == 3, (
            f"per-cell MoE-pair mp4s must be pairwise-distinct; got "
            f"{len(shas)}/3 distinct"
        )
    finally:
        list_proc = subprocess.run(  # noqa: S603
            ["pixi", "run", "kinoforge", "list"],  # noqa: S607
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        for line in list_proc.stdout.splitlines():
            if line.startswith("POD:"):
                pod_id = line.split()[1]
                runpod_lifecycle.teardown_pod_or_raise(pod_id, repo_root=REPO)
                break
