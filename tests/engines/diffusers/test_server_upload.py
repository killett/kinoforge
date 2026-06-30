"""PUT /upload route — streaming, sanitization, rejection, atomicity."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a TestClient with /tmp/kf-uploads redirected to tmp_path."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setattr(srv, "_UPLOAD_DIR", tmp_path / "kf-uploads")
    return TestClient(srv.app)


def _mp4_bytes(size: int = 4096) -> bytes:
    """Return ``size`` deterministic bytes for upload tests."""
    return bytes(i % 256 for i in range(size))


def test_upload_writes_under_upload_dir(client: TestClient, tmp_path: Path) -> None:
    """PUT /upload streams body into _UPLOAD_DIR and returns matching sha256."""
    body = _mp4_bytes(8192)
    expected_sha = hashlib.sha256(body).hexdigest()
    resp = client.put(
        "/upload",
        content=body,
        headers={"Content-Type": "video/mp4", "X-Filename": "src.mp4"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["sha256"] == expected_sha
    assert payload["size"] == 8192
    written = Path(payload["path"])
    assert written.read_bytes() == body
    assert (tmp_path / "kf-uploads") in written.parents


def test_upload_sanitizes_filename(client: TestClient, tmp_path: Path) -> None:
    """X-Filename: ../../etc/passwd must land inside _UPLOAD_DIR, basename only."""
    body = _mp4_bytes(1024)
    resp = client.put(
        "/upload",
        content=body,
        headers={"Content-Type": "video/mp4", "X-Filename": "../../etc/passwd"},
    )
    assert resp.status_code == 200
    written = Path(resp.json()["path"])
    assert (tmp_path / "kf-uploads") in written.parents
    assert "/etc/" not in str(written)
    assert written.name == "passwd"


def test_upload_falls_back_on_empty_filename(client: TestClient) -> None:
    """Missing X-Filename → server generates random <hex>.mp4 fallback."""
    body = _mp4_bytes(1024)
    resp = client.put(
        "/upload",
        content=body,
        headers={"Content-Type": "video/mp4"},
    )
    assert resp.status_code == 200
    written = Path(resp.json()["path"])
    assert written.name.endswith(".mp4")
    assert len(written.stem) == 8  # token_hex(4) → 8 hex chars
