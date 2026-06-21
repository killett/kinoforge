"""_download_one User-Agent header — Civitai Cloudflare 403 dodge."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from kinoforge.engines.diffusers.servers import wan_t2v_server as s


def _spec(url: str) -> Any:
    return s.ArtifactDownloadSpec(
        url=url,
        headers={"Authorization": "Bearer civitai-token"},
        filename="model.safetensors",
        size_hint=1024,
    )


class _FakeResp:
    def __init__(self, body: bytes = b"") -> None:
        self._body = body

    def read(self, *_a: Any, **_k: Any) -> bytes:
        out, self._body = self._body, b""
        return out

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


def test_download_one_sends_kinoforge_pod_ua(tmp_path: Path) -> None:
    """Bug: server-side download omits UA → Civitai Cloudflare 403 →
    /lora/set_stack returns 502 with no path to recovery (same class
    as the kinoforge-source civitai fix in commit 53a1e6e).
    """
    captured: dict[str, Any] = {}

    def _fake_urlopen(req: Any) -> _FakeResp:
        captured["ua"] = req.get_header("User-agent")
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp(b"weights")

    with patch("urllib.request.urlopen", _fake_urlopen):
        path, n = s._download_one(
            _spec("https://civitai.com/api/download/models/1"), tmp_path
        )
    assert captured["ua"] is not None
    assert "kinoforge" in captured["ua"].lower()
    # Operator-supplied Authorization header MUST passthrough — fixing
    # UA must not silently drop other request headers.
    assert captured["auth"] == "Bearer civitai-token"
    assert n == len(b"weights")
    assert Path(path).exists()


def test_download_one_does_not_override_caller_user_agent(tmp_path: Path) -> None:
    """Bug: helper forces its UA even when the spec already carries one,
    masking operator-set UAs for vendor sources that gate on a specific
    string. Spec UA wins.
    """
    captured: dict[str, Any] = {}

    def _fake_urlopen(req: Any) -> _FakeResp:
        captured["ua"] = req.get_header("User-agent")
        return _FakeResp(b"")

    spec = s.ArtifactDownloadSpec(
        url="https://x/y.safetensors",
        headers={"User-Agent": "custom-vendor-ua/2.0"},
        filename="y.safetensors",
        size_hint=0,
    )
    with patch("urllib.request.urlopen", _fake_urlopen):
        s._download_one(spec, tmp_path)
    assert captured["ua"] == "custom-vendor-ua/2.0"
