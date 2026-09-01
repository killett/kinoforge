"""Behavior: who chooses the SKU decides how the rate is known.

A marketplace provider books the SKU it was handed, so the catalog price IS
the rate (RATE_DETERMINISTIC). A declarative placer chooses for itself, so the
only honest source is a readback from the launched instance (RATE_READBACK).
Conflating the two is how a Lambda A100 billed $1.99 under a $1.09 cap while
every kinoforge surface reported the catalog number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from kinoforge.core import registry
from kinoforge.core.capabilities import Capability
from kinoforge.core.errors import RateCapExceeded

if TYPE_CHECKING:
    from kinoforge.core.interfaces import Instance
    from kinoforge.providers.modal import ModalProvider

_RATE_CAPS = {Capability.RATE_READBACK, Capability.RATE_DETERMINISTIC}


def _modal_provider_with_deployment(gpu: str) -> tuple[ModalProvider, Instance]:
    """Deploy one fake Modal app on ``gpu`` and return (provider, instance).

    Drives the real ``create_instance`` with injected factory/deployer seams —
    the same idiom as ``tests/providers/modal/test_provider.py`` — so the test
    proves the booked GPU is recorded at create time rather than asserting
    against a hand-populated private dict.

    Args:
        gpu: Modal GPU class to book, e.g. ``"A10"``.

    Returns:
        The provider and the Instance its ``create_instance`` returned.
    """
    from kinoforge.core.interfaces import (  # noqa: PLC0415
        InstanceSpec,
        Launch,
        Lifecycle,
        Offer,
        SetupStep,
    )
    from kinoforge.providers.modal import ModalProvider  # noqa: PLC0415

    provider = ModalProvider(
        app_factory=lambda req, modal_mod: ("APP", "SERVERFN"),
        deployer=lambda app, fn: "https://ws--kinoforge-run1-server.modal.run",
    )
    spec = InstanceSpec(
        image="runpod/pytorch:2.4.0-cuda12.4",
        offer=Offer(gpu, gpu, 24, "12.8", 0.0, mode="serverless"),
        run_id="run1",
        setup_steps=(SetupStep("echo hi"),),
        launch=Launch(("python", "-m", "server")),
        lifecycle=Lifecycle(idle_timeout_s=300),
    )
    return provider, provider.create_instance(spec)


@pytest.mark.parametrize(
    ("provider_name", "expected"),
    [
        ("skypilot", Capability.RATE_READBACK),
        ("runpod", Capability.RATE_DETERMINISTIC),
        ("modal", Capability.RATE_DETERMINISTIC),
    ],
)
def test_each_provider_declares_the_rate_source_it_actually_has(
    provider_name: str, expected: Capability
) -> None:
    """Bug caught: declaring RATE_DETERMINISTIC on skypilot would make the
    orchestrator trust a catalog number the optimizer never consulted — which
    is precisely the F4 under-report this stage exists to end."""
    import kinoforge._adapters  # noqa: F401,PLC0415 — registers the providers

    cls = registry.provider_class(provider_name)
    assert cls is not None
    assert expected in cls.capabilities()


@pytest.mark.parametrize("provider_name", ["skypilot", "runpod", "modal", "local"])
def test_no_provider_claims_both_rate_sources(provider_name: str) -> None:
    """Bug caught: declaring both lets the enforcement point pick whichever
    branch it happens to test first, so the rule that governs a money decision
    would depend on statement order."""
    import kinoforge._adapters  # noqa: F401,PLC0415

    cls = registry.provider_class(provider_name)
    assert cls is not None
    assert len(_RATE_CAPS & cls.capabilities()) <= 1


@pytest.mark.parametrize("provider_name", ["runpod", "modal", "local"])
def test_enumerating_providers_declare_catalog_enumeration(provider_name: str) -> None:
    """Bug caught: dropping the declaration on a provider that DOES have a
    catalog would make Task 9's gate refuse `kinoforge offers` on runpod —
    turning a working command into a refusal."""
    import kinoforge._adapters  # noqa: F401,PLC0415

    cls = registry.provider_class(provider_name)
    assert cls is not None
    assert Capability.CATALOG_ENUMERATION in cls.capabilities()


def test_skypilot_does_not_claim_catalog_enumeration() -> None:
    """Bug caught: SkyPilot has no catalog to enumerate — `kinoforge offers`
    against it today prints a synthetic single-entry list that no launch ever
    consults. Declaring the capability keeps that fiction alive."""
    import kinoforge._adapters  # noqa: F401,PLC0415

    cls = registry.provider_class("skypilot")
    assert cls is not None
    assert Capability.CATALOG_ENUMERATION not in cls.capabilities()


def test_skypilot_realized_rate_reads_the_launched_handle() -> None:
    """Bug caught: sourcing the rate from spec.offer (what create_instance does
    today) reports the number kinoforge ASKED for. The optimizer's choice is
    only in the handle."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: PLC0415

    class _Resources:
        def get_cost(self, seconds: float) -> float:
            assert seconds == 3600
            return 1.99

    class _Handle:
        launched_resources = _Resources()

    class _FakeSky:
        def status(self, **_kw: object) -> list[dict[str, object]]:
            return [{"name": "kf-1", "handle": _Handle(), "status": "UP"}]

    provider = SkyPilotProvider(sky_client=_FakeSky())
    inst = Instance(id="kf-1", provider="skypilot", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) == pytest.approx(1.99)


