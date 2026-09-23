"""Behavior: the client registry and each pod server agree on the LoRA targets.

Two declarations describe the same vocabulary from opposite sides of a 10-minute
boot: ``kinoforge.core.lora_profiles._REGISTRY`` (controller-side, read at config
load) and each server module's ``LORA_TARGET_UNIVERSE`` (pod-side, read by the
profile the server builds after its pipeline loads).

The dangerous direction is a client MORE permissive than the pod: the config
loads, the H200 boots, 124 GiB of weights land, and only then does the apply
refuse a target the controller had already accepted. The reverse — a pod that
could serve a target the client rejects — is merely a capability nobody can
reach. Both are caught here, for free, before anything is booked.

The registry is enumerated through :func:`registered_modules`, not by reaching
into ``_REGISTRY``: the accessor is the supported surface, and a test that
reaches past it is a test that pins a private name.
"""

from __future__ import annotations

import importlib

import pytest

from kinoforge.core.lora_profiles import (
    client_profile_for_server_module,
    registered_modules,
)


def test_the_registry_covers_both_shipped_diffusers_servers() -> None:
    """Both LoRA-serving server modules are registered, by name.

    Bug caught: an entry is dropped from ``_REGISTRY`` during a refactor. The
    parametrized test below then covers one module instead of two and passes
    vacuously, so the very mismatch it exists to catch ships unnoticed — and on
    the client side the dropped module reads as "this server serves no LoRAs",
    which refuses every stack at config load.
    """
    assert set(registered_modules()) == {
        "kinoforge.engines.diffusers.servers.wan_t2v_server",
        "kinoforge.engines.diffusers.servers.minimax_h3_server",
    }


@pytest.mark.parametrize("module_path", sorted(registered_modules()))
def test_server_declares_the_same_target_universe(module_path: str) -> None:
    """Each registered module's declared universe matches the client's.

    Bug caught: H3's server gains ``transformer_ref`` (or Wan's loses
    ``high_noise``) and only one of the two declarations is updated. Config load
    accepts a target the pod will refuse, and the refusal arrives after the
    boot, the weight download and the LoRA download have all been paid for.

    The expected value is not computed here — it is the OTHER declaration.
    That is the whole point: neither side is derived from the other, so the
    assertion fails the moment they drift.
    """
    mod = importlib.import_module(module_path)
    declared = getattr(mod, "LORA_TARGET_UNIVERSE", None)
    assert declared is not None, (
        f"{module_path} is registered as LoRA-serving but declares no "
        f"LORA_TARGET_UNIVERSE; the client registry and the pod have nothing "
        f"to agree on"
    )
    client = client_profile_for_server_module(module_path)
    assert client is not None
    assert tuple(declared) == client.target_universe
