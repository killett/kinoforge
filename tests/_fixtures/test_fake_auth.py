"""Tests for the FakeAuthStrategy shared test fixture."""

from __future__ import annotations

from kinoforge.core.auth import AuthStrategy, HealthResult, HttpRequest
from tests._fixtures.fake_auth import FakeAuthStrategy


def test_fake_auth_strategy_is_auth_strategy_subclass() -> None:
    assert issubclass(FakeAuthStrategy, AuthStrategy)


def test_fake_auth_strategy_constructs_with_defaults() -> None:
    strat = FakeAuthStrategy()
    assert strat.credentials_present() is True


def test_fake_auth_credentials_present_configurable() -> None:
    assert FakeAuthStrategy(credentials_ok=True).credentials_present() is True
    assert FakeAuthStrategy(credentials_ok=False).credentials_present() is False


def test_fake_auth_health_check_ok_default() -> None:
    r = FakeAuthStrategy().health_check()
    assert isinstance(r, HealthResult)
    assert r.ok is True
    assert r.identity == "fake-identity"


def test_fake_auth_health_check_failure_when_creds_missing() -> None:
    r = FakeAuthStrategy(credentials_ok=False).health_check()
    assert r.ok is False
    assert r.identity is None
    assert r.reason is not None


def test_fake_auth_redact_patterns_matches_configured_token() -> None:
    strat = FakeAuthStrategy(fake_token="fake-sk-xyz")
    patterns = strat.redact_patterns()
    assert any(p.search("Authorization: Fake fake-sk-xyz") for p in patterns)


def test_fake_auth_apply_adds_authorization_header() -> None:
    strat = FakeAuthStrategy(fake_token="t-1")
    req = HttpRequest(method="GET", url="https://x", headers={}, body=None)
    out = strat.apply(req)
    assert out.headers == {"Authorization": "Fake t-1"}


def test_fake_auth_client_kwargs_returns_fake_token() -> None:
    strat = FakeAuthStrategy(fake_token="abc")
    assert strat.client_kwargs() == {"fake_token": "abc"}
