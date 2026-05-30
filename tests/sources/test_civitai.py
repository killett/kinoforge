"""Tests for the CivitAI model source (AC 1–6)."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import kinoforge.sources.civitai  # noqa: F401  — registers the source on import
from kinoforge.core import registry
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError
from kinoforge.sources.civitai import CivitAISource

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_VERSION_PAYLOAD: dict[str, Any] = {
    "id": 5678,
    "modelId": 1234,
    "name": "v1",
    "files": [
        {
            "name": "model.safetensors",
            "sizeKB": 200.5,
            "downloadUrl": "https://civitai.com/api/download/models/5678",
            "hashes": {"SHA256": "ABC123DEF456"},
        }
    ],
}

_MODEL_PAYLOAD: dict[str, Any] = {
    "id": 1234,
    "modelVersions": [{"id": 5678}, {"id": 9999}],
}


class SpyFetch:
    """Injectable fetch that returns canned JSON and records calls."""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        """Initialise with a URL→payload mapping.

        Args:
            responses: Mapping from URL string to the dict to return.
        """
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._responses = responses

    def __call__(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        """Record the call and return the canned response for *url*.

        Args:
            url: The URL being fetched.
            headers: The HTTP headers passed by the caller.

        Returns:
            The canned dict for *url*.

        Raises:
            KeyError: If *url* is not in the canned responses (test bug).
        """
        self.calls.append((url, headers))
        return self._responses[url]


def _version_only_fetch() -> SpyFetch:
    """Return a spy that only knows about the version endpoint."""
    return SpyFetch(
        {"https://civitai.com/api/v1/model-versions/5678": _VERSION_PAYLOAD}
    )


def _model_and_version_fetch() -> SpyFetch:
    """Return a spy for the model endpoint and the version endpoint."""
    return SpyFetch(
        {
            "https://civitai.com/api/v1/models/1234": _MODEL_PAYLOAD,
            "https://civitai.com/api/v1/model-versions/5678": _VERSION_PAYLOAD,
        }
    )


def _make_creds(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> EnvCredentialProvider:
    """Return a credential provider backed by monkeypatched env.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        token: Token value, or None to delete the env var.

    Returns:
        An :class:`~kinoforge.core.credentials.EnvCredentialProvider`.
    """
    if token is None:
        monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CIVITAI_TOKEN", token)
    return EnvCredentialProvider()


# ---------------------------------------------------------------------------
# AC1 — handles()
# ---------------------------------------------------------------------------


def test_handles_version_ref() -> None:
    """handles() returns True for civitai:<id>@<vid>."""
    src = CivitAISource()
    # Bug this catches: regex accepting non-numeric segments, allowing typos like
    # "civitai:abc@123" to silently pass and produce a confusing downstream error.
    assert src.handles("civitai:1234@5678") is True


def test_handles_model_only_ref() -> None:
    """handles() returns True for civitai:<id> (no version)."""
    src = CivitAISource()
    assert src.handles("civitai:1234") is True


def test_handles_rejects_hf_scheme() -> None:
    """handles() returns False for a HuggingFace ref."""
    src = CivitAISource()
    # Bug: matching on "civitai" substring inside foreign refs.
    assert src.handles("hf:org/m") is False


def test_handles_rejects_non_numeric_model_id() -> None:
    """handles() returns False when the model ID is not digits."""
    src = CivitAISource()
    # Bug: accepting any colon-separated word as a valid civitai ref.
    assert src.handles("civitai:not-a-number") is False


# ---------------------------------------------------------------------------
# AC2 — resolve() with version ref returns Artifact with correct fields
# ---------------------------------------------------------------------------


def test_resolve_version_ref_returns_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve() with a versioned ref returns ≥1 Artifact with correct fields."""
    spy = _version_only_fetch()
    creds = _make_creds(monkeypatch, None)
    src = CivitAISource(fetch=spy)
    artifacts = src.resolve("civitai:1234@5678", creds)

    assert len(artifacts) >= 1
    a = artifacts[0]
    # Bug this catches: forgetting to set url, leaving it as empty string.
    assert a.url == "https://civitai.com/api/download/models/5678"
    # Bug: wrong field used for filename (e.g. "id" instead of "name").
    assert a.filename == "model.safetensors"
    # Bug: sha256 not lowercased, breaking downstream digest comparisons.
    assert a.sha256 == "abc123def456"


