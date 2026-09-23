"""Behavior: `target` generalises `branch`, and the two cannot disagree.

A misrouted H3 LoRA loads successfully and degrades output silently (the two
partitions share module names), so the routing token has to be unambiguous at
the schema edge rather than at the card.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraTarget

# Synthetic ref (repo convention: deadbeef-style placeholder, never a
# real-looking key) chosen to be unmistakable if it ever leaked into a
# log line.
_TELLTALE_REF = "civitai:deadbeef00@13"


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


def test_branch_alone_warns_with_count_not_ref(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deprecation warning names a count, never the ref.

    Same posture as ``cli-loras-bypass-vault`` in
    ``tests/core/test_lora_resolver_p3.py`` and the branch-drop warning in
    ``tests/core/test_lora_url_normalize.py``: refs are SENSITIVE under
    vault mode, so a warning that used to interpolate a static count could
    silently start leaking ``self.ref`` and every currently-committed test
    would stay green. This test would catch that regression.
    """
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        entry = LoraEntry(ref=_TELLTALE_REF, branch="high_noise")
    assert entry.target == "high_noise"
    branch_warnings = [
        r for r in caplog.records if "deprecated-lora-branch" in r.message
    ]
    assert len(branch_warnings) == 1, (
        f"expected exactly one deprecation warning; got: "
        f"{[r.message for r in caplog.records]}"
    )
    assert "1 entry" in branch_warnings[0].message
    assert _TELLTALE_REF not in branch_warnings[0].message
    assert "deadbeef" not in branch_warnings[0].message


def test_branch_alone_warns_with_count_not_ref_on_lora_target(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mirror of the ``LoraEntry`` case above, on the server-side schema.

    ``LoraTarget`` carries its own copy of the validator (server has no
    ``kinoforge.core`` import), so it carries the same ref-leak risk
    independently and needs its own guard — a fix to one copy's logging
    would not protect the other.
    """
    with caplog.at_level(logging.WARNING, logger="kinoforge.diffusers.wan_t2v_server"):
        target = LoraTarget(ref=_TELLTALE_REF, branch="low_noise")
    assert target.target == "low_noise"
    branch_warnings = [
        r for r in caplog.records if "deprecated-lora-branch" in r.message
    ]
    assert len(branch_warnings) == 1, (
        f"expected exactly one deprecation warning; got: "
        f"{[r.message for r in caplog.records]}"
    )
    assert "1 entry" in branch_warnings[0].message
    assert _TELLTALE_REF not in branch_warnings[0].message
    assert "deadbeef" not in branch_warnings[0].message


def test_agreeing_branch_and_target_emits_no_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The redundant-but-consistent case does not re-warn.

    ``target`` was already set explicitly, so ``_resolve_branch_to_target``
    never takes the mapping branch that logs — only the branch-alone path
    (asserted above) does.
    """
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        entry = LoraEntry(ref=_TELLTALE_REF, branch="low_noise", target="low_noise")
    assert entry.target == "low_noise"
    branch_warnings = [
        r for r in caplog.records if "deprecated-lora-branch" in r.message
    ]
    assert not branch_warnings, (
        f"agreeing branch+target must not warn; got: "
        f"{[r.message for r in branch_warnings]}"
    )
