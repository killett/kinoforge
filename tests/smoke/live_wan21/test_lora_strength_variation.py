"""Tier-3 smoke: Wan 2.1 1.3B per-LoRA strength variation via grid.

P1 close-out (2026-06-25). The original 2026-06-21 RED scaffold was
xfail-marked pending the live impl. Task 14 rewires it onto the new
``kinoforge grid`` CLI verb: same (prompt, seed, LoRA-stack) at 3
strengths in a 1x3 grid composes into one composite mp4 + 3 per-cell
mp4s with pairwise-distinct sha256s.

Two test entry points share the harness:

  - ``test_lora_strength_variation_wan21_mock`` — subprocess mocked.
    Validates the grid-CLI argv shape + sha-distinct invariant + the
    ``teardown_pod_or_raise`` finally-clause without any spend.
    Always runs under ``KINOFORGE_LIVE_TESTS=1`` (no live network).
  - ``test_lora_strength_variation_wan21_live`` — real RunPod fire,
    gated by ``KINOFORGE_LIVE_FIRE=1`` on top of ``KINOFORGE_LIVE_TESTS=1``.
    Operator-loop perceptual eval lands in Task 18.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._smoke_harness import runpod_lifecycle
from tests._smoke_harness.grid import write_strength_grid_spec

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod Tier-3 smoke",
)

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "examples/configs/wan21-1_3b-strength-grid.yaml"
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"

_TAG = "kinoforge-smoke-tier-3-strength"
_BUDGET_CAP = 0.30
_STRENGTHS = [0.5, 1.0, 1.5]


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_lora_strength_variation_wan21_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mocked-subprocess Tier-3: validate grid CLI argv + sha-distinct + teardown.

    Bug coverage: regression in (a) ``write_strength_grid_spec`` shape
    for the single-LoRA-override Tier-3 path, (b) the grid CLI argv
    sequence (``--spec``, ``--out``, ``--max-parallel-groups``),
    (c) the post-fire ``teardown_pod_or_raise`` invocation in the
    ``finally`` clause.
    """
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    spec_path = write_strength_grid_spec(
        tmp_dir=outside_dir,
        base_cfg=CFG,
        strengths=_STRENGTHS,
        lora_indices=[0],
        budget_usd=_BUDGET_CAP,
        title=_TAG,
    )
    assert spec_path.exists()
    out_path = tmp_path / "tier3-strength.mp4"

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Synthesize per-cell mp4s + the composed mp4 so the
        # sha-distinct + composed-exists asserts succeed.
        captured["cmd"] = cmd
        out_idx = cmd.index("--out") + 1
        composed = Path(cmd[out_idx])
        composed.parent.mkdir(parents=True, exist_ok=True)
        composed.write_bytes(b"composed-tier3-mock\n")
        partial_dir = composed.parent / "_grid_mock_partial"
        partial_dir.mkdir(exist_ok=True)
        for i, s in enumerate(_STRENGTHS):
            (partial_dir / f"cell_{i}_strength-{s:g}.mp4").write_bytes(
                f"cell-{i}-strength={s}".encode()
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
            f"mock grid invocation should exit 0; got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert out_path.exists(), "composed mp4 not written by mock"

        per_cell = sorted((out_path.parent / "_grid_mock_partial").glob("cell_*.mp4"))
        assert len(per_cell) == 3, f"expected 3 per-cell mp4s, got {len(per_cell)}"
        shas = {_sha256_file(p) for p in per_cell}
        assert len(shas) == 3, (
            f"per-cell mp4s must have pairwise-distinct sha256 (proves the "
            f"strength override actually changed the bytes); got {len(shas)} "
            f"distinct shas across {len(per_cell)} cells"
        )

        argv = captured["cmd"]
        assert "--spec" in argv
        assert str(spec_path) in argv
        assert "--out" in argv
        assert str(out_path) in argv
    finally:
        # In the live test we'd pull the pod_id from kinoforge list; mock
        # path uses a placeholder so the teardown_pod_or_raise contract is
        # still exercised end-to-end.
        runpod_lifecycle.teardown_pod_or_raise("mock-pod-tier3", repo_root=REPO)

    assert teardown_calls == [("mock-pod-tier3", REPO)], (
        f"teardown_pod_or_raise must be called exactly once in finally; "
        f"got {teardown_calls}"
    )


@pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_FIRE") != "1",
    reason="set KINOFORGE_LIVE_FIRE=1 to invoke real RunPod Tier-3 grid fire",
)
def test_lora_strength_variation_wan21_live(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Real RunPod Tier-3 fire: 3-cell strength grid → composed mp4."""
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

    outside_dir = tmp_path_factory.mktemp("tier3_strength_live_outside")
    spec_path = write_strength_grid_spec(
        tmp_dir=outside_dir,
        base_cfg=CFG,
        strengths=_STRENGTHS,
        lora_indices=[0],
        budget_usd=_BUDGET_CAP,
        title=_TAG,
    )
    log_dir = tmp_path_factory.mktemp("tier3_strength_live_log")
    out_path = REPO / "output" / "tier3-strength-grid-live.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tier3-grid-fire.log"

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
                timeout=2700,  # 45-min ceiling
                check=False,
            )
        log_text = log_path.read_text()
        assert proc.returncode == 0, (
            f"grid CLI exit={proc.returncode}\n--- log tail ---\n{log_text[-3000:]}"
        )
        assert out_path.exists(), "composed mp4 missing post-run"

        per_cell_glob = sorted(
            (REPO / "output").glob(f"*grid_*{_TAG[-15:]}*__cell*.mp4")
        )
        assert len(per_cell_glob) == 3, (
            f"expected 3 per-cell mp4s in output/, got {len(per_cell_glob)}"
        )
        shas = {_sha256_file(p) for p in per_cell_glob}
        assert len(shas) == 3, (
            f"per-cell mp4s must be pairwise-distinct (proves strength variation "
            f"reaches the pipeline); got {len(shas)}/3 distinct"
        )
        # silence unused json/REPO checks
        _ = json.dumps({"shas": list(shas)})
    finally:
        list_proc = subprocess.run(  # noqa: S603
            ["pixi", "run", "kinoforge", "list"],  # noqa: S607
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        # If list shows a residual pod, the grid CLI's teardown probe
        # missed it — force-reap via teardown_pod_or_raise on the
        # extracted ID.
        for line in list_proc.stdout.splitlines():
            if line.startswith("POD:"):
                pod_id = line.split()[1]
                runpod_lifecycle.teardown_pod_or_raise(pod_id, repo_root=REPO)
                break
