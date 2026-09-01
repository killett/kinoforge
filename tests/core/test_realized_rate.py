"""Behavior: who chooses the SKU decides how the rate is known.

A marketplace provider books the SKU it was handed, so the catalog price IS
the rate (RATE_DETERMINISTIC). A declarative placer chooses for itself, so the
only honest source is a readback from the launched instance (RATE_READBACK).
Conflating the two is how a Lambda A100 billed $1.99 under a $1.09 cap while
every kinoforge surface reported the catalog number.
"""

from __future__ import annotations

import pytest

from kinoforge.core import registry
from kinoforge.core.capabilities import Capability
from kinoforge.core.errors import RateCapExceeded

_RATE_CAPS = {Capability.RATE_READBACK, Capability.RATE_DETERMINISTIC}


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
