"""Tests for LocalProvider + FakeClock integration (AC #5–13)."""

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
from kinoforge.providers.local import LocalProvider


@pytest.fixture()
def provider() -> LocalProvider:
    """Return a LocalProvider backed by a FakeClock at t=0."""
    return LocalProvider(clock=FakeClock(start=0.0))


@pytest.fixture()
def spec() -> InstanceSpec:
    """Minimal InstanceSpec for creating a local instance."""
    return InstanceSpec(image="local:test")


def test_provider_name(provider: LocalProvider) -> None:
    """AC #5: LocalProvider.name == 'local'."""
    assert provider.name == "local"


def test_find_offers_returns_default_compliant_offer(provider: LocalProvider) -> None:
    """AC #6: find_offers returns >= 1 offer satisfying HardwareRequirements defaults."""
    reqs = HardwareRequirements()
    offers = provider.find_offers(reqs)
    assert len(offers) >= 1
    for o in offers:
        assert o.vram_gb >= reqs.min_vram_gb
        assert o.cost_rate_usd_per_hr <= reqs.max_usd_per_hr


def test_find_offers_uses_filter(provider: LocalProvider) -> None:
    """AC #6b: filter_offers is honoured — a restrictive override excludes local offers."""
    reqs = HardwareRequirements(min_vram_gb=999)  # no real card has 999 GB VRAM
    offers = provider.find_offers(reqs)
    assert offers == []


def test_create_instance_ready_and_listed(
    provider: LocalProvider, spec: InstanceSpec
) -> None:
    """AC #7: create_instance returns a ready Instance and it appears in list_instances."""
    instance = provider.create_instance(spec)
    assert instance.id != ""
    assert instance.provider == "local"
    assert instance.status == "ready"
    assert instance.created_at == 0.0  # FakeClock starts at 0
    assert instance in provider.list_instances()


def test_get_instance_returns_same_instance(
    provider: LocalProvider, spec: InstanceSpec
) -> None:
    """AC #8a: get_instance returns the same Instance object as create_instance."""
    instance = provider.create_instance(spec)
    fetched = provider.get_instance(instance.id)
    assert fetched is instance


def test_get_instance_unknown_raises_key_error(provider: LocalProvider) -> None:
    """AC #8b: get_instance raises KeyError for an unknown id."""
    with pytest.raises(KeyError):
        provider.get_instance("no-such-id")


def test_stop_instance_sets_status(provider: LocalProvider, spec: InstanceSpec) -> None:
    """AC #9a: stop_instance sets status to 'stopped'."""
    instance = provider.create_instance(spec)
    provider.stop_instance(instance.id)
    assert provider.get_instance(instance.id).status == "stopped"


def test_destroy_instance_removes_from_list(
    provider: LocalProvider, spec: InstanceSpec
) -> None:
    """AC #9b: destroy_instance removes the instance from list_instances."""
    instance = provider.create_instance(spec)
    provider.destroy_instance(instance.id)
    assert instance not in provider.list_instances()


def test_destroy_instance_idempotent(
    provider: LocalProvider, spec: InstanceSpec
) -> None:
    """AC #10: destroying an instance twice does not raise."""
    instance = provider.create_instance(spec)
    provider.destroy_instance(instance.id)
    provider.destroy_instance(instance.id)  # must not raise


def test_heartbeat_records_clock_time(spec: InstanceSpec) -> None:
    """AC #11: heartbeat records clock.now() as the instance's last heartbeat."""
    clock = FakeClock(start=100.0)
    provider = LocalProvider(clock=clock)
    instance = provider.create_instance(spec)
    # Advance time and record a heartbeat
    clock.advance(100.0)
    provider.heartbeat(instance.id)
    assert provider.last_heartbeat(instance.id) == 200.0


def test_last_heartbeat_none_before_first_beat(
    provider: LocalProvider, spec: InstanceSpec
) -> None:
    """AC #11b: last_heartbeat returns None for an instance with no recorded heartbeat."""
    instance = provider.create_instance(spec)
    assert provider.last_heartbeat(instance.id) is None


def test_endpoints_deterministic(provider: LocalProvider, spec: InstanceSpec) -> None:
    """AC #12: endpoints returns {'generate': f'local://{instance.id}'}."""
    instance = provider.create_instance(spec)
    eps = provider.endpoints(instance)
    assert eps == {"generate": f"local://{instance.id}"}


def test_import_registers_provider() -> None:
    """AC #13: importing kinoforge.providers.local registers 'local' in the registry."""
    import kinoforge.providers.local  # noqa: F401
    from kinoforge.core import registry

    factory = registry.get_provider("local")
    p = factory()
    assert p.name == "local"
