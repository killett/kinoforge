"""Behavior: a Wan MoE `target:` actually routes, instead of costing a boot.

U60. ``core/lora_profiles`` registers ``wan_t2v_server`` with
``target_universe=("high_noise", "low_noise")``, so ``LoraServerSupportCheck``
ACCEPTS ``target: high_noise`` on a Wan config. It does not route.
``_resolve_branch_to_target`` mapped ``branch`` -> ``target`` one way only, so a
``target``-only entry still carried ``branch="auto"``;
``DiffusersBackend.set_lora_stack`` shipped ``{"branch": "auto", "target":
"high_noise"}``; and ``wan_t2v_server``'s ``/lora/set_stack`` gates and routes
on ``branch``, never on ``target``. On a MoE pipeline ``branch="auto"`` is
illegal, so the swap was refused with ``BranchAutoNotAllowedOnMoE`` -> HTTP 400
**after** the Wan 2.2 boot and its ~70 GB weight fetch. The config-load check
built to prevent that class of failure was causing this one.

Both sides map, and the filing is emphatic that this is not optional on either.
The schema-parity test compares only ``.target``, so it will NOT demand the
client-side half — but a server-only map leaves ``LoraEntry.branch == "auto"``
client-side while ``warm_reuse/matcher.py`` compares ``.branch`` against the
pod's inventory row. That is U55's mechanism: every warm Wan attach would
re-swap its entire stack on every run.

The map is deliberately narrow. It fires only when ``branch`` is still at its
``"auto"`` default AND ``target`` names a member of Wan's MoE universe, so a
non-Wan routing token (H3's ``transformer``) never lands in a field whose
vocabulary excludes it — which is U55 in the other direction.
"""

from __future__ import annotations

import pytest

from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers.servers.wan_t2v_server import (
    LORA_TARGET_UNIVERSE,
    LoraTarget,
)

_REF = "civitai:1234@5678"

# Both classes are schema-equivalent by contract (tests/test_lora_schema_parity.py
# locks it). Parametrising over both is what stops the two halves diverging —
# the filing's specific warning.
_CLASSES = [LoraEntry, LoraTarget]
_IDS = ["core.LoraEntry", "server.LoraTarget"]


@pytest.mark.parametrize("cls", _CLASSES, ids=_IDS)
@pytest.mark.parametrize("token", ["high_noise", "low_noise"])
def test_a_wan_moe_target_populates_branch(cls: type, token: str) -> None:
    """The defect. A `target:`-only Wan entry must carry a routable branch.

    Bug caught: the one-way map. `branch` stays `"auto"`, the pod's
    `_check_branch_legal` rejects `auto` on a MoE pipeline, and the operator
    learns this from an HTTP 400 that arrives 25-30 minutes and one ~70 GB
    weight fetch after they started the run.
    """
    entry = cls(ref=_REF, target=token)

    assert entry.branch == token


@pytest.mark.parametrize("cls", _CLASSES, ids=_IDS)
def test_a_non_wan_target_leaves_branch_alone(cls: type) -> None:
    """The map must not put H3's vocabulary into Wan's MoE field.

    Bug caught: mapping unconditionally. `branch`'s vocabulary is Wan's MoE
    tokens; writing `"transformer"` into it is U55 in the other direction —
    the exact defect just fixed by making H3 stop doing this.
    """
    entry = cls(ref=_REF, target="transformer")

    assert entry.branch == "auto"
    assert entry.target == "transformer"


@pytest.mark.parametrize("cls", _CLASSES, ids=_IDS)
def test_an_explicit_branch_still_maps_forward_to_target(cls: type) -> None:
    """Negative control — the pre-existing direction must keep working.

    Bug caught: replacing the forward map instead of adding the reverse.
    Every Wan 2.2 config in the repo spells its routing as `branch:` today,
    on the advice `docs/breaking-changes.md` gives precisely because of U60.
    """
    entry = cls(ref=_REF, branch="high_noise")

    assert entry.target == "high_noise"
    assert entry.branch == "high_noise"


@pytest.mark.parametrize("cls", _CLASSES, ids=_IDS)
def test_a_disagreeing_pair_is_still_a_hard_error(cls: type) -> None:
    """Negative control — the reverse map must not paper over a conflict.

    Bug caught: letting the new branch assignment run before the
    disagreement check, which would silently pick a winner between two
    routings the operator explicitly wrote differently.
    """
    with pytest.raises(ValueError, match="disagree"):
        cls(ref=_REF, branch="high_noise", target="low_noise")


@pytest.mark.parametrize("cls", _CLASSES, ids=_IDS)
def test_an_entry_with_neither_stays_auto(cls: type) -> None:
    """Negative control — "unspecified" must survive as unspecified.

    Bug caught: defaulting `branch` to a concrete MoE token. On a
    single-transformer pipe (Wan 2.1) `"auto"` is the only legal value, so
    inventing one here would break every non-MoE config in the project.
    """
    entry = cls(ref=_REF)

    assert entry.branch == "auto"
    assert entry.target is None


def test_the_two_classes_agree_on_every_universe_member() -> None:
    """The halves must not drift, which is the filing's stated risk.

    Bug caught: mapping on one side only. `tests/test_lora_schema_parity.py`
    compares `.target` and would stay green, while `warm_reuse/matcher.py`
    compares `.branch` — so a server-only map makes every warm Wan attach
    re-swap its whole stack on every run (U55's mechanism).
    """
    for token in LORA_TARGET_UNIVERSE:
        core = LoraEntry(ref=_REF, target=token)
        server = LoraTarget(ref=_REF, target=token)
        assert (core.branch, core.target) == (server.branch, server.target)
