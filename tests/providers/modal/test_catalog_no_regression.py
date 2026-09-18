"""Behavior: adding a catalog row must not change what a shipped config books.

``filter_offers`` keeps every offer clearing ``min_vram_gb``/``min_cuda`` and
only RANKS by ``placement.accelerators`` (U36: it is a preference ordering, not
an allowlist). The ``max_usd_per_hr`` ceiling is skipped for ``mode=
"serverless"`` offers, which every Modal offer is. So a new, larger, pricier
card becomes a silent candidate for configs that never asked for it, and the
only thing keeping it out of ``candidates[0]`` is the ranking.

The provider books ``candidates[0]`` (``providers/modal/__init__.py:262``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import load_config
from kinoforge.providers.modal._catalog import modal_offers

_MODAL_CONFIGS = sorted(Path("examples/configs").glob("modal-*.yaml"))


def test_the_shipped_modal_configs_are_actually_discovered() -> None:
    # Bug caught: a glob that matches nothing makes every parametrized test
    # below vacuously pass, so the guard silently protects nothing.
    assert len(_MODAL_CONFIGS) >= 5


@pytest.mark.parametrize("cfg_path", _MODAL_CONFIGS, ids=lambda p: p.stem)
def test_shipped_config_still_books_its_first_choice(cfg_path: Path) -> None:
    cfg = load_config(str(cfg_path))
    assert cfg.compute is not None  # every shipped Modal config has one
    accelerators = cfg.compute.placement.accelerators

    # A config with no stated preference has no protection from ranking, so a
    # new card could take candidates[0]. Fail rather than pass vacuously.
    assert accelerators, (
        f"{cfg_path.name} declares no placement.accelerators, so a newly added "
        "catalog row could silently become its booked GPU"
    )

    offers = modal_offers(cfg.placement())
    assert offers, f"{cfg_path.name} matched no Modal offer at all"
    assert offers[0].id == accelerators[0], (
        f"{cfg_path.name} would now book {offers[0].id!r}, not its first "
        f"declared choice {accelerators[0]!r}"
    )
