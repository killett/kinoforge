"""Substrate tests for B2 / Layer X balance-readout."""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from kinoforge.core.balance_endpoints import (
    BalanceEndpoint,
    NoBalanceEndpoint,
    ProviderBalance,
    TransportError,
    provider_balance_supported,
)


def test_provider_balance_frozen() -> None:
    """BUG CATCH: mutation MUST raise; aggregator relies on frozen identity."""
    b = ProviderBalance(usd=10.0, as_of=datetime(2026, 6, 12), source="src")
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.usd = 20.0  # type: ignore[misc]


def test_provider_balance_default_currency() -> None:
    """Currency defaults to USD so today's call sites don't need to pass it."""
    b = ProviderBalance(usd=10.0, as_of=datetime(2026, 6, 12), source="src")
    assert b.currency == "USD"


def test_no_balance_endpoint_read_returns_none() -> None:
    """NoBalanceEndpoint short-circuits read() to None unconditionally."""
    assert NoBalanceEndpoint().read() is None  # type: ignore[func-returns-value]


def test_no_balance_endpoint_is_balance_endpoint_protocol() -> None:
    """Protocol structural conformance — required for build_balance_endpoint_for return type."""
    endpoint = NoBalanceEndpoint()
    assert isinstance(endpoint, BalanceEndpoint)


def test_transport_error_is_exception_not_value_error() -> None:
    """BUG CATCH: TransportError under ValueError gets swallowed by broad
    `except ValueError` arms in legacy CLI code — keep it under bare Exception."""
    assert issubclass(TransportError, Exception)
    assert not issubclass(TransportError, ValueError)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("runpod", True),
        ("skypilot", False),
        ("local", False),
        ("", False),
        ("unknown", False),
        ("RUNPOD", False),  # case-sensitive
    ],
)
def test_provider_balance_supported(kind: str, expected: bool) -> None:
    """RunPod is the lone shipping satisfier; everything else False per spec §3."""
    assert provider_balance_supported(kind) is expected


def test_substrate_does_not_import_provider_modules() -> None:
    """Core-import-ban invariant: balance_endpoints.py contains no imports
    from kinoforge.providers / engines / sources."""
    src = __import__(
        "kinoforge.core.balance_endpoints",
        fromlist=["*"],
    ).__file__
    assert src is not None
    with open(src) as fh:
        text = fh.read()
    forbidden_prefixes = (
        "kinoforge.providers",
        "kinoforge.engines",
        "kinoforge.sources",
    )
    for prefix in forbidden_prefixes:
        assert prefix not in text, (
            f"balance_endpoints.py contains forbidden import prefix {prefix!r}"
        )
