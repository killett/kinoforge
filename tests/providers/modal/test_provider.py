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


def test_create_instance_deploys_and_returns_endpoint():
    from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer

    captured = {}

    def fake_factory(req, modal_mod):
        captured["req"] = req
        return ("APP", "SERVERFN")

    def fake_deploy(app, server_fn):
        captured["deployed"] = (app, server_fn)
        return "https://ws--kinoforge-run777-server.modal.run"

    provider = ModalProvider(app_factory=fake_factory, deployer=fake_deploy)
    spec = InstanceSpec(
        image="runpod/pytorch:2.4.0-cuda12.4",
        offer=Offer("A10", "A10", 24, "12.4", 1.10, mode="serverless"),
        run_id="run777",
        provision_script="echo hi",
        run_cmd=["python", "-m", "server"],
        env={"HF_HOME": "/cache/hf"},
        lifecycle=Lifecycle(idle_timeout_s=300),
    )
    inst = provider.create_instance(spec)

    assert inst.id == "run777"
    assert inst.provider == "modal"
    assert inst.status == "starting"
    assert inst.endpoints == {"8000": "https://ws--kinoforge-run777-server.modal.run"}
    assert inst.cost_rate_usd_per_hr == 1.10
    assert captured["req"].gpu == "A10"
    assert captured["req"].scaledown_window_s == 300


def test_create_instance_requires_run_cmd():
    from kinoforge.core.interfaces import InstanceSpec, Offer

    provider = ModalProvider()
    spec = InstanceSpec(
        image="img",
        offer=Offer("A10", "A10", 24, "12.4", 1.10, mode="serverless"),
        run_id="r",
        provision_script="echo hi",
        run_cmd=None,  # no server → invalid for Modal
    )
    import pytest

    with pytest.raises(ValueError, match="run_cmd"):
        provider.create_instance(spec)
