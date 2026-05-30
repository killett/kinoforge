"""Tests for the env-backed credential provider."""

import pytest

from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.interfaces import CredentialProvider


def test_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIVITAI_TOKEN", "secret-123")
    # Bug this catches: returning a hardcoded value or stale cached env snapshot.
    assert EnvCredentialProvider().get("CIVITAI_TOKEN") == "secret-123"


def test_missing_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE_KEY", raising=False)
    # Bug this catches: raising KeyError or returning "" instead of None.
    assert EnvCredentialProvider().get("NOPE_KEY") is None


def test_is_a_credential_provider() -> None:
    # Bug this catches: forgetting to inherit, breaking adapter polymorphism.
    assert isinstance(EnvCredentialProvider(), CredentialProvider)