def test_resolve_version_ref_size_converted_from_kb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve() converts sizeKB to bytes (int)."""
    spy = _version_only_fetch()
    creds = _make_creds(monkeypatch, None)
    src = CivitAISource(fetch=spy)
    artifacts = src.resolve("civitai:1234@5678", creds)
    # 200.5 KB = 205312 bytes  (int(200.5 * 1024))
    assert artifacts[0].size == int(200.5 * 1024)


# ---------------------------------------------------------------------------
# AC3 — Authorization header attached iff token present
# ---------------------------------------------------------------------------


def test_resolve_sends_auth_header_when_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CIVITAI_TOKEN is set, fetch receives Authorization: Bearer <token>."""
    spy = _version_only_fetch()
    creds = _make_creds(monkeypatch, "my-secret-token")
    src = CivitAISource(fetch=spy)
    artifacts = src.resolve("civitai:1234@5678", creds)

    # Bug this catches: building the header but not passing it to fetch, or
    # building it only on the Artifact but not on the HTTP call.
    _url, headers = spy.calls[0]
    assert headers.get("Authorization") == "Bearer my-secret-token"

    # The Artifact itself should also carry the header for the downloader.
    assert artifacts[0].headers.get("Authorization") == "Bearer my-secret-token"


def test_resolve_sends_no_auth_header_when_token_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CIVITAI_TOKEN is absent, fetch receives no Authorization header."""
    spy = _version_only_fetch()
    creds = _make_creds(monkeypatch, None)
    src = CivitAISource(fetch=spy)
    src.resolve("civitai:1234@5678", creds)

    _url, headers = spy.calls[0]
    # Bug: always injecting an empty-string Authorization header.
    assert "Authorization" not in headers


def test_resolve_artifact_has_no_auth_header_when_token_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact.headers is empty dict when no token."""
    spy = _version_only_fetch()
    creds = _make_creds(monkeypatch, None)
    src = CivitAISource(fetch=spy)
    artifacts = src.resolve("civitai:1234@5678", creds)
    assert artifacts[0].headers == {}


# ---------------------------------------------------------------------------
# AC4 — model-only path calls /models first, then /model-versions
# ---------------------------------------------------------------------------


def test_model_only_ref_calls_both_endpoints_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """civitai:<modelId> hits /models/{id} then /model-versions/{vid}."""
    spy = _model_and_version_fetch()
    creds = _make_creds(monkeypatch, None)
    src = CivitAISource(fetch=spy)
    artifacts = src.resolve("civitai:1234", creds)

    urls_called = [c[0] for c in spy.calls]
    # Bug this catches: going straight to /model-versions without first resolving
    # the model-only ref through the /models endpoint.
    assert urls_called[0] == "https://civitai.com/api/v1/models/1234"
    assert urls_called[1] == "https://civitai.com/api/v1/model-versions/5678"
    assert len(artifacts) >= 1


def test_model_only_ref_picks_first_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model-only path picks the FIRST entry in modelVersions[].id."""
    spy = _model_and_version_fetch()
    creds = _make_creds(monkeypatch, None)
    src = CivitAISource(fetch=spy)
    src.resolve("civitai:1234", creds)

    # second call must be to the FIRST version id (5678), not the second (9999)
    second_url = spy.calls[1][0]
    assert "5678" in second_url
    assert "9999" not in second_url


# ---------------------------------------------------------------------------
# AC5 — AuthError propagates
# ---------------------------------------------------------------------------


def test_resolve_reraises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fetch that raises AuthError is re-raised (not swallowed)."""

    def _failing_fetch(url: str, headers: dict[str, str]) -> dict[str, Any]:
        raise AuthError("401 Unauthorized")

    creds = _make_creds(monkeypatch, None)
    src = CivitAISource(fetch=_failing_fetch)
    # Bug this catches: wrapping all exceptions in a generic KinoforgeError,
    # losing the AuthError subtype that callers need to distinguish cred issues.
    with pytest.raises(AuthError):
        src.resolve("civitai:1234@5678", creds)


# ---------------------------------------------------------------------------
# AC6 — self-registration on import
# ---------------------------------------------------------------------------


def test_self_registers_on_import() -> None:
    """Importing kinoforge.sources.civitai registers the source under 'civitai'."""
    # Force a fresh import (module already loaded at top of file, so registry is populated).
    # After reload, the class identity changes so we check the scheme and handles() instead
    # of isinstance() — both confirm the correct source type is registered.
    importlib.reload(kinoforge.sources.civitai)
    src = registry.source_for_ref("civitai:1@2")
    # Bug this catches: self-registration being conditional on a flag, so repeated
    # imports leave the registry empty.
    assert src.scheme == "civitai"
    assert src.handles("civitai:1@2") is True


def test_scheme_attribute_is_civitai() -> None:
    """scheme class attribute is 'civitai'."""
    assert CivitAISource.scheme == "civitai"
