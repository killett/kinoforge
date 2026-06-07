"""Layer 1 — AuthStrategy ABC + typed boundary objects."""

from __future__ import annotations

import dataclasses

import pytest

from kinoforge.core.auth import (
    AuthStrategy,
    HealthResult,
    HttpRequest,
)
from kinoforge.core.interfaces import CredentialProvider

# ---------------------------------------------------------------------------
# Boundary types
# ---------------------------------------------------------------------------


def test_health_result_is_frozen_dataclass() -> None:
    r = HealthResult(
        ok=True, identity="kinoforge-runner@proj.iam.gserviceaccount.com", reason=None
    )
    assert r.ok is True
    assert r.identity == "kinoforge-runner@proj.iam.gserviceaccount.com"
    assert r.reason is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ok = False  # type: ignore[misc]


def test_http_request_is_frozen_dataclass() -> None:
    req = HttpRequest(method="GET", url="https://x", headers={"k": "v"}, body=None)
    assert req.method == "GET"
    assert req.url == "https://x"
    assert req.headers == {"k": "v"}
    assert req.body is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.method = "POST"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


def test_auth_strategy_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError, match="abstract"):
        AuthStrategy()  # type: ignore[abstract]


def test_auth_strategy_exposes_five_abstract_methods() -> None:
    expected = {
        "credentials_present",
        "health_check",
        "redact_patterns",
        "apply",
        "client_kwargs",
    }
    assert AuthStrategy.__abstractmethods__ == expected


def test_auth_strategy_subclass_must_implement_all_five() -> None:
    class Partial(AuthStrategy):  # missing methods on purpose
        def credentials_present(self) -> bool:
            return True

    with pytest.raises(TypeError, match="abstract"):
        Partial()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Bearer strategy
# ---------------------------------------------------------------------------


class _StaticCreds(CredentialProvider):
    """Inline CredentialProvider double for tests."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._mapping = mapping

    def get(self, key: str) -> str | None:
        return self._mapping.get(key)


def test_bearer_credentials_present_true_when_env_var_set() -> None:
    from kinoforge.core.auth import Bearer

    strat = Bearer(
        env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": "secret-123"})
    )
    assert strat.credentials_present() is True


def test_bearer_credentials_present_false_when_env_var_unset_or_empty() -> None:
    from kinoforge.core.auth import Bearer

    for value in (None, ""):
        strat = Bearer(
            env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": value})
        )
        assert strat.credentials_present() is False


def test_bearer_health_check_ok_against_fake_endpoint() -> None:
    from kinoforge.core.auth import Bearer, HealthResult

    calls: list[tuple[str, dict[str, str]]] = []

    def fake_http_get(url: str, headers: dict[str, str]) -> dict[str, str]:
        calls.append((url, headers))
        return {"account_id": "acc-xyz"}

    strat = Bearer(
        env_var="FAL_KEY",
        credential_provider=_StaticCreds({"FAL_KEY": "secret-123"}),
        health_check_url="https://fal.run/health",
        http_get=fake_http_get,
    )
    result = strat.health_check()
    assert isinstance(result, HealthResult)
    assert result.ok is True
    assert result.identity is not None
    assert calls[0][0] == "https://fal.run/health"
    assert calls[0][1]["Authorization"] == "Bearer secret-123"


def test_bearer_health_check_fail_when_env_missing() -> None:
    from kinoforge.core.auth import Bearer

    strat = Bearer(
        env_var="FAL_KEY",
        credential_provider=_StaticCreds({"FAL_KEY": None}),
        health_check_url="https://fal.run/health",
    )
    result = strat.health_check()
    assert result.ok is False
    assert result.identity is None
    assert "missing" in (result.reason or "").lower()


def test_bearer_redact_patterns_matches_actual_secret_value() -> None:
    from kinoforge.core.auth import Bearer

    strat = Bearer(
        env_var="FAL_KEY",
        credential_provider=_StaticCreds({"FAL_KEY": "sk-secret-abc-123"}),
    )
    patterns = strat.redact_patterns()
    assert any(p.search("Authorization: Bearer sk-secret-abc-123") for p in patterns)
    # Must NOT redact the env-var-name itself (we name the env var freely in logs).
    assert not any(p.search("FAL_KEY") for p in patterns)


def test_bearer_apply_adds_authorization_header() -> None:
    from kinoforge.core.auth import Bearer, HttpRequest

    strat = Bearer(
        env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": "abc"})
    )
    req = HttpRequest(
        method="POST", url="https://fal.run/x", headers={"X-Foo": "y"}, body=b"{}"
    )
    out = strat.apply(req)
    # Original untouched
    assert "Authorization" not in req.headers
    # New request has both old + new headers
    assert out.headers["X-Foo"] == "y"
    assert out.headers["Authorization"] == "Bearer abc"
    assert out.body == b"{}"


def test_bearer_apply_respects_scheme_and_header_name_overrides() -> None:
    from kinoforge.core.auth import Bearer, HttpRequest

    strat = Bearer(
        env_var="HF_TOKEN",
        credential_provider=_StaticCreds({"HF_TOKEN": "xyz"}),
        scheme="Token",
        header_name="X-Api-Key",
    )
    req = HttpRequest(method="GET", url="https://x", headers={}, body=None)
    out = strat.apply(req)
    assert out.headers == {"X-Api-Key": "Token xyz"}


def test_bearer_client_kwargs_returns_api_key_shape() -> None:
    from kinoforge.core.auth import Bearer

    strat = Bearer(
        env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": "abc"})
    )
    assert strat.client_kwargs() == {"api_key": "abc"}


def test_bearer_client_kwargs_empty_when_no_token() -> None:
    from kinoforge.core.auth import Bearer

    strat = Bearer(
        env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": None})
    )
    assert strat.client_kwargs() == {}


def test_bearer_health_check_ok_without_url_returns_env_var_identity() -> None:
    """When no health_check_url is configured, health_check returns ok=True
    with identity = env-var-name as a proxy (used when 'key is present' is
    all the check needs).
    """
    from kinoforge.core.auth import Bearer

    strat = Bearer(
        env_var="FAL_KEY",
        credential_provider=_StaticCreds({"FAL_KEY": "secret-abc"}),
        # health_check_url omitted on purpose
    )
    result = strat.health_check()
    assert result.ok is True
    assert result.identity == "FAL_KEY"
    assert result.reason is None


def test_bearer_health_check_fail_on_http_error() -> None:
    """A raising http_get must surface as ok=False with the exception text."""
    from kinoforge.core.auth import Bearer

    def boom(url: str, headers: dict[str, str]) -> dict[str, str]:
        raise ConnectionError("upstream 503")

    strat = Bearer(
        env_var="FAL_KEY",
        credential_provider=_StaticCreds({"FAL_KEY": "secret-abc"}),
        health_check_url="https://fal.run/health",
        http_get=boom,
    )
    result = strat.health_check()
    assert result.ok is False
    assert result.identity is None
    assert "upstream 503" in (result.reason or "")


def test_bearer_apply_raises_when_token_missing() -> None:
    """apply() must NOT silently emit an empty Bearer header when token is missing."""
    from kinoforge.core.auth import Bearer, HttpRequest

    strat = Bearer(
        env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": None})
    )
    req = HttpRequest(method="GET", url="https://x", headers={}, body=None)
    with pytest.raises(RuntimeError, match="FAL_KEY"):
        strat.apply(req)
