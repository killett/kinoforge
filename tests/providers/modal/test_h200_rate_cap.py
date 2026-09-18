"""Behavior: an H200 config caps its rate at or above what H200 actually costs.

Before H200 entered the Modal catalog, a config asking for more than 80 GB of
VRAM got a ``CapacityError`` for free. Now it books H200 at $4.54/hr — and
because every Modal offer is ``mode="serverless"``, ``filter_offers`` SKIPS the
``max_usd_per_hr`` ceiling at selection time (``ModalProvider.capability_matrix``
marks it UNSUPPORTED, saying so in terms). The ceiling is then applied by
``_enforce_rate_cap`` AFTER launch, which destroys the instance.

So an under-capped H200 config pays for a boot it cannot keep — and for H3 that
boot is 123.8 GiB coming off a network Volume. This is the most silent way to
spend money in this repo, which is why it is frozen here rather than left to a
config comment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import load_config
from kinoforge.providers.modal._catalog import MODAL_GPU_CATALOG

_H200_RATE = next(o.cost_rate_usd_per_hr for o in MODAL_GPU_CATALOG if o.id == "H200")


def _configs_declaring_h200() -> list[Path]:
    """Return every top-level example config whose placement names H200.

    Returns:
        Matching config paths, sorted.
    """
    out: list[Path] = []
    for path in sorted(Path("examples/configs").glob("*.yaml")):
        cfg = load_config(str(path))
        if cfg.compute is None:
            continue
        if "H200" in (cfg.placement().accelerators or []):
            out.append(path)
    return out


def test_h200_is_still_priced_at_the_rate_this_guard_assumes() -> None:
    """The guard is pinned to the catalog, not to a literal.

    Bug caught: Modal reprices H200 upward, every cap in the tree is now too
    low, and a guard hardcoding 4.54 keeps passing while every H200 config
    launches-then-dies.
    """
    assert _H200_RATE == pytest.approx(4.54)


def test_every_h200_config_caps_at_or_above_the_h200_rate() -> None:
    """No shipped config can book H200 and then be reaped for being over cap.

    Bug caught: a new H200 config is copied from a Wan config and inherits
    ``max_usd_per_hr: 4.00``. It books H200 at $4.54, and ``_enforce_rate_cap``
    destroys it once the realized rate is read back — after the boot is paid
    for, and with an error that reads like a capacity problem rather than a
    one-line config problem.
    """
    caps = {
        path.name: load_config(str(path)).placement().max_usd_per_hr
        for path in _configs_declaring_h200()
    }
    assert caps, "no config declares H200 — this guard is covering nothing"
    too_low = {name: cap for name, cap in caps.items() if cap < _H200_RATE}
    assert not too_low, (
        f"these configs book H200 at ${_H200_RATE}/hr with a lower cap, so "
        f"_enforce_rate_cap destroys them after launch: {too_low}"
    )
