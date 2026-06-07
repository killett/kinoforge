"""Layer 1 — AuthStrategy ABC + typed boundary objects."""

from __future__ import annotations

import dataclasses

import pytest

from kinoforge.core.auth import (
    AuthStrategy,
    HealthResult,
    HttpRequest,
)

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
