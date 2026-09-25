"""Behavior: warm-attach compares the ROUTING TOKEN, not Wan's MoE field.

U55. ``servers/_lora.py`` stamped ``branch=row.target`` when building an H3
pod's inventory row, so a ``t2va`` pod reported ``branch="transformer"`` — H3's
vocabulary written into the field that holds Wan's MoE tokens. Meanwhile
``LoraEntry._resolve_branch_to_target`` maps ONE way: a cfg that sets ``target:``
directly leaves ``.branch`` at its ``"auto"`` default, because only the
deprecated ``branch:`` key ever writes into it.

``matcher.is_stack_match`` compared those two fields directly, so an H3 cfg
spelling ``target: transformer`` produced ``active.branch="transformer"`` against
``target.branch="auto"`` — a guaranteed mismatch. Harmless only while nothing
routes a warm-attach decision onto an H3 pod; the moment that wiring lands every
warm H3 pod fails the comparison and re-swaps on every run, a ~1.4 GB turbo-LoRA
re-download each time.

The fix has to hold BOTH shapes at once, which is what these tests pin:

* Wan's ``LoraInventoryEntry`` has no ``target`` field at all — only ``branch``.
  Comparing on ``target`` alone would read ``None == None`` for every Wan row and
  silently attach a pod with the WRONG MoE branch loaded. That is the regression
  this change could most easily introduce, so it gets an explicit control.
* H3 rows carry the real token in ``target``.

Hence a routing token read as "``target`` when set, else ``branch``, else
``auto``" on both sides, with a request-side ``"auto"`` treated as "the profile's
default partition" — a wildcard. That wildcard cannot weaken Wan: a MoE pipe
REFUSES ``branch="auto"`` outright (``BranchAutoNotAllowedOnMoE``), so the only
Wan shape that reaches it is the single-transformer pipe, whose inventory reads
``"auto"`` too and would have matched exactly anyway.
"""

from __future__ import annotations

from dataclasses import dataclass

from kinoforge.core.warm_reuse.matcher import is_stack_match


@dataclass
class _WanRow:
    """A Wan pod inventory row: ``branch`` only, no ``target`` field."""

    ref: str
    last_strength: float | None
    branch: str


@dataclass
class _H3Row:
    """An H3 pod inventory row, as ``servers/_lora.py`` builds it."""

    ref: str
    last_strength: float | None
    branch: str
    target: str | None


@dataclass
class _Want:
    """A resolved cfg-side stack entry (``core.lora.LoraEntry`` shape)."""

    ref: str
    strength: float
    branch: str = "auto"
    target: str | None = None


_REF = "hf:lightx2v/Minimax-h3-Turbo:turbo.safetensors"


def test_an_h3_pod_matches_a_cfg_that_spells_its_routing_as_target() -> None:
    """The defect. An H3 warm pod must not re-swap a stack it already holds.

    Bug caught: comparing ``active.branch`` against ``target.branch``. The pod
    reports the H3 token, the cfg leaves ``branch`` at ``"auto"`` because it
    used ``target:``, and the stacks never match — so every run re-downloads
    ~1.4 GB that is already on the pod.
    """
    # `branch` carries H3's token here because that is what the pod puts on
    # the wire today (`_inventory_rows`: `branch=row.target`). Building the
    # fixture as `branch="auto"` would encode the fix and pass trivially.
    active = [
        _H3Row(ref=_REF, last_strength=1.0, branch="transformer", target="transformer")
    ]
    want = [_Want(ref=_REF, strength=1.0, target="transformer")]

    assert is_stack_match(active, want) is True


def test_an_h3_cfg_with_no_target_matches_the_pods_default_partition() -> None:
    """Omitting ``target:`` means "the profile's default", not "no partition".

    Bug caught: an exact comparison that fails the default case. The pod
    reports the RESOLVED token (``transformer``) while the cfg carries no
    target at all, so a strict equality would mean a cfg that simply omits the
    key can never warm-attach — the common shape, permanently cold.
    """
    active = [
        _H3Row(ref=_REF, last_strength=1.0, branch="transformer", target="transformer")
    ]
    want = [_Want(ref=_REF, strength=1.0)]

    assert is_stack_match(active, want) is True


def test_a_wan_moe_branch_mismatch_is_still_a_mismatch() -> None:
    """Negative control, and the regression this change could most easily cause.

    Bug caught: comparing on ``target`` alone. Wan's ``LoraInventoryEntry``
    has NO ``target`` field, so both sides would read ``None``, every Wan stack
    would compare equal on routing, and the matcher would attach a pod with the
    wrong MoE branch loaded — producing silently wrong video from a pod that
    looked like a hit.
    """
    active = [_WanRow(ref=_REF, last_strength=1.0, branch="high_noise")]
    want = [_Want(ref=_REF, strength=1.0, branch="low_noise", target="low_noise")]

    assert is_stack_match(active, want) is False


def test_a_wan_moe_branch_agreement_still_matches() -> None:
    """Wan's existing warm-attach behaviour must be untouched.

    Bug caught: a routing token that reads ``target`` first and stops. A Wan
    row exposes no ``target``, so falling back to ``branch`` is what keeps the
    pre-existing MoE path working at all.
    """
    active = [_WanRow(ref=_REF, last_strength=1.0, branch="high_noise")]
    want = [_Want(ref=_REF, strength=1.0, branch="high_noise", target="high_noise")]

    assert is_stack_match(active, want) is True


def test_a_pre_p2_row_with_no_branch_attribute_still_compares_as_auto() -> None:
    """Pre-P2 inventory rows predate the field entirely.

    Bug caught: dropping the ``getattr(..., "auto")`` default while
    generalising, which would raise ``AttributeError`` mid-match on a ledger
    row written before P2 — turning a cache miss into a crash.
    """

    @dataclass
    class _PreP2:
        ref: str
        last_strength: float | None

    active = [_PreP2(ref=_REF, last_strength=1.0)]
    want = [_Want(ref=_REF, strength=1.0)]

    assert is_stack_match(active, want) is True


def test_the_h3_server_does_not_stamp_its_target_into_the_branch_field() -> None:
    """The other half of the fix: stop writing H3's vocabulary into Wan's field.

    Bug caught: ``branch=row.target`` at the row builder. Even with the matcher
    generalised, leaving H3's token in ``branch`` keeps a field whose documented
    vocabulary is Wan's MoE tokens holding something that is not one — which is
    how the next reader of ``branch`` inherits this defect.
    """
    from kinoforge.engines.diffusers.servers import _lora

    @dataclass
    class _Applied:
        ref: str
        filename: str
        size_bytes: int
        adapter_name: str
        strength: float
        target: str

    # Drive the REAL builder. Constructing `LoraInventoryEntryModel` directly
    # and asserting its default would test pydantic, not the call site where
    # `branch=row.target` actually happens.
    applied = _Applied(
        ref=_REF,
        filename="turbo.safetensors",
        size_bytes=1,
        adapter_name="a0",
        strength=1.0,
        target="transformer",
    )
    original = _lora.inventory_snapshot
    _lora.inventory_snapshot = lambda: [applied]  # type: ignore[list-item]
    try:
        rows = _lora._inventory_rows()
    finally:
        _lora.inventory_snapshot = original

    assert [r.target for r in rows] == ["transformer"]
    assert [r.branch for r in rows] == ["auto"]
