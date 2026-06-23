"""LoraEntry.branch field — schema constraint + alias-normalize validator.

P2 §2.2 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.
Each test names the concrete bug it catches so a future refactor that
relaxes the rule fails loudly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kinoforge.core.lora import LoraEntry


def test_branch_defaults_to_auto() -> None:
    """Bug: missing default makes ``branch`` required, breaking every cfg
    and vault file that pre-dates P2 (no migration runs on those files;
    they just stop parsing)."""
    entry = LoraEntry(ref="civitai:1@1")
    assert entry.branch == "auto"


def test_branch_h_alias_normalizes_to_high_noise() -> None:
    """Bug: storing ``"h"`` as canonical leaves consumers — matcher,
    server-side router, inventory key — comparing against ``"h"`` vs
    ``"high_noise"`` and reporting false-negative matches."""
    entry = LoraEntry(ref="civitai:1@1", branch="h")  # type: ignore[arg-type]
    assert entry.branch == "high_noise"


def test_branch_l_alias_normalizes_to_low_noise() -> None:
    """Same as h alias, low-noise variant."""
    entry = LoraEntry(ref="civitai:1@1", branch="l")  # type: ignore[arg-type]
    assert entry.branch == "low_noise"


def test_branch_unknown_value_rejected() -> None:
    """Bug: a typo like ``"medium"`` silently accepted by ``extra="allow"``
    or a missing Literal constraint reaches the server-side router as an
    unknown branch and crashes with KeyError much later in
    ``_resolve_transformer`` — a TypeError at parse time is the right
    failure mode."""
    with pytest.raises(ValidationError, match="branch"):
        LoraEntry(ref="civitai:1@1", branch="medium")  # type: ignore[arg-type]


def test_branch_typo_field_name_rejected() -> None:
    """Bug: an extra-field typo (``"branche"`` instead of ``"branch"``)
    silently accepted because ``extra="allow"`` was relaxed, leading to
    runs with the default ``"auto"`` branch even though the user meant to
    pin an explicit one. The whole point of ``extra="forbid"`` on
    LoraEntry is that typos are loud."""
    with pytest.raises(ValidationError):
        LoraEntry(ref="civitai:1@1", branche="h")  # type: ignore[call-arg]


def test_branch_model_dump_returns_canonical_form() -> None:
    """Bug: alias normalization runs at parse time but ``model_dump``
    emits the alias (``"h"``) instead of the canonical form
    (``"high_noise"``), so a vault/cfg file that the user wrote with
    ``"h"`` and the system re-serialized round-trips back to ``"h"`` —
    parity with LoraTarget is then impossible (LoraTarget would only see
    ``"high_noise"`` on the wire). Storage and wire MUST share the
    canonical token."""
    entry = LoraEntry(ref="civitai:1@1", branch="h")  # type: ignore[arg-type]
    assert entry.model_dump()["branch"] == "high_noise"
