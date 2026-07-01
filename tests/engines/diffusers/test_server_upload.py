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


def test_upload_rejects_wrong_content_type(client: TestClient) -> None:
    """text/plain must be rejected with 415."""
    resp = client.put(
        "/upload",
        content=b"hello",
        headers={"Content-Type": "text/plain", "X-Filename": "x.mp4"},
    )
    assert resp.status_code == 415


def test_upload_rejects_oversize(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Body larger than the cap → 413 and no published file under upload dir."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setattr(srv, "_UPLOAD_MAX_BYTES", 1024)
    body = _mp4_bytes(2048)
    resp = client.put(
        "/upload",
        content=body,
        headers={"Content-Type": "video/mp4", "X-Filename": "big.mp4"},
    )
    assert resp.status_code == 413
    upload_dir = tmp_path / "kf-uploads"
    leftover = list(upload_dir.iterdir()) if upload_dir.exists() else []
    assert not any(p.name == "big.mp4" for p in leftover)


def test_upload_atomic_publish_no_partial(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-flight abort surrogate (oversize) leaves no published file at advertised name.

    Bug caught: skipping ``os.replace`` and writing directly to the published path
    would leave a half-written file at the advertised name when an abort fires.
    This test confirms only the ``.part`` tempfile (or nothing) survives the abort.
    """
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setattr(srv, "_UPLOAD_MAX_BYTES", 1024)
    resp = client.put(
        "/upload",
        content=_mp4_bytes(2048),
        headers={"Content-Type": "video/mp4", "X-Filename": "atom.mp4"},
    )
    assert resp.status_code == 413
    upload_dir = tmp_path / "kf-uploads"
    if upload_dir.exists():
        for p in upload_dir.iterdir():
            assert p.suffix == ".part" or not p.name.startswith("atom"), p
