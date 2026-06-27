"""Shared helper for tiered `lora_swap:` grid smokes (Tier-3 + Tier-4).

Companion to :mod:`tests._smoke_harness.grid` (which uses the
``generate:`` cell variant with per-cell strength overrides). This
helper uses the new ``lora_swap:`` variant so a strength sweep packs
into ONE warm-pod via server-side ``/lora/set_stack`` swaps instead
of N cold-boots.

Canonical refs:
- Tier-3 (Wan 2.1 1.3B, single LoRA, single transformer): Pokemon +
  static-rotation pair, ``branch=auto`` (no MoE).
  Source: ``examples/configs/wan21-1_3b-strength-grid.yaml``.
- Tier-4 (Wan 2.2 14B Arcane, MoE pair): high-noise + low-noise
  tensors on ``branch=high_noise`` / ``branch=low_noise``.
  Source: ``examples/configs/wan22-14b-strength-grid.yaml``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml

_TIER3_LORAS: tuple[dict[str, Any], ...] = (
    {"ref": "civitai:1479320@1673265", "branch": "auto"},
    {"ref": "civitai:1595383@1805395", "branch": "auto"},
)

_TIER4_LORAS: tuple[dict[str, Any], ...] = (
    {"ref": "civitai:2197303@2474081", "branch": "high_noise"},
    {"ref": "civitai:2197303@2474073", "branch": "low_noise"},
)

_TIER3_TITLE = "Wan 2.1 1.3B strength sweep (lora_swap)"
_TIER4_TITLE = "Wan 2.2 14B Arcane high+low strength sweep (lora_swap)"


def write_lora_swap_grid_spec(
    *,
    tier: Literal["tier3", "tier4"],
    strengths: Sequence[float],
    out_path: Path,
    base_cfg_path: Path,
    budget_cap_usd: float | None = None,
    on_swap_failure: Literal["strict", "continue", "classify"] = "classify",
) -> Path:
    """Write a grid YAML with N ``lora_swap:`` cells varying strength.

    Args:
        tier: ``tier3`` (Wan 2.1 1.3B; single-transformer; branch=auto)
            or ``tier4`` (Wan 2.2 14B Arcane; MoE pair;
            branch=high_noise+low_noise).
        strengths: One cell per value; e.g. ``[0.5, 1.0, 1.5]``.
        out_path: Where to write the spec YAML (must be OUTSIDE the
            active repo; ``GridSpec.load``'s under-repo guard rejects
            in-repo paths).
        base_cfg_path: Absolute path to the base cfg whose
            ``(base_model, engine, precision)`` derives the
            ``WarmAttachKey`` used to group cells.
        budget_cap_usd: Defaults to a tier-appropriate cap
            (``0.5`` for tier3, ``2.0`` for tier4).
        on_swap_failure: Spec-level failure policy. Default ``classify``
            (the executor's pattern-matched route).

    Returns:
        ``out_path`` (for caller convenience).

    Raises:
        ValueError: ``strengths`` empty or ``tier`` invalid.
    """
    if not strengths:
        raise ValueError("strengths must be non-empty")
    if tier == "tier3":
        template_loras = _TIER3_LORAS
        title = _TIER3_TITLE
        budget = 0.5 if budget_cap_usd is None else budget_cap_usd
    elif tier == "tier4":
        template_loras = _TIER4_LORAS
        title = _TIER4_TITLE
        budget = 2.0 if budget_cap_usd is None else budget_cap_usd
    else:
        raise ValueError(f"tier must be 'tier3' or 'tier4'; got {tier!r}")

    cells: list[dict[str, Any]] = []
    for s in strengths:
        stack = [
            {"ref": entry["ref"], "strength": s, "branch": entry["branch"]}
            for entry in template_loras
        ]
        cells.append(
            {
                "lora_swap": {"config": str(base_cfg_path), "stack": stack},
                "caption": f"strength={s:g}",
            }
        )

    spec = {
        "title": title,
        "layout": f"1x{len(strengths)}",
        "budget_cap_usd": budget,
        "on_swap_failure": on_swap_failure,
        "cells": cells,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(spec))  # kinoforge:public-write
    out_path.chmod(0o600)
    return out_path
