"""Still-image input on the pod: /upload content types, /upscale media dispatch."""

from __future__ import annotations

import hashlib
import importlib
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def srv_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Fresh server with CUDA bypassed; yields (srv, client, fake_pipe, gpu_calls, out_png)."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    importlib.reload(srv)

    import imageio.v3 as iio

    out_png = tmp_path / "out.upscaled.png"
    iio.imwrite(out_png, np.zeros((16, 16, 3), dtype=np.uint8))
    out_mp4 = tmp_path / "out.mp4"
    out_mp4.write_bytes(b"\x00" * 64)

    fake_pipe = MagicMock(name="SpandrelPipe")
    fake_pipe.upscale = MagicMock(return_value=out_mp4)
    fake_pipe.upscale_image = MagicMock(return_value=out_png)
    fake_loaded = {
        "name": "spandrel-realesrgan-fp16",
        "pipe": fake_pipe,
        "vram_bytes": 1,
        "last_used_monotonic": 0.0,
        "on_device": "cuda",
    }
    gpu_calls: list[str] = []

    async def _fake_ensure_on_gpu(name: str) -> dict[str, Any]:
        gpu_calls.append(name)
        return fake_loaded

    monkeypatch.setattr(srv, "_ensure_on_gpu", _fake_ensure_on_gpu)
    monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(srv, "_UPLOAD_DIR", tmp_path / "kf-uploads")
    monkeypatch.setattr(srv, "LORAS_DIR", tmp_path / "loras")
    monkeypatch.setattr(srv, "_load_pipeline", lambda **_kw: MagicMock())
    monkeypatch.setattr(srv, "_pipe_arity", 1)
    with TestClient(srv.app) as client:
        yield srv, client, fake_pipe, gpu_calls, out_png


def _wait(client: TestClient, job_id: str, timeout_s: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        r = client.get(f"/upscale/status/{job_id}")
        if r.status_code == 200:
            last = r.json()
            if last.get("state") in {"done", "error"}:
                return last
        time.sleep(0.01)
    raise AssertionError(f"job never finished: {last}")


def _src_png(tmp_path: Path) -> Path:
    import imageio.v3 as iio

    p = tmp_path / "src.png"
    iio.imwrite(p, np.full((8, 8, 3), 7, dtype=np.uint8))
    return p


def _body(src: Path, media: str | None, engine: str = "spandrel") -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_url": f"file://{src}",
        "source_filename": src.name,
        "scale": "2x",
        "engine": engine,
        "spandrel": {"arch": "realesrgan", "precision": "fp16"},
        "flashvsr": {"debug_stats": False} if engine == "flashvsr" else None,
    }
    if media is not None:
        body["media"] = media
    return body


class TestSubmitRefusal:
    def test_image_with_non_spandrel_engine_is_400_before_any_load(
        self, srv_env: Any, tmp_path: Path
    ) -> None:
        # Bug caught: the refusal happens inside _run_upscale_job AFTER
        # _ensure_on_gpu — a FlashVSR load (minutes) for a request that was
        # always going to fail.
        srv, client, pipe, gpu_calls, _ = srv_env
        r = client.post(
            "/upscale", json=_body(_src_png(tmp_path), "image", engine="flashvsr")
        )
        assert r.status_code == 400
        assert "image" in r.json()["detail"]
        assert gpu_calls == []


class TestDispatch:
    def test_image_dispatches_to_upscale_image(
        self, srv_env: Any, tmp_path: Path
    ) -> None:
        # Bug caught: media is accepted and ignored — PNG bytes go through
        # pipe.upscale and the FFMPEG reader.
        srv, client, pipe, gpu_calls, out_png = srv_env
        r = client.post("/upscale", json=_body(_src_png(tmp_path), "image"))
        assert r.status_code == 200
        st = _wait(client, r.json()["job_id"])
        assert st["state"] == "done", st
        pipe.upscale_image.assert_called_once()
        pipe.upscale.assert_not_called()
        assert st["result"]["filename"] == out_png.name

    def test_default_media_dispatches_to_upscale(
        self, srv_env: Any, tmp_path: Path
    ) -> None:
        srv, client, pipe, gpu_calls, _ = srv_env
        r = client.post("/upscale", json=_body(_src_png(tmp_path), None))
        assert r.status_code == 200
        st = _wait(client, r.json()["job_id"])
        assert st["state"] == "done", st
        pipe.upscale.assert_called_once()
        pipe.upscale_image.assert_not_called()


class TestUploadContentTypes:
    @pytest.mark.parametrize(
        ("ctype", "name"),
        [
            ("image/png", "a1b2c3d4.png"),
            ("image/jpeg", "a1b2c3d4.jpg"),
            ("video/mp4", "a1b2c3d4.mp4"),
        ],
    )
    def test_accepted_types_store_under_their_name(
        self, srv_env: Any, ctype: str, name: str
    ) -> None:
        # Bug caught: /upload keeps the video/mp4-only gate and every image
        # upload is a 415 after the pod has booted.
        srv, client, *_ = srv_env
        body = b"\x89PNG\r\n" + bytes(range(64))
        r = client.put(
            "/upload", content=body, headers={"Content-Type": ctype, "X-Filename": name}
        )
        assert r.status_code == 200, r.text
        assert r.json()["path"].endswith(name)
        assert r.json()["sha256"] == hashlib.sha256(body).hexdigest()

    def test_other_types_are_still_415(self, srv_env: Any) -> None:
        # Bug caught: the gate was widened to "anything".
        srv, client, *_ = srv_env
        r = client.put(
            "/upload",
            content=b"hello",
            headers={"Content-Type": "text/plain", "X-Filename": "x.txt"},
        )
        assert r.status_code == 415
