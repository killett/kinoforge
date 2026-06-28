"""Grid spec pydantic models + outside-repo loader.

This module owns the spec-file surface. The loader (``GridSpec.load``)
ships in Task 4; this initial cut is models-only so downstream tests
have a parseable schema target.

The schema discipline mirrors existing kinoforge cfg models:
``extra="forbid"`` everywhere to catch typos, no implicit defaults
for cost-sensitive knobs (``budget_cap_usd`` is required).
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as _ValidationError

from kinoforge.core.grid.errors import (
    GridSpecParseError,
    GridSpecPathError,
    GridSpecUnderRepoError,
)
from kinoforge.core.redaction import RedactionRegistry

_log = logging.getLogger(__name__)

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


class LoraStackEntry(BaseModel):
    """One LoRA reference in a ``lora_swap:`` cell's stack.

    Mirrors the P3 CLI ``--loras`` heredoc shape so the grid executor can
    serialize a list of these straight into the heredoc payload routed
    through ``POST /lora/set_stack``.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    strength: float = Field(default=1.0, ge=-1.0, le=2.0)
    # Canonical form matches `kinoforge.core.lora.LoraEntry.branch` so
    # the executor can serialize a stack straight into the CLI `--loras`
    # heredoc without translation. Parity is enforced by AC10's
    # exempt-tag scan + by `tests/test_lora_schema_parity.py`.
    branch: Literal["high_noise", "low_noise", "auto"] = "auto"


class LoraSwapCell(BaseModel):
    """Cell driving a server-side warm-attach LoRA-stack swap.

    Cells sharing the same ``WarmAttachKey(base, engine, precision)``
    derived from ``config`` pack into one group; the group cold-boots one
    pod for cell-1 and attaches via ``--attach-pod`` for cells 2..N. The
    ``stack`` flows through the existing P3 ``--loras`` CLI surface and
    routes to ``POST /lora/set_stack`` on the warm pod.
    """

    model_config = ConfigDict(extra="forbid")

    config: Path
    stack: list[LoraStackEntry] = Field(min_length=0)


class GridCell(BaseModel):
    """One cell in the grid; exactly one of ``generate``/``path``/``lora_swap`` required."""

    model_config = ConfigDict(extra="forbid")

    generate: GenerateCell | None = None
    path: Path | None = None
    lora_swap: LoraSwapCell | None = None
    caption: str | None = None

    @model_validator(mode="after")
    def _check_variant(self) -> GridCell:
        n_set = sum(v is not None for v in (self.generate, self.path, self.lora_swap))
        if n_set != 1:
            raise ValueError(
                "cell must declare exactly one of `generate:` / `path:` / `lora_swap:`"
            )
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
    on_swap_failure: Literal["strict", "continue", "classify"] = "classify"
    allow_in_repo: bool = False

    @classmethod
    def load(cls, path: Path | str) -> GridSpec:
        """Load a grid spec YAML; outside-repo guard + redaction registration.

        Mirrors :func:`kinoforge.core.vault.load_vault`. After successful
        validation, the spec's ``title`` plus every cell ``caption`` is
        registered with :class:`RedactionRegistry` so the same strings
        appear redacted in logs / ``successful-generations.md`` while
        still rendering plain into the output mp4 (the output dir is the
        universal exempt zone).

        Args:
            path: Path to the grid spec YAML.

        Returns:
            The validated :class:`GridSpec`.

        Raises:
            GridSpecPathError: Path missing or unreadable.
            GridSpecUnderRepoError: Path resolves under the active repo.
            GridSpecParseError: YAML or pydantic violation.
        """
        p = Path(path).resolve()
        if not p.exists() or not p.is_file():
            raise GridSpecPathError(f"grid spec not found: {p}")
        if not os.access(p, os.R_OK):
            raise GridSpecPathError(f"grid spec not readable: {p}")

        repo_root = _git_repo_root()
        if repo_root is not None:
            try:
                p.relative_to(repo_root)
            except ValueError:
                pass
            else:
                if not _yaml_opts_in_repo(p):
                    raise GridSpecUnderRepoError(
                        f"grid spec path is under the active repo root "
                        f"({repo_root}): {p}; move it outside the repo to "
                        f"avoid accidental commits (captions and overrides "
                        f"may contain LoRA refs / prompts). To intentionally "
                        f"ship an in-repo example spec built from official "
                        f"refs only, set `allow_in_repo: true` at the spec "
                        f"top level."
                    )

        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            _log.warning(
                "grid spec %s is readable by group/other (mode %o); "
                "recommend chmod 600",
                p,
                mode,
            )

        try:
            raw = yaml.safe_load(p.read_text())
        except yaml.YAMLError as e:
            raise GridSpecParseError(f"YAML parse failed for {p}: {e}") from e
        if not isinstance(raw, dict):
            raise GridSpecParseError(
                f"grid spec YAML root must be a mapping, got {type(raw).__name__}"
            )

        try:
            spec = cls.model_validate(raw)
        except _ValidationError as e:
            raise GridSpecParseError(f"grid spec schema violation in {p}: {e}") from e

        _register_caption_tokens(spec)
        return spec


def _yaml_opts_in_repo(p: Path) -> bool:
    """Peek YAML for ``allow_in_repo: true`` BEFORE pydantic parse."""
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError:
        return False
    return bool(isinstance(raw, dict) and raw.get("allow_in_repo", False))


def _git_repo_root() -> Path | None:
    """Return the active git repo root, or ``None`` if not inside a repo."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return Path(out) if out else None


def _register_caption_tokens(spec: GridSpec) -> None:
    """Register title + every caption with :class:`RedactionRegistry`."""
    reg = RedactionRegistry.instance()
    if spec.title:
        try:
            reg.add(spec.title, kind="grid:title")
        except ValueError:
            pass
    for cell in spec.cells:
        if cell.caption:
            try:
                reg.add(cell.caption, kind="grid:caption")
            except ValueError:
                pass
        if cell.lora_swap is not None:
            for entry in cell.lora_swap.stack:
                try:
                    reg.add(entry.ref, kind="grid:lora_ref")
                except ValueError:
                    pass
