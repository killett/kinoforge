"""`fetch_artifact_bytes`: one reader for pod-proxy URLs and file:// uris."""

from __future__ import annotations

import io
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.interfaces import Artifact
from kinoforge.core.orchestrator import fetch_artifact_bytes


def test_file_uri_reads_bytes(tmp_path: Path) -> None:
    p = tmp_path / "up.png"
    p.write_bytes(b"png-bytes")
    assert fetch_artifact_bytes(Artifact(uri=f"file://{p}")) == b"png-bytes"


def test_http_uri_gets_with_kinoforge_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug caught: the default Python-urllib UA is 403'd by RunPod's
    # Cloudflare edge; a missing timeout hangs on a dead pod forever.
    seen: dict[str, Any] = {}

    class _Resp(io.BytesIO):
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: Any) -> None:
            pass

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> _Resp:
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return _Resp(b"body")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = fetch_artifact_bytes(
        Artifact(uri="https://pod-8000.proxy.runpod.net/artifacts/x.png")
    )
    assert out == b"body"
    assert seen["url"].endswith("/artifacts/x.png")
    assert seen["ua"] == "kinoforge-orchestrator/0.1"
    assert seen["timeout"] == 600
