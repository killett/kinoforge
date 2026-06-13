"""Tests for the B2 RunPod GraphQL balance satisfier."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from kinoforge.core.balance_endpoints import ProviderBalance, TransportError
from kinoforge.providers.runpod.balance import RunPodBalanceEndpoint

_FIXTURE = Path(__file__).parent / "fixtures" / "runpod_balance_response.json"


def _fixture_with_balance(balance: float) -> dict[str, object]:
    """Return a fixture-shaped dict with the clientBalance value overridden."""
    base = json.loads(_FIXTURE.read_text())
    base["data"]["myself"]["clientBalance"] = balance
    return base


def test_happy_path_returns_provider_balance() -> None:
    """BUG CATCH: float() unwrap MUST work whether SDK returns int or float."""
    captured: list[tuple[str, dict[str, object], dict[str, str]]] = []

    def fake_http_post(
        url: str, body: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]:
        captured.append((url, body, headers))
        return _fixture_with_balance(42.18)

    endpoint = RunPodBalanceEndpoint(api_key="rp_test", http_post=fake_http_post)
    before = datetime.now()
    result = endpoint.read()
    after = datetime.now()

    assert isinstance(result, ProviderBalance)
    assert result.usd == 42.18
    assert result.source == "runpod-graphql-clientBalance"
    assert result.currency == "USD"
    assert before <= result.as_of <= after
    url, body, headers = captured[0]
    assert url == "https://api.runpod.io/graphql"
    assert body == {"query": "{ myself { clientBalance } }"}
    assert headers["Authorization"] == "Bearer rp_test"
    assert headers["Content-Type"] == "application/json"
    assert "User-Agent" in headers


@pytest.mark.parametrize("api_key", [None, ""])
def test_missing_credential_returns_none_without_call(api_key: str | None) -> None:
    """No credential -> return None; MUST NOT call http_post.

    BUG CATCH: an ``api_key or 'MISSING'`` fallback that still hits the API
    would 401 the operator silently and burn a wire call per ``kinoforge cost``.
    """
    spy_call_count = 0

    def spy_http_post(
        url: str, body: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]:
        nonlocal spy_call_count
        spy_call_count += 1
        return {}

    endpoint = RunPodBalanceEndpoint(api_key=api_key, http_post=spy_http_post)
    assert endpoint.read() is None
    assert spy_call_count == 0


@pytest.mark.parametrize(
    "drift_response",
    [
        {"errors": [{"message": "auth"}]},
        {"data": {}},
        {"data": {"myself": {"id": "abc"}}},
        {"data": {"myself": {"clientBalance": "not-a-number"}}},
        {"data": {"myself": {"clientBalance": None}}},
    ],
)
def test_schema_drift_raises_transport_error(drift_response: dict[str, object]) -> None:
    """BUG CATCH: a KeyError leaking out of read() would break the render
    path's contract. EVERY schema-drift shape MUST land as TransportError."""

    def fake_http_post(
        url: str, body: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]:
        return drift_response

    endpoint = RunPodBalanceEndpoint(api_key="rp_test", http_post=fake_http_post)
    with pytest.raises(TransportError) as exc_info:
        endpoint.read()
    assert "schema drift" in str(exc_info.value)


def test_transport_error_propagates() -> None:
    """An http_post raising TransportError (network failure) MUST propagate."""

    def fake_http_post(
        url: str, body: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]:
        raise TransportError("connection refused")

    endpoint = RunPodBalanceEndpoint(api_key="rp_test", http_post=fake_http_post)
    with pytest.raises(TransportError, match="connection refused"):
        endpoint.read()


def test_negative_balance_flows_through_verbatim() -> None:
    """RunPod auto-debit accounts can sit briefly negative; rendered verbatim
    per spec §12 (negative balance row)."""

    def fake_http_post(
        url: str, body: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]:
        return _fixture_with_balance(-3.50)

    endpoint = RunPodBalanceEndpoint(api_key="rp_test", http_post=fake_http_post)
    result = endpoint.read()
    assert result is not None
    assert result.usd == -3.50
