"""Behavior of the shared HTTP helpers.

Pins the 4 kinoforge-internal patterns documented in the harness README:
UA header on every request, ?api_key= suffix when RUNPOD_API_KEY is set,
URLError retry budget, HTTPError no-retry passthrough.
"""

from __future__ import annotations

import email.message
import json
import urllib.error
from typing import Any
from unittest.mock import patch

import pytest

from tests._smoke_harness import http


class _FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def test_post_json_sends_kinoforge_smoke_ua(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper sends Python-urllib default UA → Cloudflare 403."""
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    captured: dict[str, Any] = {}

    def _fake_urlopen(req: Any, timeout: int | None = None) -> _FakeResponse:  # noqa: ARG001
        captured["ua"] = req.get_header("User-agent")
        return _FakeResponse({"ok": True})

    with patch("urllib.request.urlopen", _fake_urlopen):
        http.post_json("http://localhost:8000/x", {}, timeout=5)
    assert captured["ua"] == "kinoforge-smoke/0.1"


def test_post_json_appends_api_key_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper forgets the api_key suffix → RunPod proxy 403."""
    monkeypatch.setenv("RUNPOD_API_KEY", "secret-xyz")
    captured: dict[str, Any] = {}

    def _fake_urlopen(req: Any, timeout: int | None = None) -> _FakeResponse:  # noqa: ARG001
        captured["url"] = req.full_url
        return _FakeResponse({"ok": True})

    with patch("urllib.request.urlopen", _fake_urlopen):
        http.get_json("https://pod.proxy.runpod.net/lora/inventory", timeout=5)
    assert "api_key=secret-xyz" in captured["url"]


def test_no_api_key_suffix_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper appends an empty api_key= → confuses local Tier-1 server."""
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    captured: dict[str, Any] = {}

    def _fake_urlopen(req: Any, timeout: int | None = None) -> _FakeResponse:  # noqa: ARG001
        captured["url"] = req.full_url
        return _FakeResponse({"ok": True})

    with patch("urllib.request.urlopen", _fake_urlopen):
        http.get_json("http://localhost:8000/lora/inventory", timeout=5)
    assert "api_key" not in captured["url"]


def test_api_key_uses_ampersand_when_url_has_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper unconditionally prepends '?' → produces malformed URL."""
    monkeypatch.setenv("RUNPOD_API_KEY", "k")
    captured: dict[str, Any] = {}

    def _fake_urlopen(req: Any, timeout: int | None = None) -> _FakeResponse:  # noqa: ARG001
        captured["url"] = req.full_url
        return _FakeResponse({"ok": True})

    with patch("urllib.request.urlopen", _fake_urlopen):
        http.get_json("https://x/y?existing=1", timeout=5)
    assert "?existing=1&api_key=k" in captured["url"]


def test_url_error_retries_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper retries forever / not at all / on the wrong exception."""
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    calls: list[int] = []

    def _flaky(req: Any, timeout: int | None = None) -> _FakeResponse:  # noqa: ARG001
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("connection reset")
        return _FakeResponse({"ok": True})

    sleeps: list[float] = []
    monkeypatch.setattr(http, "_sleep", lambda s: sleeps.append(s))
    with patch("urllib.request.urlopen", _flaky):
        out = http.get_json("http://x/y", timeout=5)
    assert out == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [0.5, 1.5]


def test_url_error_gives_up_after_3_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper retries forever, hanging CI."""
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    def _always_fails(req: Any, timeout: int | None = None) -> _FakeResponse:  # noqa: ARG001
        raise urllib.error.URLError("dead")

    monkeypatch.setattr(http, "_sleep", lambda s: None)
    with patch("urllib.request.urlopen", _always_fails):
        with pytest.raises(urllib.error.URLError):
            http.get_json("http://x/y", timeout=5)


def test_http_error_propagates_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper retries 4xx → masks real auth errors as transient."""
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    calls: list[int] = []

    def _http_403(req: Any, timeout: int | None = None) -> _FakeResponse:  # noqa: ARG001
        calls.append(1)
        raise urllib.error.HTTPError(
            req.full_url,
            403,
            "Forbidden",
            email.message.Message(),
            None,
        )

    monkeypatch.setattr(http, "_sleep", lambda s: None)
    with patch("urllib.request.urlopen", _http_403):
        with pytest.raises(urllib.error.HTTPError):
            http.get_json("http://x/y", timeout=5)
    assert len(calls) == 1


def test_post_json_serialises_body_and_returns_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper returns raw bytes / forgets Content-Type."""
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    captured: dict[str, Any] = {}

    def _capture(req: Any, timeout: int | None = None) -> _FakeResponse:  # noqa: ARG001
        captured["body"] = req.data
        captured["content_type"] = req.get_header("Content-type")
        return _FakeResponse({"echoed": True})

    with patch("urllib.request.urlopen", _capture):
        out = http.post_json("http://x/y", {"hello": "world"}, timeout=5)
    assert json.loads(captured["body"]) == {"hello": "world"}
    assert captured["content_type"] == "application/json"
    assert out == {"echoed": True}
