"""Unit tests for kinoforge.core.grid.spec — pydantic schema layer.

Loader-specific tests (path guard, redaction) live in this file too but
under names containing 'load' — Task 4 ships those.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kinoforge.core.grid.spec import GenerateCell, GridSpec, PathCell

_MINIMAL_GENERATE_CELL: dict[str, Any] = {
    "generate": {
        "config": "examples/configs/wan22-14b-arcane.yaml",
        "overrides": {"loras[0].strength": 0.5},
    },
    "caption": "strength=0.5",
}


def _spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "test",
        "layout": "1x3",
        "budget_cap_usd": 1.0,
        "cells": [
            _MINIMAL_GENERATE_CELL,
            _MINIMAL_GENERATE_CELL,
            _MINIMAL_GENERATE_CELL,
        ],
    }
    base.update(overrides)
    return base


def test_minimal_spec_parses() -> None:
    spec = GridSpec.model_validate(_spec())
    assert spec.title == "test"
    assert len(spec.cells) == 3
    assert isinstance(spec.cells[0].generate, GenerateCell)


def test_path_cell_parses() -> None:
    spec = GridSpec.model_validate(
        _spec(cells=[{"path": "/tmp/a.mp4", "caption": "x"}])
    )
    assert spec.cells[0].path is not None
    # PathCell shape — top-level path field present, generate absent.
    bare_path = PathCell.model_validate({"path": "/tmp/a.mp4"})
    assert bare_path.path == Path("/tmp/a.mp4")


def test_cell_with_both_generate_and_path_rejected() -> None:
    with pytest.raises(ValidationError, match="exactly one of"):
        GridSpec.model_validate(
            _spec(cells=[{**_MINIMAL_GENERATE_CELL, "path": "/tmp/a.mp4"}])
        )


def test_cell_with_neither_generate_nor_path_rejected() -> None:
    with pytest.raises(ValidationError, match="must declare exactly one of"):
        GridSpec.model_validate(_spec(cells=[{"caption": "nothing"}]))


def test_override_value_must_be_scalar() -> None:
    bad_cell = {
        "generate": {
            "config": "x.yaml",
            "overrides": {"loras": [{"alias": "a", "strength": 1.0}]},
        },
        "caption": "x",
    }
    with pytest.raises(ValidationError, match="scalar required"):
        GridSpec.model_validate(_spec(cells=[bad_cell]))


def test_missing_budget_cap_rejected() -> None:
    raw = _spec()
    del raw["budget_cap_usd"]
    with pytest.raises(ValidationError, match="budget_cap_usd"):
        GridSpec.model_validate(raw)


def test_extra_top_level_key_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        GridSpec.model_validate(_spec(unexpected_key="hi"))


@pytest.mark.parametrize("layout", ["1x3", "2x2", "3x3", "auto", "10x10"])
def test_layout_valid(layout: str) -> None:
    GridSpec.model_validate(_spec(layout=layout))


@pytest.mark.parametrize("layout", ["banana", "1x", "x3", "0x3", "3x0", "1.5x2"])
def test_layout_invalid(layout: str) -> None:
    with pytest.raises(ValidationError, match="layout"):
        GridSpec.model_validate(_spec(layout=layout))


# ---------------------------------------------------------------------------
# Loader tests (Task 4)
# ---------------------------------------------------------------------------

import os  # noqa: E402

import yaml  # noqa: E402

from kinoforge.core.grid.errors import (  # noqa: E402
    GridSpecParseError,
    GridSpecPathError,
    GridSpecUnderRepoError,
)
from kinoforge.core.redaction import RedactionRegistry  # noqa: E402


def _write_spec_yaml(p: Path, payload: dict[str, Any]) -> Path:
    p.write_text(yaml.safe_dump(payload))
    os.chmod(p, 0o600)
    return p


def test_load_outside_repo_returns_grid_spec(tmp_path: Path) -> None:
    p = _write_spec_yaml(tmp_path / "grid.yaml", _spec())
    spec = GridSpec.load(p)
    assert len(spec.cells) == 3


def test_load_under_repo_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kinoforge.core.grid.spec as spec_mod

    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: tmp_path)
    inside = _write_spec_yaml(tmp_path / "inside.yaml", _spec())
    with pytest.raises(GridSpecUnderRepoError, match="under the active repo"):
        GridSpec.load(inside)


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(GridSpecPathError, match="not found"):
        GridSpec.load(tmp_path / "nope.yaml")


def test_load_malformed_yaml_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("loras: [\n  oops")
    with pytest.raises(GridSpecParseError):
        GridSpec.load(p)


def test_load_registers_captions_with_redaction(tmp_path: Path) -> None:
    raw = _spec(title="Confidential-Project-Title-XYZ")
    raw["cells"] = [
        {**_MINIMAL_GENERATE_CELL, "caption": "secret-lora-name strength=0.5"},
        {**_MINIMAL_GENERATE_CELL, "caption": "another-secret strength=1.0"},
        {**_MINIMAL_GENERATE_CELL, "caption": "third-secret-zzz strength=1.5"},
    ]
    p = _write_spec_yaml(tmp_path / "g.yaml", raw)
    GridSpec.load(p)
    reg = RedactionRegistry.instance()
    assert reg.redact("Confidential-Project-Title-XYZ") != (
        "Confidential-Project-Title-XYZ"
    )
    assert reg.redact("secret-lora-name strength=0.5") != (
        "secret-lora-name strength=0.5"
    )


def test_load_idempotent(tmp_path: Path) -> None:
    p = _write_spec_yaml(tmp_path / "g.yaml", _spec(title="Repeat-Idempotent-Token"))
    GridSpec.load(p)
    GridSpec.load(p)
