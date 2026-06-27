"""Sanity tests for `write_lora_swap_grid_spec`."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import kinoforge.core.grid.spec as spec_mod
from kinoforge.core.grid.spec import GridSpec
from tests._smoke_harness.lora_swap_grid import write_lora_swap_grid_spec


def _write_base_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "base.yaml"
    p.write_text(
        "engine:\n"
        "  kind: fake\n"
        "  precision: fp16\n"
        "models:\n"
        '  - ref: "hf:org/base"\n'
        "    kind: base\n"
        "    target: diffusion_models\n"
    )
    return p


def test_tier3_helper_writes_spec_loadable_via_grid_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: None)
    base = _write_base_cfg(tmp_path)
    out = tmp_path / "grid.yaml"

    write_lora_swap_grid_spec(
        tier="tier3",
        strengths=[0.5, 1.0, 1.5],
        out_path=out,
        base_cfg_path=base,
    )

    spec = GridSpec.load(out)
    assert spec.title is not None and "Wan 2.1 1.3B" in spec.title
    assert len(spec.cells) == 3
    for c in spec.cells:
        assert c.lora_swap is not None
        assert len(c.lora_swap.stack) == 2
        for entry in c.lora_swap.stack:
            assert entry.branch == "auto"
    strengths = [c.lora_swap.stack[0].strength for c in spec.cells if c.lora_swap]
    assert strengths == [0.5, 1.0, 1.5]


def test_tier4_helper_writes_high_low_branch_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: None)
    base = _write_base_cfg(tmp_path)
    out = tmp_path / "grid.yaml"

    write_lora_swap_grid_spec(
        tier="tier4",
        strengths=[1.0],
        out_path=out,
        base_cfg_path=base,
    )

    spec = GridSpec.load(out)
    assert spec.title is not None and "Arcane" in spec.title
    assert len(spec.cells) == 1
    branches = {
        entry.branch
        for entry in spec.cells[0].lora_swap.stack  # type: ignore[union-attr]
    }
    assert branches == {"high_noise", "low_noise"}


def test_helper_threads_on_swap_failure_into_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: None)
    base = _write_base_cfg(tmp_path)
    out = tmp_path / "grid.yaml"

    write_lora_swap_grid_spec(
        tier="tier3",
        strengths=[1.0],
        out_path=out,
        base_cfg_path=base,
        on_swap_failure="continue",
    )

    spec = GridSpec.load(out)
    assert spec.on_swap_failure == "continue"


def test_helper_writes_yaml_with_chmod_0o600(tmp_path: Path) -> None:
    """File permissions guard against group/world readers picking up
    civitai LoRA refs from the spec on shared dev hosts."""
    base = _write_base_cfg(tmp_path)
    out = tmp_path / "grid.yaml"
    write_lora_swap_grid_spec(
        tier="tier3",
        strengths=[1.0],
        out_path=out,
        base_cfg_path=base,
    )
    assert (out.stat().st_mode & 0o777) == 0o600


def test_helper_rejects_empty_strengths(tmp_path: Path) -> None:
    base = _write_base_cfg(tmp_path)
    out = tmp_path / "grid.yaml"
    with pytest.raises(ValueError, match="strengths must be non-empty"):
        write_lora_swap_grid_spec(
            tier="tier3",
            strengths=[],
            out_path=out,
            base_cfg_path=base,
        )


def test_helper_yaml_is_directly_parseable_for_inspection(tmp_path: Path) -> None:
    """The raw YAML must be inspectable by hand (no Python pickle / opaque
    formats); operators routinely diff specs across runs."""
    base = _write_base_cfg(tmp_path)
    out = tmp_path / "grid.yaml"
    write_lora_swap_grid_spec(
        tier="tier4",
        strengths=[0.5, 1.0],
        out_path=out,
        base_cfg_path=base,
    )
    raw = yaml.safe_load(out.read_text())
    assert raw["layout"] == "1x2"
    assert raw["budget_cap_usd"] == 2.0
    assert len(raw["cells"]) == 2
