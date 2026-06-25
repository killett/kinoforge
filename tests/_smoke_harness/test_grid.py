"""Unit tests for the shared strength-grid smoke harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.grid.spec import GridSpec
from tests._smoke_harness.grid import write_strength_grid_spec


@pytest.fixture
def base_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "base.yaml"
    p.write_text("model: fake\nprompt: hi\nloras: []\n")
    return p


@pytest.fixture
def outside_repo_dir(tmp_path: Path) -> Path:
    return tmp_path


def test_tier3_shape_single_lora_index(outside_repo_dir: Path, base_cfg: Path) -> None:
    spec_path = write_strength_grid_spec(
        tmp_dir=outside_repo_dir,
        base_cfg=base_cfg,
        strengths=[0.5, 1.0, 1.5],
        lora_indices=[0],
        budget_usd=0.30,
        title="tier3-strength",
    )
    assert spec_path.exists()
    spec = GridSpec.load(spec_path)
    assert len(spec.cells) == 3
    for cell in spec.cells:
        assert cell.generate is not None
        keys = set(cell.generate.overrides.keys())
        assert keys == {"loras[0].strength"}, (
            f"Tier-3 single-LoRA shape: one override per cell, got {keys}"
        )


def test_tier4_shape_moe_pair_lora_indices(
    outside_repo_dir: Path, base_cfg: Path
) -> None:
    spec_path = write_strength_grid_spec(
        tmp_dir=outside_repo_dir,
        base_cfg=base_cfg,
        strengths=[0.5, 1.0, 1.5],
        lora_indices=[0, 1],
        budget_usd=1.50,
        title="tier4-moe-pair",
    )
    spec = GridSpec.load(spec_path)
    for cell in spec.cells:
        assert cell.generate is not None
        keys = set(cell.generate.overrides.keys())
        assert keys == {"loras[0].strength", "loras[1].strength"}, (
            "Tier-4 MoE shape: both transformers' LoRAs get the same "
            f"strength per cell — bug coverage is whether adapter_weights= "
            f"reaches BOTH transformers; got {keys}"
        )


def test_captions_carry_strength(outside_repo_dir: Path, base_cfg: Path) -> None:
    spec_path = write_strength_grid_spec(
        tmp_dir=outside_repo_dir,
        base_cfg=base_cfg,
        strengths=[0.0, 1.25],
        lora_indices=[0],
        budget_usd=0.1,
        title="caption-test",
    )
    spec = GridSpec.load(spec_path)
    captions = [c.caption for c in spec.cells]
    assert captions == ["strength=0", "strength=1.25"], (
        f"captions must carry the strength value so the composed grid "
        f"is self-documenting; got {captions}"
    )


def test_empty_strengths_raises(outside_repo_dir: Path, base_cfg: Path) -> None:
    with pytest.raises(ValueError, match="strengths must be non-empty"):
        write_strength_grid_spec(
            tmp_dir=outside_repo_dir,
            base_cfg=base_cfg,
            strengths=[],
            lora_indices=[0],
            budget_usd=0.3,
            title="t",
        )


def test_empty_lora_indices_raises(outside_repo_dir: Path, base_cfg: Path) -> None:
    with pytest.raises(ValueError, match="lora_indices must be non-empty"):
        write_strength_grid_spec(
            tmp_dir=outside_repo_dir,
            base_cfg=base_cfg,
            strengths=[0.5],
            lora_indices=[],
            budget_usd=0.3,
            title="t",
        )


def test_round_trip_layout_matches_strength_count(
    outside_repo_dir: Path, base_cfg: Path
) -> None:
    spec_path = write_strength_grid_spec(
        tmp_dir=outside_repo_dir,
        base_cfg=base_cfg,
        strengths=[0.5, 1.0, 1.5, 2.0],
        lora_indices=[0],
        budget_usd=0.5,
        title="four-cell",
    )
    spec = GridSpec.load(spec_path)
    assert spec.layout == "1x4"
    assert spec.budget_cap_usd == 0.5
