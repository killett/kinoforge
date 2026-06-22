"""Warm-attach matcher: refs AND strength equality (P1)."""

from __future__ import annotations

from kinoforge.core.lora import LoraEntry
from kinoforge.core.warm_reuse.matcher import is_stack_match
from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraInventoryEntry


def _inv(ref: str, last_strength: float | None) -> LoraInventoryEntry:
    return LoraInventoryEntry(
        ref=ref,
        filename=f"{ref}.bin",
        size_bytes=1,
        downloaded_at_local="x",
        last_used_at_local="x",
        adapter_name="lora_0",
        last_strength=last_strength,
    )


def test_same_refs_same_strength_is_match() -> None:
    """Bug: a future edit drops the strength check from a happy-path
    match — warm-attach still works but the API is no longer claiming
    strength equality."""
    active = [_inv("civitai:1@2", 0.5)]
    target = [LoraEntry(ref="civitai:1@2", strength=0.5)]
    assert is_stack_match(active, target) is True


def test_same_refs_different_strength_not_match() -> None:
    """Bug: a future edit drops the strength check → user can't iterate
    on strength because the matcher silently keeps the old weight."""
    active = [_inv("civitai:1@2", 0.5)]
    target = [LoraEntry(ref="civitai:1@2", strength=1.5)]
    assert is_stack_match(active, target) is False


def test_isclose_tolerance_swallows_json_float_drift() -> None:
    """Bug: a future edit uses == instead of math.isclose → 0.1 round-
    tripped through JSON shows up as 0.10000000000000001 and the
    matcher schedules an unnecessary set_stack."""
    active = [_inv("civitai:1@2", 0.10000000000000001)]
    target = [LoraEntry(ref="civitai:1@2", strength=0.1)]
    assert is_stack_match(active, target) is True


def test_missing_last_strength_treated_as_1_0() -> None:
    """Bug: pre-P1 pod inventory entries (no last_strength) crash the
    matcher or compare as 0 → every warm-attach against a pre-P1 pod
    falsely fails to match."""
    active = [_inv("civitai:1@2", None)]
    target = [LoraEntry(ref="civitai:1@2", strength=1.0)]
    assert is_stack_match(active, target) is True


def test_ref_set_differs_short_circuits_to_false() -> None:
    """Bug: a future edit only checks strength after refs match by
    SET — but order matters for set_adapters. Different order must
    fail even if both refs are present."""
    active = [_inv("civitai:1@2", 1.0), _inv("civitai:3@4", 1.0)]
    target = [
        LoraEntry(ref="civitai:3@4", strength=1.0),
        LoraEntry(ref="civitai:1@2", strength=1.0),
    ]
    assert is_stack_match(active, target) is False


def test_length_mismatch_not_match() -> None:
    """Bug: a future edit zips with zip(...) without strict=True →
    a shorter active list silently matches the prefix of target."""
    active = [_inv("civitai:1@2", 1.0)]
    target = [
        LoraEntry(ref="civitai:1@2", strength=1.0),
        LoraEntry(ref="civitai:3@4", strength=1.0),
    ]
    assert is_stack_match(active, target) is False