def test_skypilot_realized_rate_is_none_when_the_handle_has_no_resources() -> None:
    """Bug caught: raising here turns an unreadable rate into a crash between
    create_instance and provision, leaving a live instance nobody tore down.
    None is what lets the enforcement point destroy it deliberately."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: PLC0415

    class _FakeSky:
        def status(self, **_kw: object) -> list[dict[str, object]]:
            return [{"name": "kf-1", "handle": None, "status": "UP"}]

    provider = SkyPilotProvider(sky_client=_FakeSky())
    inst = Instance(id="kf-1", provider="skypilot", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_skypilot_realized_rate_is_none_for_an_unknown_cluster() -> None:
    """Boundary. Bug caught: returning the FIRST record's cost regardless of
    name would report a neighbouring cluster's rate — worse than unreadable,
    because it looks authoritative."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: PLC0415

    class _Resources:
        def get_cost(self, seconds: float) -> float:
            return 99.0

    class _Handle:
        launched_resources = _Resources()

    class _FakeSky:
        def status(self, **_kw: object) -> list[dict[str, object]]:
            return [{"name": "someone-elses-cluster", "handle": _Handle()}]

    provider = SkyPilotProvider(sky_client=_FakeSky())
    inst = Instance(id="kf-1", provider="skypilot", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_skypilot_realized_rate_is_none_when_status_raises() -> None:
    """Bug caught: `sky.status()` reaches the API server over the network and
    does fail (the S3 smoke saw one). Letting that propagate crashes the launch
    between create_instance and provision — the exact window where an
    un-torn-down cluster becomes an orphan."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: PLC0415

    class _FakeSky:
        def status(self, **_kw: object) -> list[dict[str, object]]:
            raise RuntimeError("api server unreachable")

    provider = SkyPilotProvider(sky_client=_FakeSky())
    inst = Instance(id="kf-1", provider="skypilot", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_skypilot_realized_rate_is_none_when_get_cost_raises() -> None:
    """Bug caught: `Resources.get_cost` raises on a resources record whose
    cloud has no price for the SKU. Same window, same orphan."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: PLC0415

    class _Resources:
        def get_cost(self, seconds: float) -> float:
            raise ValueError("no price for this SKU")

    class _Handle:
        launched_resources = _Resources()

    class _FakeSky:
        def status(self, **_kw: object) -> list[dict[str, object]]:
            return [{"name": "kf-1", "handle": _Handle()}]

    provider = SkyPilotProvider(sky_client=_FakeSky())
    inst = Instance(id="kf-1", provider="skypilot", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_runpod_realized_rate_reads_cost_per_hr_from_the_pod() -> None:
    """Bug caught: RunPod's catalog price and the pod's billed costPerHr can
    drift (spot/community repricing). The pod is the authority."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.runpod import RunPodProvider  # noqa: PLC0415

    def _post(url: str, body: dict[str, object]) -> dict[str, object]:
        return {"data": {"myself": {"pods": [{"id": "pod-1", "costPerHr": "0.34"}]}}}

    provider = RunPodProvider(creds=None, http_post=_post, http_get=lambda _: {})
    inst = Instance(id="pod-1", provider="runpod", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) == pytest.approx(0.34)


def test_runpod_realized_rate_is_none_when_the_pod_is_gone() -> None:
    """Boundary. Bug caught: falling back to the first pod in the list would
    price a cluster-mate; falling back to 0.0 would report a free pod, and 0.0
    passes every cap silently."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.runpod import RunPodProvider  # noqa: PLC0415

    def _post(url: str, body: dict[str, object]) -> dict[str, object]:
        return {"data": {"myself": {"pods": [{"id": "other", "costPerHr": "9.99"}]}}}

    provider = RunPodProvider(creds=None, http_post=_post, http_get=lambda _: {})
    inst = Instance(id="pod-1", provider="runpod", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_runpod_realized_rate_is_none_when_cost_per_hr_is_absent() -> None:
    """Bug caught: an early-boot pod answers without costPerHr. Coercing that
    absence to 0.0 (which `_pod_to_instance` deliberately does for the STATUS
    surface) would tell the enforcement point the pod is free."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.runpod import RunPodProvider  # noqa: PLC0415

    def _post(url: str, body: dict[str, object]) -> dict[str, object]:
        return {"data": {"myself": {"pods": [{"id": "pod-1"}]}}}

    provider = RunPodProvider(creds=None, http_post=_post, http_get=lambda _: {})
    inst = Instance(id="pod-1", provider="runpod", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_runpod_realized_rate_is_none_when_the_transport_fails() -> None:
    """Bug caught: the GraphQL call is a network hop mid-launch; propagating
    its failure crashes between create_instance and provision."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.runpod import RunPodProvider  # noqa: PLC0415

    def _post(url: str, body: dict[str, object]) -> dict[str, object]:
        raise OSError("connection reset")

    provider = RunPodProvider(creds=None, http_post=_post, http_get=lambda _: {})
    inst = Instance(id="pod-1", provider="runpod", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_modal_realized_rate_is_the_catalog_price_of_the_booked_gpu() -> None:
    """Bug caught: Modal's price filter is structurally skipped (its catalog is
    mode="serverless", and filter_offers applies the ceiling only to pods), so
    the catalog read here is the ONLY thing that can enforce a cap on Modal."""
    from kinoforge.providers.modal._catalog import MODAL_GPU_CATALOG  # noqa: PLC0415

    a10 = next(o for o in MODAL_GPU_CATALOG if o.gpu_type == "A10")
    provider, inst = _modal_provider_with_deployment("A10")
    assert provider.realized_rate(inst) == pytest.approx(a10.cost_rate_usd_per_hr)


def test_modal_realized_rate_falls_back_to_the_instance_rate_on_warm_attach() -> None:
    """Bug caught: a warm attach never ran create_instance in this process, so
    the deployment map is empty. Returning None there would tear down a healthy
    warm pod under the readback branch the moment Modal ever declares it."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.modal import ModalProvider  # noqa: PLC0415

    provider = ModalProvider()
    inst = Instance(
        id="unknown-run",
        provider="modal",
        status="ready",
        created_at=0.0,
        cost_rate_usd_per_hr=1.10,
    )
    assert provider.realized_rate(inst) == pytest.approx(1.10)


def test_modal_realized_rate_is_none_when_nothing_is_known() -> None:
    """Boundary. Bug caught: reporting 0.0 for an instance whose rate is simply
    unknown reads as free, and free passes every cap."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.modal import ModalProvider  # noqa: PLC0415

    provider = ModalProvider()
    inst = Instance(id="unknown-run", provider="modal", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_local_realized_rate_is_zero_not_none() -> None:
    """Bug caught: None on an unbilled provider is indistinguishable from an
    unreadable rate, and 0.0 is not a guess here — it is the price."""
    from kinoforge.core.interfaces import Instance  # noqa: PLC0415
    from kinoforge.providers.local import LocalProvider  # noqa: PLC0415

    inst = Instance(id="local-1", provider="local", status="ready", created_at=0.0)
    assert LocalProvider().realized_rate(inst) == pytest.approx(0.0)


def test_the_abc_default_is_none_so_a_new_provider_cannot_pass_silently() -> None:
    """Bug caught: defaulting to 0.0 would let a fifth provider that never
    implements realized_rate() report every instance as free, and 0.0 is under
    every cap ever written."""
    from typing import cast  # noqa: PLC0415

    from kinoforge.core.interfaces import ComputeProvider, Instance  # noqa: PLC0415

    inst = Instance(id="x", provider="x", status="ready", created_at=0.0)
    unbound = ComputeProvider.realized_rate
    assert unbound(cast("ComputeProvider", object()), inst) is None


def test_rate_cap_exceeded_names_both_numbers_and_the_identity() -> None:
    """Bug caught: a message that says only 'over budget' leaves an operator
    unable to tell a cap that is too low from a placement that is wrong, which
    is the difference between editing one YAML line and debugging a provider."""
    exc = RateCapExceeded(
        realized=1.99,
        cap=1.09,
        instance_id="kinoforge-xyz",
        placement_summary="sku=A100:1, cloud=lambda, region=us-west-2",
    )
    text = str(exc)
    assert "1.9900" in text
    assert "1.0900" in text
    assert "kinoforge-xyz" in text
    assert "cloud=lambda" in text


def test_rate_cap_exceeded_renders_an_unreadable_rate_without_lying() -> None:
    """Bug caught: formatting None through a float format string either raises
    or prints '0.0000', and a report of $0.0000/hr for a rate nobody could read
    is worse than saying it was unreadable."""
    exc = RateCapExceeded(
        realized=None,
        cap=1.09,
        instance_id="kinoforge-xyz",
        placement_summary="sku=A100:1",
    )
    assert "unreadable" in str(exc)
    assert "0.0000" not in str(exc)


def test_rate_cap_exceeded_keeps_its_fields_readable_by_a_caller() -> None:
    """Bug caught: a caller (the live smoke, and the CLI's error surface) reads
    `exc.realized` to prove the readback was a real number rather than the
    unreadable branch. Formatting the numbers into the message and dropping the
    attributes would make that claim unprovable."""
    exc = RateCapExceeded(
        realized=1.99,
        cap=1.09,
        instance_id="kinoforge-xyz",
        placement_summary="sku=A100:1",
    )
    assert exc.realized == pytest.approx(1.99)
    assert exc.cap == pytest.approx(1.09)
    assert exc.instance_id == "kinoforge-xyz"
    assert exc.placement_summary == "sku=A100:1"
