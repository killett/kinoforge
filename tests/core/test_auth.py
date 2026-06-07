"""Layer 1 — AuthStrategy ABC + typed boundary objects."""

from __future__ import annotations

import dataclasses
import pathlib

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


# ---------------------------------------------------------------------------
# GCPServiceAccount strategy
# ---------------------------------------------------------------------------


class _FakeGCPCredentials:
    """Minimal stand-in for ``google.auth.credentials.Credentials``."""

    def __init__(self, token: str, service_account_email: str | None = None) -> None:
        self.token = token
        self.service_account_email = service_account_email
        self.refresh_calls = 0

    def refresh(self, _request: object) -> None:
        self.refresh_calls += 1

    @property
    def expired(self) -> bool:
        return False

    @property
    def valid(self) -> bool:
        return True


def _install_fake_google_auth(
    monkeypatch: pytest.MonkeyPatch,
    credentials: _FakeGCPCredentials | None = None,
    raise_default: Exception | None = None,
) -> None:
    """Install a fake ``google.auth`` module so tests run without the SDK."""
    import sys
    import types

    fake_google = types.ModuleType("google")
    fake_auth = types.ModuleType("google.auth")
    fake_transport = types.ModuleType("google.auth.transport")
    fake_transport_requests = types.ModuleType("google.auth.transport.requests")

    def fake_default(scopes=None, quota_project_id=None):  # noqa: ANN001,ANN202
        if raise_default is not None:
            raise raise_default
        return (credentials, "fake-project-id")

    fake_auth.default = fake_default  # type: ignore[attr-defined]
    fake_transport_requests.Request = lambda: object()  # type: ignore[attr-defined]

    fake_google.auth = fake_auth  # type: ignore[attr-defined]
    fake_auth.transport = fake_transport  # type: ignore[attr-defined]
    fake_transport.requests = fake_transport_requests  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.auth", fake_auth)
    monkeypatch.setitem(sys.modules, "google.auth.transport", fake_transport)
    monkeypatch.setitem(
        sys.modules, "google.auth.transport.requests", fake_transport_requests
    )


def test_gcp_credentials_present_true_when_adc_file_exists(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    fake_sa = tmp_path / "sa.json"
    fake_sa.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_sa))
    strat = GCPServiceAccount()
    assert strat.credentials_present() is True


def test_gcp_credentials_present_false_when_adc_file_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    strat = GCPServiceAccount()
    assert strat.credentials_present() is False


def test_gcp_health_check_ok_when_default_returns_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    creds = _FakeGCPCredentials(
        token="ya29.fake-access-token",
        service_account_email="kinoforge-runner@proj.iam.gserviceaccount.com",
    )
    _install_fake_google_auth(monkeypatch, credentials=creds)
    strat = GCPServiceAccount()
    result = strat.health_check()
    assert result.ok is True
    assert result.identity == "kinoforge-runner@proj.iam.gserviceaccount.com"
    assert creds.refresh_calls == 1


def test_gcp_health_check_fail_when_default_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    _install_fake_google_auth(monkeypatch, raise_default=RuntimeError("no ADC"))
    strat = GCPServiceAccount()
    result = strat.health_check()
    assert result.ok is False
    assert "no ADC" in (result.reason or "")


def test_gcp_redact_patterns_matches_access_token_shape() -> None:
    from kinoforge.core.auth import GCPServiceAccount

    strat = GCPServiceAccount()
    patterns = strat.redact_patterns()
    sample = "Authorization: Bearer ya29.abc-def_ghi-123"
    assert any(p.search(sample) for p in patterns)


def test_gcp_apply_adds_authorization_bearer_from_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import GCPServiceAccount, HttpRequest

    creds = _FakeGCPCredentials(
        token="ya29.actual-token",
        service_account_email="kf@proj.iam.gserviceaccount.com",
    )
    _install_fake_google_auth(monkeypatch, credentials=creds)
    strat = GCPServiceAccount()
    req = HttpRequest(
        method="POST", url="https://aiplatform...", headers={}, body=b"{}"
    )
    out = strat.apply(req)
    assert out.headers["Authorization"] == "Bearer ya29.actual-token"


def test_gcp_client_kwargs_returns_credentials_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    creds = _FakeGCPCredentials(
        token="ya29.x", service_account_email="kf@proj.iam.gserviceaccount.com"
    )
    _install_fake_google_auth(monkeypatch, credentials=creds)
    strat = GCPServiceAccount()
    kwargs = strat.client_kwargs()
    assert kwargs == {"credentials": creds}


# ---------------------------------------------------------------------------
# AWSSigV4 strategy
# ---------------------------------------------------------------------------


