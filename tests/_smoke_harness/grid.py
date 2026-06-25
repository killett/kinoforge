"""Shared helper for tiered LoRA-strength-variation grid smokes.

Both Tier-3 (Wan 2.1 1.3B) and Tier-4 (Wan 2.2 14B MoE pair) use the
same shape:

  - 3 cells at strengths {low, mid, high}
  - same base cfg per cell, only the LoRA strength overrides change
  - spec lives in a tmp dir OUTSIDE the repo (`tmp_path_factory`-rooted
    paths satisfy ``GridSpec.load``'s under-repo guard)

This helper writes the spec; the smoke test owns subprocess invocation
+ teardown verification.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yaml


def write_strength_grid_spec(
    *,
    tmp_dir: Path,
    base_cfg: Path,
    strengths: Sequence[float],
    lora_indices: Sequence[int],
    budget_usd: float,
    title: str,
) -> Path:
    """Write a 1xN strength-sweep grid spec into ``tmp_dir`` and return the path.

    Args:
        tmp_dir: Directory OUTSIDE the active git repo. Use
            ``tmp_path_factory.mktemp("grid")`` in pytest fixtures.
        base_cfg: Path to the shared base cfg (a real
            ``examples/configs/*.yaml`` file).
        strengths: One float per cell — e.g. ``[0.5, 1.0, 1.5]``.
        lora_indices: Indices into the cfg's ``loras:`` list that get
            the strength override. ``[0]`` for single-LoRA stacks (Tier-3),
            ``[0, 1]`` for the Wan 2.2 MoE high+low pair (Tier-4).
        budget_usd: ``budget_cap_usd`` for the grid — fails fast over.
        title: Human-readable title; lands in the composed mp4's
            filename slug and the operator-visible summary.

    Returns:
        The absolute spec path. Pass it as ``--spec`` to
        ``kinoforge grid``.

    Raises:
        ValueError: ``strengths`` empty or ``lora_indices`` empty.
    """
    if not strengths:
        raise ValueError("strengths must be non-empty")
    if not lora_indices:
        raise ValueError("lora_indices must be non-empty")

    cells = []
    for s in strengths:
        overrides: dict[str, float] = {f"loras[{i}].strength": s for i in lora_indices}
        cells.append(
            {
                "generate": {
                    "config": str(base_cfg),
                    "overrides": overrides,
                },
                "caption": f"strength={s:g}",
            }
        )

    spec = {
        "title": title,
        "layout": f"1x{len(strengths)}",
        "budget_cap_usd": budget_usd,
        "cells": cells,
    }
    spec_path = tmp_dir / "strength_grid.yaml"
    spec_path.write_text(yaml.safe_dump(spec))  # kinoforge:public-write
    spec_path.chmod(0o600)
    return spec_path
