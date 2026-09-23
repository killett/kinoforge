"""Behavior: the client registry knows which server modules serve LoRAs.

The universe is per model family. A globally-keyed vocabulary would let a Wan
token pass config load and fail only after a 45-minute H200 boot.
"""

from __future__ import annotations

from kinoforge.core.lora_profiles import (
    client_profile_for_server_module,
    registered_modules,
)

_WAN = "kinoforge.engines.diffusers.servers.wan_t2v_server"
_H3 = "kinoforge.engines.diffusers.servers.minimax_h3_server"


def test_h3_module_is_supported_with_partition_targets() -> None:
    """H3's universe is its two checkpoint partitions, not Wan's noise split."""
    profile = client_profile_for_server_module(_H3)
    assert profile is not None
    assert profile.supported is True
    assert profile.target_universe == ("transformer", "transformer_ref")


def test_wan_module_is_supported_with_moe_targets() -> None:
    """Wan's universe stays the MoE noise split."""
    profile = client_profile_for_server_module(_WAN)
    assert profile is not None
    assert profile.target_universe == ("high_noise", "low_noise")


def test_unregistered_module_returns_none() -> None:
    """An unknown server module is not silently assumed to serve LoRAs.

    Catches a future fifth server inheriting today's silent no-op by omission.
    """
    assert client_profile_for_server_module("pkg.mod.some_new_server") is None


def test_registered_modules_exposes_both_entries_without_touching_private_state() -> (
    None
):
    """The public accessor is the supported way to enumerate the registry.

    Guards the parity test (a later task) against reaching into a private
    ``_REGISTRY`` dict directly — this is what it should call instead.
    """
    assert set(registered_modules()) == {_WAN, _H3}