class _FakeBoto3Credentials:
    def __init__(
        self, access_key: str, secret_key: str, token: str | None = None
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token

    def get_frozen_credentials(self) -> _FakeBoto3Credentials:
        return self


class _FakeStsClient:
    def __init__(
        self, arn: str = "arn:aws:iam::123456789012:user/kinoforge-ci"
    ) -> None:
        self._arn = arn

    def get_caller_identity(self) -> dict[str, str]:
        return {"Arn": self._arn, "Account": "123456789012", "UserId": "AIDA..."}


class _FakeBoto3Session:
    def __init__(
        self,
        credentials: _FakeBoto3Credentials | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        self._credentials = credentials
        self.region_name = region_name

    def get_credentials(self) -> _FakeBoto3Credentials | None:
        return self._credentials

    def client(self, service_name: str, region_name: str | None = None) -> object:
        if service_name == "sts":
            return _FakeStsClient()
        raise NotImplementedError(
            f"FakeBoto3Session: no fake for service {service_name!r}"
        )


def _install_fake_boto3(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeBoto3Session,
) -> None:
    import sys
    import types

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.Session = lambda profile_name=None: session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)


def test_sigv4_credentials_present_true_when_session_has_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import AWSSigV4

    creds = _FakeBoto3Credentials("AKIATESTKEY", "secret")
    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=creds))
    strat = AWSSigV4(region_name="us-east-1")
    assert strat.credentials_present() is True


def test_sigv4_credentials_present_false_when_session_has_no_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import AWSSigV4

    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=None))
    strat = AWSSigV4(region_name="us-east-1")
    assert strat.credentials_present() is False


def test_sigv4_health_check_ok_via_sts_get_caller_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import AWSSigV4

    creds = _FakeBoto3Credentials("AKIATESTKEY", "secret")
    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=creds))
    strat = AWSSigV4(region_name="us-east-1")
    result = strat.health_check()
    assert result.ok is True
    assert result.identity == "arn:aws:iam::123456789012:user/kinoforge-ci"


def test_sigv4_health_check_fail_when_no_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import AWSSigV4

    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=None))
    strat = AWSSigV4(region_name="us-east-1")
    result = strat.health_check()
    assert result.ok is False
    assert "no AWS credentials" in (result.reason or "")


def test_sigv4_redact_patterns_includes_access_key_and_authz_signature() -> None:
    from kinoforge.core.auth import AWSSigV4

    strat = AWSSigV4(region_name="us-east-1")
    patterns = strat.redact_patterns()
    assert any(p.search("AKIAIOSFODNN7EXAMPLE") for p in patterns)
    assert any(
        p.search(
            "AWS4-HMAC-SHA256 Credential=AKIA.../20260607/us-east-1/bedrock/aws4_request"
        )
        for p in patterns
    )


def test_sigv4_apply_signs_request_with_authorization_and_amz_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import AWSSigV4, HttpRequest

    creds = _FakeBoto3Credentials("AKIATESTKEY", "VerySecretKey123")
    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=creds))
    strat = AWSSigV4(region_name="us-east-1", service_name="bedrock-runtime")
    req = HttpRequest(
        method="POST",
        url="https://bedrock-runtime.us-east-1.amazonaws.com/async-invoke",
        headers={"Content-Type": "application/json"},
        body=b'{"foo": "bar"}',
    )
    out = strat.apply(req)
    assert out.headers["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIATESTKEY/"
    )
    assert "X-Amz-Date" in out.headers


def test_sigv4_client_kwargs_returns_aws_credential_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.core.auth import AWSSigV4

    creds = _FakeBoto3Credentials(
        "AKIATESTKEY", "VerySecretKey123", token="session-token-abc"
    )
    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=creds))
    strat = AWSSigV4(region_name="us-east-1")
    kwargs = strat.client_kwargs()
    assert kwargs["aws_access_key_id"] == "AKIATESTKEY"
    assert kwargs["aws_secret_access_key"] == "VerySecretKey123"
    assert kwargs["aws_session_token"] == "session-token-abc"
    assert kwargs["region_name"] == "us-east-1"


# ---------------------------------------------------------------------------
# build_auth_strategy registry
# ---------------------------------------------------------------------------


def test_registry_builds_bearer() -> None:
    from kinoforge.core.auth import Bearer, build_auth_strategy

    strat = build_auth_strategy({"strategy": "bearer", "env_var": "FAL_KEY"})
    assert isinstance(strat, Bearer)


def test_registry_builds_gcp_service_account() -> None:
    from kinoforge.core.auth import GCPServiceAccount, build_auth_strategy

    strat = build_auth_strategy({"strategy": "gcp_service_account"})
    assert isinstance(strat, GCPServiceAccount)


def test_registry_builds_aws_sigv4() -> None:
    from kinoforge.core.auth import AWSSigV4, build_auth_strategy

    strat = build_auth_strategy({"strategy": "aws_sigv4", "region_name": "us-east-1"})
    assert isinstance(strat, AWSSigV4)


def test_registry_unknown_strategy_raises_unknown_adapter() -> None:
    from kinoforge.core.auth import build_auth_strategy
    from kinoforge.core.errors import UnknownAdapter

    with pytest.raises(UnknownAdapter, match="not_a_real_strategy"):
        build_auth_strategy({"strategy": "not_a_real_strategy"})


def test_registry_missing_strategy_key_raises_keyerror() -> None:
    from kinoforge.core.auth import build_auth_strategy

    with pytest.raises(KeyError, match="strategy"):
        build_auth_strategy({"env_var": "FAL_KEY"})


def test_registry_passes_through_strategy_specific_kwargs() -> None:
    from kinoforge.core.auth import Bearer, build_auth_strategy

    strat = build_auth_strategy(
        {
            "strategy": "bearer",
            "env_var": "HF_TOKEN",
            "scheme": "Token",
            "header_name": "X-Api-Key",
        }
    )
    assert isinstance(strat, Bearer)
    assert strat._scheme == "Token"
    assert strat._header_name == "X-Api-Key"
