"""Behavior: ensure_endpoints defaults to endpoints on every provider but skypilot."""

from __future__ import annotations

from kinoforge.core.interfaces import Instance
from kinoforge.providers.local import LocalProvider


def test_default_ensure_endpoints_is_the_plain_read() -> None:
    """A provider with nothing to repair answers identically through both doors.

    Bug caught: making ``ensure_endpoints`` abstract would force runpod, modal
    and local to write an identical passthrough, and one of them would
    eventually drift from ``endpoints``.
    """
    provider = LocalProvider()
    inst = Instance(
        id="local-1",
        provider="local",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    assert provider.ensure_endpoints(inst) == provider.endpoints(inst)
