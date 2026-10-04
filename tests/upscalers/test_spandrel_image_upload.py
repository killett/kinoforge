"""Still-image input through SpandrelEngine: upload headers + /upscale payload."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kinoforge.core.interfaces import Artifact, Instance, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget


def _instance() -> Instance:
    return Instance(
        id="pod-fake",
        provider="fake",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://pod.example/proxy"},
        tags={},
    )


def _cfg() -> dict[str, object]:
    return {
        "upscale": {
            "engine": "spandrel",
            "scale": "2x",
            "spandrel": {
                "model_url": "hf:ai-forever/Real-ESRGAN/RealESRGAN_x2.pth",
                "arch": "realesrgan",
                "precision": "fp16",
                "tile_size": 512,
                "batch_size": 4,
            },
        },
    }


def _job(uri: str, media: str = "video") -> UpscaleJob:
    return UpscaleJob(
        source=Artifact(uri=uri, sha256="0" * 64, size=1),
        scale=ScaleTarget(kind="factor", value=2.0),
        media=media,  # type: ignore[arg-type]
    )


def _file(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(bytes(i % 256 for i in range(4096)))
    return p


def _capture_put(engine: Any) -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def fake_put(
        url: str, data: Any, headers: dict[str, str], timeout: int
    ) -> dict[str, Any]:
        seen["headers"] = dict(headers)
        body = data.read()
        return {
            "path": f"/tmp/kf-uploads/{headers['X-Filename']}",
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }

    return patch.object(engine, "_put_upload", side_effect=fake_put), seen


class TestUploadHeaders:
    @pytest.mark.parametrize(
        ("name", "ctype", "suffix"),
        [
            ("in.png", "image/png", ".png"),
            ("in.jpg", "image/jpeg", ".jpg"),
            ("in.JPEG", "image/jpeg", ".jpeg"),
        ],
    )
    def test_image_headers_follow_the_suffix(
        self, tmp_path: Path, name: str, ctype: str, suffix: str
    ) -> None:
        # Bug caught: the pod's /upload sees video/mp4 for a PNG body and
        # stores it under <sha8>.mp4 — the runtime then FFMPEG-decodes a PNG.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        engine = SpandrelEngine()
        src = _file(tmp_path, name)
        patcher, seen = _capture_put(engine)
        with patcher:
            url = engine._upload_source(_instance(), src, media="image")
        sha8 = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
        assert seen["headers"]["Content-Type"] == ctype
        assert seen["headers"]["X-Filename"] == f"{sha8}{suffix}"
        assert url.endswith(suffix)

    def test_video_headers_unchanged(self, tmp_path: Path) -> None:
        # Bug caught: the media branch changes the proven video header pair.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        engine = SpandrelEngine()
        src = _file(tmp_path, "in.mp4")
        patcher, seen = _capture_put(engine)
        with patcher:
            engine._upload_source(_instance(), src)
        sha8 = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
        assert seen["headers"]["Content-Type"] == "video/mp4"
        assert seen["headers"]["X-Filename"] == f"{sha8}.mp4"

    def test_image_with_unknown_suffix_raises_before_http(self, tmp_path: Path) -> None:
        from kinoforge.upscalers.spandrel import SpandrelEngine

        engine = SpandrelEngine()
        src = _file(tmp_path, "in.gif")
        patcher, seen = _capture_put(engine)
        with patcher, pytest.raises(ValueError, match="png"):
            engine._upload_source(_instance(), src, media="image")
        assert "headers" not in seen


class TestUpscalePayload:
    def _drive(self, media: str, src: Path) -> tuple[dict[str, Any], Any, Any]:
        from kinoforge.upscalers.spandrel import SpandrelEngine
        from kinoforge.upscalers.spandrel import _engine as spandrel_mod

        engine = SpandrelEngine()
        captured: dict[str, Any] = {}

        def fake_http(
            *, method: str, url: str, payload: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            if method == "POST":
                assert payload is not None
                captured.update(payload)
                return {"job_id": "j-img"}
            return {
                "state": "done",
                "progress": 1.0,
                "error": None,
                "result": {
                    "filename": "out.upscaled.png",
                    "sha256": "z",
                    "size": 1,
                    "input_resolution": [8, 8],
                    "output_resolution": [16, 16],
                    "engine_meta": {},
                },
            }

        with (
            patch.object(
                engine, "_upload_source", return_value="file:///tmp/kf-uploads/up.bin"
            ) as upl,
            patch.object(spandrel_mod, "_http_json", side_effect=fake_http),
        ):
            result = engine.upscale(_instance(), _job(f"file://{src}", media), _cfg())
        return captured, upl, result

    def test_image_job_forwards_media_everywhere(self, tmp_path: Path) -> None:
        # Bug caught: media reaches the upload but not the payload (or vice
        # versa), or the result artifact loses it so the orchestrator
        # publishes the PNG as .mp4.
        captured, upl, result = self._drive("image", _file(tmp_path, "in.png"))
        assert upl.call_args.kwargs["media"] == "image"
        assert captured["media"] == "image"
        assert result.artifact.meta["media"] == "image"

    def test_video_job_sends_media_video(self, tmp_path: Path) -> None:
        captured, upl, result = self._drive("video", _file(tmp_path, "in.mp4"))
        assert upl.call_args.kwargs["media"] == "video"
        assert captured["media"] == "video"
        assert result.artifact.meta["media"] == "video"
