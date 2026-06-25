"""Grid spec pydantic models + outside-repo loader.

This module owns the spec-file surface. The loader (``GridSpec.load``)
ships in Task 4; this initial cut is models-only so downstream tests
have a parseable schema target.

The schema discipline mirrors existing kinoforge cfg models:
``extra="forbid"`` everywhere to catch typos, no implicit defaults
for cost-sensitive knobs (``budget_cap_usd`` is required).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_LAYOUT_RE = re.compile(r"^(?:[1-9]\d*x[1-9]\d*|auto)$")
_SCALAR_TYPES = (int, float, str, bool, type(None))


class CaptionStyle(BaseModel):
    """Optional per-spec caption styling."""

    model_config = ConfigDict(extra="forbid")

    position: Literal["top-center", "bottom-center", "top-left", "none"] = "top-center"
    font_size_pct: float = Field(default=5.0, gt=0, le=50)
    bg_alpha: float = Field(default=0.5, ge=0.0, le=1.0)


class GenerateCell(BaseModel):
    """Cell that orchestrates a generation."""

    model_config = ConfigDict(extra="forbid")

    config: Path
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _scalar_only(cls, v: dict[str, Any]) -> dict[str, Any]:
        for k, val in v.items():
            if not isinstance(val, _SCALAR_TYPES) or isinstance(val, (list, dict)):
                raise ValueError(
                    f"override {k!r}: scalar required (int/float/str/bool/null); "
                    f"got {type(val).__name__}. To swap a whole subtree, declare "
                    f"a separate base cfg + reference it from the cell."
                )
        return v


class PathCell(BaseModel):
    """Cell that points at a pre-existing mp4."""

    model_config = ConfigDict(extra="forbid")

    path: Path


class GridCell(BaseModel):
    """One cell in the grid; exactly one of ``generate`` or ``path`` required."""

    model_config = ConfigDict(extra="forbid")

    generate: GenerateCell | None = None
    path: Path | None = None
    caption: str | None = None

    @model_validator(mode="after")
    def _check_variant(self) -> GridCell:
        if self.generate is not None and self.path is not None:
            raise ValueError(
                "cell variants are mutually exclusive: declare `generate:` OR "
                "`path:`, not both"
            )
        if self.generate is None and self.path is None:
            raise ValueError("cell must declare exactly one of `generate:` or `path:`")
        return self


class GridSpec(BaseModel):
    """Top-level grid spec.

    The loader (``GridSpec.load``) enforces the outside-repo + redaction
    contract; this base model is the in-memory shape.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    layout: Annotated[str, Field(pattern=_LAYOUT_RE.pattern)] = "auto"
    budget_cap_usd: float = Field(gt=0)
    caption_style: CaptionStyle = Field(default_factory=CaptionStyle)
    cells: list[GridCell] = Field(min_length=1)
