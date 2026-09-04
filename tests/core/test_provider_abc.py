"""Behavior: the ComputeProvider ABC's defaults, and what each provider declares.

Two claims live here. ``ensure_endpoints`` defaults to the plain ``endpoints``
read on every provider but skypilot; and every registered provider explicitly
declares which of its create errors prove nothing was booked (compute-seam S5,
ruling C1).
"""

from __future__ import annotations

import pytest

import kinoforge._adapters  # noqa: F401 — composition root; providers self-register
from kinoforge.core import registry
from kinoforge.core.interfaces import ComputeProvider, Instance
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.runpod import RunPodProvider

#: Every provider the composition root registers. Named explicitly for the same
#: reason ``tests/providers/test_field_consumption_parity.py`` does it: an
#: import-order regression that leaves the registry empty turns a parametrize
#: over it into zero cases, which reads as a green suite.
_EXPECTED_PROVIDERS = frozenset({"local", "modal", "runpod", "skypilot"})


@pytest.mark.parametrize(
    ("provider_factory", "endpoints", "tags"),
    [
        (LocalProvider, {}, {}),
        (RunPodProvider, {}, {"ports": "8000"}),
        (ModalProvider, {"8000": "https://kf--x.modal.run"}, {}),
    ],
    ids=["local", "runpod", "modal"],
)
def test_default_ensure_endpoints_is_the_plain_read(
    provider_factory: type[ComputeProvider],
    endpoints: dict[str, str],
    tags: dict[str, str],
) -> None:
    """A provider with nothing to repair answers identically through both doors.

    Bug caught: making ``ensure_endpoints`` abstract would force runpod, modal
    and local to write an identical passthrough, and one of them would
    eventually drift from ``endpoints``. Covers all three ABC-default
    inheritors with zero network client construction — each provider's bare
    ``()`` constructor is offline-safe (RunPod's default HTTP seams are built
    lazily and never invoked here; Modal's ``endpoints`` reads only its
    in-memory deployment cache). SkyPilot is the sole override and is tested
    separately in ``tests/providers/test_skypilot_endpoints.py``.

    Each case is seeded so its provider returns a NON-EMPTY map. With a bare
    ``Instance`` both runpod and modal return ``{}`` and the assertion is
    ``{} == {}`` — which passes for an ``ensure_endpoints`` that ignores its
    argument and returns a literal ``{}``, i.e. for the exact drift this is
    meant to catch.

    Args:
        provider_factory: The provider class under test.
        endpoints: Seed endpoints (modal echoes these when it has no
            deployment record).
        tags: Seed tags (runpod derives its proxy URLs from ``ports``).
    """
    provider = provider_factory()
    inst = Instance(
        id=f"{provider.name}-1",
        provider=provider.name,
        status="ready",
        created_at=0.0,
        endpoints=endpoints,
        tags=tags,
        cost_rate_usd_per_hr=0.0,
    )
    through_the_ensure_door = provider.ensure_endpoints(inst)
    assert through_the_ensure_door == provider.endpoints(inst)
    assert through_the_ensure_door, (
        "both doors returned {} — this case cannot tell a passthrough from a "
        "hardcoded empty map"
    )


@pytest.mark.parametrize("provider_name", sorted(_EXPECTED_PROVIDERS))
def test_every_provider_declares_its_nothing_booked_errors(
    provider_name: str,
) -> None:
    """Declaring is part of writing a provider (compute-seam S5, ruling C1).

    The orchestrator writes a durable ``kf_launch_phase=launching`` row before
    ``create_instance`` and, since C1, deletes it on failure ONLY for the
    errors a provider names here — a raise on its own is not proof nothing was
    booked. The ABC default is the empty tuple, which is the SAFE answer, so a
    provider that forgets simply keeps every row and nothing breaks loudly.
    That silence is what this test exists to remove: a new provider author has
    to make the call explicitly, in their own class.

    Bug caught: adding a provider whose ``create_instance`` has an obvious
    "refused before anything was requested" error and never declaring it, so
    every such refusal leaves a ghost row until the reconciler's grace window
    expires.

    Args:
        provider_name: The registry key of the provider under test.
    """
    cls = registry.provider_class(provider_name)
    assert cls is not None, f"{provider_name} is not registered"
    assert "nothing_booked_errors" in vars(cls), (
        f"{cls.__name__} inherits the ABC default instead of declaring which "
        f"of its create errors prove nothing was booked"
    )
    declared = cls.nothing_booked_errors()
    assert isinstance(declared, tuple)
    assert all(
        isinstance(exc, type) and issubclass(exc, BaseException) for exc in declared
    ), f"{cls.__name__} declared something that cannot appear in an except clause"


def test_the_registry_holds_every_expected_provider() -> None:
    """The parametrize above cannot go vacuous unnoticed.

    Bug caught: an import-order regression that empties the registry, which
    turns ``test_every_provider_declares_its_nothing_booked_errors`` into zero
    collected cases and reads as green.
    """
    assert _EXPECTED_PROVIDERS.issubset(set(registry.provider_names()))
