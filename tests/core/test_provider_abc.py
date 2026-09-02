"""Behavior: ensure_endpoints defaults to endpoints on every provider but skypilot."""

from __future__ import annotations

import pytest

from kinoforge.core.interfaces import ComputeProvider, Instance
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.runpod import RunPodProvider


@pytest.mark.parametrize(
    "provider_factory",
    [LocalProvider, RunPodProvider, ModalProvider],
    ids=["local", "runpod", "modal"],
)
def test_default_ensure_endpoints_is_the_plain_read(
    provider_factory: type[ComputeProvider],
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
    """
    provider = provider_factory()
    inst = Instance(
        id=f"{provider.name}-1",
        provider=provider.name,
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    assert provider.ensure_endpoints(inst) == provider.endpoints(inst)
