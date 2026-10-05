"""servers/_upload.py: the PUT /upload contract _upload_source cross-checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from kinoforge.engines.diffusers.servers import _upload

_TYPES = frozenset({"image/png", "image/jpeg"})


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    upload_dir = tmp_path / "uploads"

    @app.put("/upload")
    async def handler(request: Request) -> dict:  # type: ignore[type-arg]
        return await _upload.receive_upload(
            request,
            upload_dir=upload_dir,
            content_types=_TYPES,
            max_bytes=4096,
            fallback_suffix=".png",
        )

    return TestClient(app)


def test_upload_streams_body_and_reports_sha(
    client: TestClient, tmp_path: Path
) -> None:
    """Bug caught: sha computed over the tempfile name or a truncated body —
    _upload_source raises UploadIntegrityError on any mismatch."""
    body = bytes(i % 256 for i in range(2048))
    resp = client.put(
        "/upload",
        content=body,
        headers={"Content-Type": "image/png", "X-Filename": "a1b2c3d4.png"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()
    assert payload["size"] == 2048
    written = Path(payload["path"])
    assert written.read_bytes() == body
    assert written.parent == tmp_path / "uploads"
    assert written.name == "a1b2c3d4.png"


def test_wrong_content_type_is_415(client: TestClient) -> None:
    """Bug caught: the text server silently accepting an mp4 it cannot use."""
    resp = client.put(
        "/upload",
        content=b"xx",
        headers={"Content-Type": "video/mp4", "X-Filename": "a.mp4"},
    )
    assert resp.status_code == 415


def test_filename_is_reduced_to_a_safe_basename(
    client: TestClient, tmp_path: Path
) -> None:
    """Bug caught: path traversal via X-Filename."""
    resp = client.put(
        "/upload",
        content=b"xx",
        headers={"Content-Type": "image/png", "X-Filename": "../../etc/passwd"},
    )
    assert resp.status_code == 200
    written = Path(resp.json()["path"])
    assert written.name == "passwd"
    assert written.parent == tmp_path / "uploads"


def test_missing_filename_gets_a_random_png_name(client: TestClient) -> None:
    """Bug caught: an empty basename → os.replace onto the directory itself."""
    resp = client.put("/upload", content=b"xx", headers={"Content-Type": "image/png"})
    assert resp.status_code == 200
    assert Path(resp.json()["path"]).name.endswith(".png")


def test_oversize_body_is_413_and_leaves_no_partial(
    client: TestClient, tmp_path: Path
) -> None:
    """Bug caught: the .part tempfile surviving a rejected upload and filling the pod disk."""
    resp = client.put(
        "/upload",
        content=b"x" * 5000,
        headers={"Content-Type": "image/png", "X-Filename": "big.png"},
    )
    assert resp.status_code == 413
    assert list((tmp_path / "uploads").glob("*.part")) == []
    assert not (tmp_path / "uploads" / "big.png").exists()


def test_sanitize_filename_rules() -> None:
    """Bug caught: a cleaned-to-empty name returned as '' instead of the fallback."""
    assert (
        _upload.sanitize_filename("dir/sub/ok-1.PNG", fallback_suffix=".png")
        == "ok-1.PNG"
    )
    assert _upload.sanitize_filename("///", fallback_suffix=".png").endswith(".png")
    assert _upload.sanitize_filename(None, fallback_suffix=".jpg").endswith(".jpg")
