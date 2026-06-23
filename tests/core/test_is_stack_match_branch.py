"""``is_stack_match`` ``(ref, strength, branch)`` tuple comparison.

P2 §4.2 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

The matcher now compares ``branch`` per entry alongside ``ref`` and
``strength``. ``branch`` IN the matcher; OUT of ``capability_key`` (the
warm pod serves every branch combination via ``/lora/set_stack`` swap —
the same pod is reusable, but the stack-match decides whether a swap
is even necessary).
"""

from __future__ import annotations

from dataclasses import dataclass

from kinoforge.core.lora import LoraEntry
from kinoforge.core.warm_reuse.matcher import is_stack_match


@dataclass
class _Inventory:
    """Mirror of the duck-typed inventory entry shape consumed by the matcher."""

    ref: str
    last_strength: float | None
    branch: str = "auto"


def test_matches_identical_stacks() -> None:
    """Bug: matcher trivially fails on identical stacks because the
    branch column never gets compared (or compares None to "auto" and
    diverges silently)."""
    active = [
        _Inventory(ref="x", last_strength=1.0, branch="high_noise"),
        _Inventory(ref="y", last_strength=0.8, branch="low_noise"),
    ]
    target = [
        LoraEntry(ref="x", strength=1.0, branch="high_noise"),
        LoraEntry(ref="y", strength=0.8, branch="low_noise"),
    ]
    assert is_stack_match(active, target) is True


def test_rejects_branch_differences() -> None:
    """Bug: matcher returns True when same ref + strength but DIFFERENT
    branch — leading to a warm-attach onto a pod where the LoRA loaded
    into the wrong transformer. The whole purpose of P2 routing is
    defeated."""
    active = [_Inventory(ref="x", last_strength=1.0, branch="high_noise")]
    target = [LoraEntry(ref="x", strength=1.0, branch="low_noise")]
    assert is_stack_match(active, target) is False


def test_matches_duplicate_ref_in_two_branches_composite() -> None:
    """Bug: matcher collapses duplicate refs to a single entry (set
    semantics) so the same ref active in both branches looks like one
    entry — Q6 Option 1 composite identity gets broken silently."""
    active = [
        _Inventory(ref="x", last_strength=1.0, branch="high_noise"),
        _Inventory(ref="x", last_strength=1.0, branch="low_noise"),
    ]
    target = [
        LoraEntry(ref="x", strength=1.0, branch="high_noise"),
        LoraEntry(ref="x", strength=1.0, branch="low_noise"),
    ]
    assert is_stack_match(active, target) is True


def test_rejects_transposed_branches() -> None:
    """Bug: matcher ignores order, returning True when high/low ordering
    is swapped — leads to a wrong-stage LoRA recipe being treated as a
    cache hit."""
    active = [
        _Inventory(ref="x", last_strength=1.0, branch="low_noise"),
        _Inventory(ref="y", last_strength=0.8, branch="high_noise"),
    ]
    target = [
        LoraEntry(ref="x", strength=1.0, branch="high_noise"),
        LoraEntry(ref="y", strength=0.8, branch="low_noise"),
    ]
    assert is_stack_match(active, target) is False


def test_pre_p2_inventory_missing_branch_defaults_to_auto() -> None:
    """Bug: a pre-P2 pod returns inventory entries with no ``branch``
    field. The matcher must default that to ``"auto"`` so a pre-P2
    inventory + post-P2 cfg comparing ``branch="auto"`` still matches
    (rolling-upgrade compatibility)."""

    @dataclass
    class _Legacy:
        """Inventory shape without a branch attr."""

        ref: str
        last_strength: float

    active = [_Legacy(ref="x", last_strength=1.0)]
    target = [LoraEntry(ref="x", strength=1.0, branch="auto")]
    assert is_stack_match(active, target) is True
