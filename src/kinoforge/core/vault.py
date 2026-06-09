"""Vault file loader + alias derivation + repo-root path validation.

The vault is the user's sole on-disk place where positive / negative
prompts and LoRA refs/labels appear. Loaded once at CLI entry; contents
live in process memory only. After load, tokens are registered with the
:class:`RedactionRegistry` so every downstream surface that interpolates
them gets redacted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kinoforge.core.errors import (
    VaultEmptyError,
    VaultParseError,
    VaultPathError,
    VaultUnderRepoError,
)
from kinoforge.core.redaction import RedactionRegistry

logger = logging.getLogger(__name__)


class VaultSegment(BaseModel):
    """A single segment in a multi-segment vault."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class VaultLoRA(BaseModel):
    """A LoRA reference carried by the vault."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    label: str | None = None


class Vault(BaseModel):
    """The user's private prompts + LoRA refs.

    Lives outside the repo. Tokens registered with the
    :class:`RedactionRegistry` on load.
    """

    model_config = ConfigDict(extra="forbid")

    positive_prompt: str | None = None
    segments: list[VaultSegment] | None = None
    negative_prompt: str | None = None
    loras: list[VaultLoRA] = Field(default_factory=list)
    alias: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")

    @model_validator(mode="after")
    def _exactly_one_of_prompt_or_segments(self) -> Vault:
        """Reject vaults that populate both ``positive_prompt`` and ``segments``."""
        has_prompt = (
            self.positive_prompt is not None and self.positive_prompt.strip() != ""
        )
        has_segments = self.segments is not None and len(self.segments) > 0
        if has_prompt and has_segments:
            raise ValueError(
                "vault: specify exactly one of positive_prompt or segments, not both"
            )
        return self

    def __repr__(self) -> str:
        """Render only the alias; never the prompt or LoRA bodies."""
        return f"<Vault alias={self.alias or '<unset>'}>"


def _git_repo_root() -> Path | None:
    """Return the active git repo root, or ``None`` if not inside a repo.

    Wrapped in a function so tests can monkey-patch the result.

    Returns:
        The ``Path`` returned by ``git rev-parse --show-toplevel``, or
        ``None`` when git is missing, fails, or times out.
    """
    cmd = ("git", "rev-parse", "--show-toplevel")
    try:
        out = subprocess.run(  # noqa: S603, S607
            cmd,  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def load_vault(path: Path | str) -> Vault:
    """Load a vault YAML file.

    Args:
        path: Path to the vault file.

    Returns:
        The validated :class:`Vault` model.

    Raises:
        VaultPathError: Path missing or unreadable.
        VaultUnderRepoError: Path resolves under the active git repo root.
        VaultParseError: YAML malformed or pydantic violation.
        VaultEmptyError: Neither ``positive_prompt`` nor ``segments``
            populated.
    """
    p = Path(path).resolve()
    if not p.exists() or not p.is_file():
        raise VaultPathError(f"vault file not found: {p}")
    if not os.access(p, os.R_OK):
        raise VaultPathError(f"vault file not readable: {p}")

    repo_root = _git_repo_root()
    if repo_root is not None:
        try:
            p.relative_to(repo_root)
        except ValueError:
            pass  # not under repo — fine
        else:
            raise VaultUnderRepoError(
                f"vault path is under the active repo root ({repo_root}): {p}; "
                f"move it outside the repo to avoid accidental commits"
            )

    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        logger.warning(
            "vault file %s is readable by group/other (mode %o); recommend chmod 600",
            p,
            mode,
        )

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise VaultParseError(str(p), e) from e
    if not isinstance(raw, dict):
        raise VaultParseError(
            str(p),
            TypeError(f"vault YAML root must be a mapping, got {type(raw).__name__}"),
        )

    try:
        v = Vault.model_validate(raw)
    except ValidationError as e:
        raise VaultParseError(str(p), e) from e

    has_prompt = v.positive_prompt is not None and v.positive_prompt.strip() != ""
    has_segments = v.segments is not None and len(v.segments) > 0
    if not (has_prompt or has_segments):
        raise VaultEmptyError(f"vault has neither positive_prompt nor segments: {p}")

    return v


def compute_profile_alias(config: Any, vault: Vault | None) -> str:  # noqa: ANN401
    """Compute the on-disk profile cache key.

    Args:
        config: The loaded ``Config`` (carries base model, engine kind,
            precision).
        vault: The loaded vault, or ``None`` for public-by-design runs.

    Returns:
        ``vault.alias`` when set explicitly; ``cfg-<sha256[:12]>`` when a
        vault is present without an explicit alias; the existing
        ``CapabilityKey.derive()`` hash when no vault is provided.
    """
    if vault is None:
        derived: str = config.capability_key().derive()
        return derived

    if vault.alias:
        return vault.alias

    base_ref = ""
    for entry in config.models:
        if entry.kind == "base":
            base_ref = entry.ref
            break
    material = json.dumps(
        {
            "base": base_ref,
            "loras": [lo.ref for lo in vault.loras],
            "engine": config.engine.kind,
            "precision": config.engine.precision,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "cfg-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def register_vault_tokens(vault: Vault) -> None:
    """Register every sensitive string from ``vault`` with the registry.

    Idempotent — calling twice is safe.

    Args:
        vault: The loaded vault whose prompts and LoRA refs/labels should
            be redacted on every downstream surface.
    """
    r = RedactionRegistry.instance()
    pairs: list[tuple[str, str]] = []
    if vault.positive_prompt:
        pairs.append((vault.positive_prompt, "prompt:positive"))
    if vault.negative_prompt:
        pairs.append((vault.negative_prompt, "prompt:negative"))
    if vault.segments:
        for seg in vault.segments:
            pairs.append((seg.prompt, "prompt:positive"))
    for lo in vault.loras:
        pairs.append((lo.ref, "lora:ref"))
        if lo.label:
            pairs.append((lo.label, "lora:label"))
    r.add_many(pairs)
