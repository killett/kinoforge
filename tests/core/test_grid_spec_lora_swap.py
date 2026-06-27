"""Unit tests for `lora_swap:` cell variant + `GridSpec.on_swap_failure`.

Models live in `kinoforge.core.grid.spec`; redaction registration runs
through `GridSpec.load`. Mirrors the shape of `test_grid_spec.py`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import kinoforge.core.grid.spec as spec_mod
from kinoforge.core.grid.errors import GridSpecParseError
from kinoforge.core.grid.spec import (
    GridSpec,
    LoraStackEntry,
    LoraSwapCell,
)
from kinoforge.core.redaction import RedactionRegistry


def _write_spec_yaml(p: Path, payload: dict[str, Any]) -> Path:
    p.write_text(yaml.safe_dump(payload))
    os.chmod(p, 0o600)
    return p


def _swap_cell(
    *,
    config: str = "/outside/base.yaml",
    stack: list[dict[str, Any]] | None = None,
    caption: str | None = None,
) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "lora_swap": {"config": config, "stack": stack if stack is not None else []},
    }
    if caption is not None:
        cell["caption"] = caption
    return cell


def _spec_with_swap_cell(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "budget_cap_usd": 1.0,
        "cells": [_swap_cell()],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# LoraStackEntry
# ---------------------------------------------------------------------------


def test_lora_stack_entry_defaults_strength_and_branch() -> None:
    e = LoraStackEntry(ref="civitai:1@2")
    assert e.strength == 1.0
    assert e.branch == "auto"


def test_lora_stack_entry_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        LoraStackEntry(ref="civitai:1@2", bogus_field="x")  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [2.5, -1.5, 100.0])
def test_lora_stack_entry_strength_out_of_range_rejected(bad: float) -> None:
    with pytest.raises(ValidationError, match="strength"):
        LoraStackEntry(ref="civitai:1@2", strength=bad)


@pytest.mark.parametrize("boundary", [-1.0, 2.0])
def test_lora_stack_entry_strength_boundary_accepted(boundary: float) -> None:
    e = LoraStackEntry(ref="civitai:1@2", strength=boundary)
    assert e.strength == boundary


def test_lora_stack_entry_branch_literal_rejects_bogus() -> None:
    with pytest.raises(ValidationError, match="branch"):
        LoraStackEntry(ref="civitai:1@2", branch="bogus")  # type: ignore[arg-type]


@pytest.mark.parametrize("branch", ["high_noise", "low_noise", "auto"])
def test_lora_stack_entry_branch_literal_accepted(branch: str) -> None:
    e = LoraStackEntry(ref="civitai:1@2", branch=branch)  # type: ignore[arg-type]
    assert e.branch == branch


# ---------------------------------------------------------------------------
# LoraSwapCell
# ---------------------------------------------------------------------------


def test_lora_swap_cell_empty_stack_legal() -> None:
    cell = LoraSwapCell(config=Path("/outside/base.yaml"), stack=[])
    assert cell.stack == []


def test_lora_swap_cell_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        LoraSwapCell(  # type: ignore[call-arg]
            config=Path("/outside/base.yaml"), stack=[], bogus="x"
        )


def test_lora_swap_cell_stack_validates_entries() -> None:
    cell = LoraSwapCell(
        config=Path("/outside/base.yaml"),
        stack=[
            LoraStackEntry(ref="civitai:1@2", strength=0.5, branch="high_noise"),
            LoraStackEntry(ref="civitai:3@4", strength=1.0, branch="low_noise"),
        ],
    )
    assert [(e.ref, e.strength, e.branch) for e in cell.stack] == [
        ("civitai:1@2", 0.5, "high_noise"),
        ("civitai:3@4", 1.0, "low_noise"),
    ]


# ---------------------------------------------------------------------------
# GridCell 3-way mutex
# ---------------------------------------------------------------------------


def test_grid_cell_lora_swap_alone_accepted() -> None:
    spec = GridSpec.model_validate(_spec_with_swap_cell())
    assert spec.cells[0].lora_swap is not None
    assert spec.cells[0].generate is None
    assert spec.cells[0].path is None


def test_grid_cell_generate_and_lora_swap_rejected() -> None:
    raw = _spec_with_swap_cell(
        cells=[
            {
                "generate": {"config": "/outside/base.yaml"},
                "lora_swap": {"config": "/outside/base.yaml", "stack": []},
            }
        ]
    )
    with pytest.raises(ValidationError, match="exactly one of"):
        GridSpec.model_validate(raw)


def test_grid_cell_path_and_lora_swap_rejected() -> None:
    raw = _spec_with_swap_cell(
        cells=[
            {
                "path": "/tmp/a.mp4",
                "lora_swap": {"config": "/outside/base.yaml", "stack": []},
            }
        ]
    )
    with pytest.raises(ValidationError, match="exactly one of"):
        GridSpec.model_validate(raw)


def test_grid_cell_all_three_variants_rejected() -> None:
    raw = _spec_with_swap_cell(
        cells=[
            {
                "generate": {"config": "/outside/base.yaml"},
                "path": "/tmp/a.mp4",
                "lora_swap": {"config": "/outside/base.yaml", "stack": []},
            }
        ]
    )
    with pytest.raises(ValidationError, match="exactly one of"):
        GridSpec.model_validate(raw)


def test_grid_cell_zero_variants_rejected() -> None:
    raw = _spec_with_swap_cell(cells=[{"caption": "nothing"}])
    with pytest.raises(ValidationError, match="exactly one of"):
        GridSpec.model_validate(raw)


# ---------------------------------------------------------------------------
# GridSpec.on_swap_failure
# ---------------------------------------------------------------------------


def test_on_swap_failure_defaults_to_classify() -> None:
    spec = GridSpec.model_validate(_spec_with_swap_cell())
    assert spec.on_swap_failure == "classify"


@pytest.mark.parametrize("policy", ["strict", "continue", "classify"])
def test_on_swap_failure_accepts_literal(policy: str) -> None:
    spec = GridSpec.model_validate(_spec_with_swap_cell(on_swap_failure=policy))
    assert spec.on_swap_failure == policy


def test_on_swap_failure_rejects_bogus_literal() -> None:
    with pytest.raises(ValidationError, match="on_swap_failure"):
        GridSpec.model_validate(_spec_with_swap_cell(on_swap_failure="bogus"))


# ---------------------------------------------------------------------------
# Redaction registration via GridSpec.load
# ---------------------------------------------------------------------------


def test_lora_swap_refs_registered_with_redaction_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reset registry so the redact() assertion below depends only on what
    # this test registers (not on prior test leakage).
    RedactionRegistry._singleton = None
    # Bypass the under-repo guard (tmp_path is under pytest's basetemp which
    # the load() guard would treat as outside-repo already, but be explicit).
    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: None)

    raw: dict[str, Any] = {
        "budget_cap_usd": 1.0,
        "cells": [
            _swap_cell(
                stack=[
                    {"ref": "civitai:42@99", "strength": 0.5, "branch": "high_noise"},
                    {"ref": "civitai:7@13", "strength": 1.0, "branch": "low_noise"},
                ]
            ),
        ],
    }
    p = _write_spec_yaml(tmp_path / "grid.yaml", raw)

    GridSpec.load(p)

    reg = RedactionRegistry.instance()
    redacted = reg.redact("log: civitai:42@99 and civitai:7@13 leaked")
    assert "civitai:42@99" not in redacted
    assert "civitai:7@13" not in redacted


def test_grid_spec_load_accepts_lora_swap_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end loader smoke: YAML → GridSpec with lora_swap cells."""
    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: None)
    raw: dict[str, Any] = {
        "title": "swap-sweep",
        "layout": "1x3",
        "budget_cap_usd": 0.5,
        "on_swap_failure": "continue",
        "cells": [
            _swap_cell(
                stack=[{"ref": "civitai:1@2", "strength": s, "branch": "auto"}],
                caption=f"strength={s}",
            )
            for s in (0.5, 1.0, 1.5)
        ],
    }
    p = _write_spec_yaml(tmp_path / "grid.yaml", raw)

    spec = GridSpec.load(p)

    assert spec.on_swap_failure == "continue"
    assert len(spec.cells) == 3
    strengths = [c.lora_swap.stack[0].strength for c in spec.cells if c.lora_swap]
    assert strengths == [0.5, 1.0, 1.5]


def test_grid_spec_load_swap_cell_extra_field_raises_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: None)
    raw: dict[str, Any] = {
        "budget_cap_usd": 1.0,
        "cells": [
            {
                "lora_swap": {
                    "config": "/outside/base.yaml",
                    "stack": [],
                    "bogus": "x",
                }
            }
        ],
    }
    p = _write_spec_yaml(tmp_path / "grid.yaml", raw)
    with pytest.raises(GridSpecParseError, match="extra|bogus"):
        GridSpec.load(p)
