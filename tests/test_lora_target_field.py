"""Behavior: `target` generalises `branch`, and the two cannot disagree.

A misrouted H3 LoRA loads successfully and degrades output silently (the two
partitions share module names), so the routing token has to be unambiguous at
the schema edge rather than at the card.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kinoforge.core.lora import LoraEntry


def test_branch_alias_populates_target() -> None:
    """`branch="h"` yields canonical branch AND the matching target."""
    entry = LoraEntry(ref="civitai:1@2", branch="h")  # type: ignore[arg-type]
    assert entry.branch == "high_noise"
    assert entry.target == "high_noise"


def test_target_defaults_to_none_when_unspecified() -> None:
    """An undeclared target stays None so the profile's default resolves it."""
    assert LoraEntry(ref="civitai:1@2").target is None


def test_disagreeing_branch_and_target_is_refused() -> None:
    """Both set and disagreeing raises — no precedence rule silently drops one."""
    with pytest.raises(ValidationError, match="branch.*target.*disagree"):
        LoraEntry(ref="civitai:1@2", branch="high_noise", target="low_noise")


def test_agreeing_branch_and_target_is_accepted() -> None:
    """The redundant-but-consistent case is legal — it is not a conflict."""
    entry = LoraEntry(ref="civitai:1@2", branch="low_noise", target="low_noise")
    assert entry.target == "low_noise"


def test_h3_target_needs_no_branch() -> None:
    """An H3 routing token is expressible without touching Wan's vocabulary."""
    entry = LoraEntry(ref="hf:o/r:f.safetensors", target="transformer_ref")
    assert entry.target == "transformer_ref"
    assert entry.branch == "auto"
