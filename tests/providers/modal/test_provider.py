"""Behavior: ModalProvider registration, offers, and heartbeat semantics."""

from kinoforge.core import registry
from kinoforge.core.interfaces import HardwareRequirements
from kinoforge.providers.modal import ModalProvider


def test_registry_resolves_modal():
    import kinoforge._adapters  # noqa: F401  # triggers self-registration

    provider = registry.get_provider("modal")()
    assert isinstance(provider, ModalProvider)
    assert provider.name == "modal"


def test_find_offers_returns_filtered_catalog():
    offers = ModalProvider().find_offers(HardwareRequirements(min_vram_gb=80))
    assert {o.id for o in offers} == {"A100-80GB", "H100"}


def test_last_heartbeat_is_none_and_heartbeat_is_noop():
    # Bug caught: a missing last_heartbeat AttributeError's every HeartbeatLoop
    # tick (the gen §21 SkyPilot failure); heartbeat must not raise either.
    provider = ModalProvider()
    assert provider.last_heartbeat("pod-x") is None
    provider.heartbeat("pod-x")  # must not raise
