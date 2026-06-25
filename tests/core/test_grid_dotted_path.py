"""Unit tests for kinoforge.core.grid.dotted_path."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kinoforge.core.grid.dotted_path import set_path
from kinoforge.core.grid.errors import DottedPathError


class _Lora(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str
    strength: float = Field(ge=0.0)


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str
    loras: list[_Lora] = Field(default_factory=list)


@pytest.fixture
def cfg() -> _Cfg:
    return _Cfg(
        prompt="orig",
        loras=[
            _Lora(alias="a", strength=1.0),
            _Lora(alias="b", strength=1.0),
        ],
    )


def test_set_path_mutates_list_field(cfg: _Cfg) -> None:
    new = set_path(cfg, "loras[0].strength", 0.5)
    assert isinstance(new, _Cfg)
    assert new.loras[0].strength == 0.5
    assert new.loras[1].strength == 1.0, "neighbor leaked"


def test_set_path_mutates_scalar_field(cfg: _Cfg) -> None:
    new = set_path(cfg, "prompt", "updated")
    assert isinstance(new, _Cfg)
    assert new.prompt == "updated"


def test_set_path_index_out_of_range_raises(cfg: _Cfg) -> None:
    with pytest.raises(DottedPathError, match="index 99 out of range"):
        set_path(cfg, "loras[99].strength", 0.5)


def test_set_path_unknown_field_raises(cfg: _Cfg) -> None:
    with pytest.raises(DottedPathError, match="no field 'nope'"):
        set_path(cfg, "nope.such.field", 0.5)


def test_set_path_wildcard_rejected(cfg: _Cfg) -> None:
    with pytest.raises(DottedPathError, match="wildcards not supported"):
        set_path(cfg, "loras[*].strength", 0.5)


def test_set_path_empty_raises(cfg: _Cfg) -> None:
    with pytest.raises(DottedPathError, match="empty path"):
        set_path(cfg, "", 0.5)


def test_set_path_revalidates_after_mutation(cfg: _Cfg) -> None:
    with pytest.raises(ValidationError):
        set_path(cfg, "loras[0].strength", -0.5)
