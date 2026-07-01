"""SpandrelEngine._upload_source + upscale() file:// dispatch."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from kinoforge.core.errors import UploadIntegrityError
from kinoforge.core.interfaces import Artifact, Instance, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget


@pytest.fixture
def mp4(tmp_path: Path) -> Path:
    """Write a 16 KiB deterministic mp4 stub and return its path."""
    p = tmp_path / "src.mp4"
    p.write_bytes(bytes(i % 256 for i in range(16 * 1024)))
    return p


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
                "model_url": "hf:lllyasviel/realesrgan/RealESRGAN_x2plus.pth",
                "arch": "realesrgan",
                "precision": "fp16",
                "tile_size": 512,
                "batch_size": 4,
            },
        },
    }


def _job(uri: str) -> UpscaleJob:
    return UpscaleJob(
        source=Artifact(uri=uri, sha256="0" * 64, size=1),
        scale=ScaleTarget(kind="factor", value=2.0),
    )


def test_upload_source_happy_path(mp4: Path) -> None:
    """Successful upload returns file:// URL pointing at server-reported path.

    Bug caught: the helper returns the local path back unchanged or the raw
    server path without the file:// prefix → /upscale rejects the URI scheme
    or the wan_t2v_server downloader treats it as a relative path.
    """
    from kinoforge.upscalers.spandrel import SpandrelEngine

    expected_sha = hashlib.sha256(mp4.read_bytes()).hexdigest()
    server_path = "/tmp/kf-uploads/abcd1234.mp4"

    def fake_putter(
        url: str, data: object, headers: dict[str, str], timeout: int
    ) -> dict[str, object]:
        body = data.read()  # type: ignore[attr-defined]
        assert len(body) == mp4.stat().st_size
        assert headers["Content-Type"] == "video/mp4"
        return {"path": server_path, "size": len(body), "sha256": expected_sha}

    engine = SpandrelEngine()
    with patch.object(engine, "_put_upload", side_effect=fake_putter) as putter:
        url = engine._upload_source(_instance(), mp4)
    assert url == f"file://{server_path}"
    assert putter.call_count == 1


def test_upload_source_integrity_mismatch(mp4: Path) -> None:
    """Server sha256 != local sha256 → UploadIntegrityError with both hashes.

    Bug caught: helper drops the integrity check and returns the file:// URL
    even though server-reported sha disagrees → silent data corruption during
    upscale; output is junk and operator has no signal to retry.
    """
    from kinoforge.upscalers.spandrel import SpandrelEngine

    engine = SpandrelEngine()
    bad_sha = "0" * 64
    with patch.object(
        engine,
        "_put_upload",
        return_value={
            "path": "/tmp/kf-uploads/x.mp4",
            "size": mp4.stat().st_size,
            "sha256": bad_sha,
        },
    ):
        with pytest.raises(UploadIntegrityError) as exc_info:
            engine._upload_source(_instance(), mp4)
    assert exc_info.value.server_sha256 == bad_sha
    assert exc_info.value.local_sha256 != bad_sha


def test_upload_source_502_recovers(mp4: Path) -> None:
    """First PUT 502 → retry once succeeds.

    Bug caught: helper does not retry the proxy cold-warmup 502 → first
    upscale on a freshly-booted pod always 502s and the operator must
    re-fire the smoke. Mirror the /lora/set_stack startup-window retry.
    """
    from urllib.error import HTTPError

    from kinoforge.upscalers.spandrel import SpandrelEngine

    expected_sha = hashlib.sha256(mp4.read_bytes()).hexdigest()
    calls = {"n": 0}

    def flaky_putter(
        url: str, data: object, headers: dict[str, str], timeout: int
    ) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPError(url, 502, "Bad Gateway", hdrs=None, fp=None)  # type: ignore[arg-type]
        body = data.read()  # type: ignore[attr-defined]
        return {
            "path": "/tmp/kf-uploads/y.mp4",
            "size": len(body),
            "sha256": expected_sha,
        }

    engine = SpandrelEngine()
    with patch.object(engine, "_put_upload", side_effect=flaky_putter):
        url = engine._upload_source(_instance(), mp4)
    assert url.startswith("file:///tmp/kf-uploads/")
    assert calls["n"] == 2


def test_upscale_passes_through_http_source() -> None:
    """job.source.uri https:// → no upload helper called; submit URL unchanged.

    Bug caught: scheme dispatch wrong way around — every source uploads,
    including https:// URLs the pod can already fetch via its existing
    downloader. Wastes bandwidth and breaks idempotency.
    """
    from kinoforge.upscalers.spandrel import SpandrelEngine
    from kinoforge.upscalers.spandrel import _engine as spandrel_mod

    engine = SpandrelEngine()
    captured: dict[str, object] = {}

    def fake_http(
        *, method: str, url: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        if method == "POST":
            assert payload is not None
            captured.update(payload)
            return {"job_id": "j-https"}
        return {
            "state": "done",
            "progress": 1.0,
            "result": {
                "filename": "out.mp4",
                "sha256": "z",
                "size": 1,
                "input_resolution": [64, 48],
                "output_resolution": [128, 96],
                "engine_meta": {},
            },
            "error": None,
        }

    with (
        patch.object(engine, "_upload_source") as upl,
        patch.object(spandrel_mod, "_http_json", side_effect=fake_http),
    ):
        engine.upscale(_instance(), _job("https://example.com/x.mp4"), _cfg())
    upl.assert_not_called()
    assert captured["source_url"] == "https://example.com/x.mp4"


def test_upscale_uploads_bare_absolute_path_source(mp4: Path) -> None:
    """job.source.uri = bare /abs/path (no scheme) → upload helper fires.

    Bug caught: multi-stage warm-reuse (Wan T2V stage-1 → spandrel stage-2)
    hands SpandrelEngine a bare local path because the store returns the
    stored file's path without a ``file://`` prefix. Dispatch that only
    checks ``.startswith("file://")`` would pass the bare path through and
    the pod's ``_download_to_local_temp`` raises ``unknown url type``.
    """
    from kinoforge.upscalers.spandrel import SpandrelEngine
    from kinoforge.upscalers.spandrel import _engine as spandrel_mod

    engine = SpandrelEngine()
    captured: dict[str, object] = {}

    def fake_http(
        *, method: str, url: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        if method == "POST":
            assert payload is not None
            captured.update(payload)
            return {"job_id": "j-bare"}
        return {
            "state": "done",
            "progress": 1.0,
            "result": {
                "filename": "out.mp4",
                "sha256": "z",
                "size": 1,
                "input_resolution": [64, 48],
                "output_resolution": [128, 96],
                "engine_meta": {},
            },
            "error": None,
        }

    with (
        patch.object(
            engine, "_upload_source", return_value="file:///tmp/kf-uploads/bare.mp4"
        ) as upl,
        patch.object(spandrel_mod, "_http_json", side_effect=fake_http),
    ):
        engine.upscale(_instance(), _job(str(mp4)), _cfg())
    upl.assert_called_once()
    assert upl.call_args.args[1] == mp4
    assert captured["source_url"] == "file:///tmp/kf-uploads/bare.mp4"


def test_upscale_uploads_file_source(mp4: Path) -> None:
    """job.source.uri file:// → upload helper called once; submit gets pod path.

    Bug caught: dispatch fires the upload but then submits the LOCAL file://
    URL anyway, so /upscale's downloader tries to read a path that does not
    exist on the pod. /upscale fails with FileNotFoundError immediately.
    """
    from kinoforge.upscalers.spandrel import SpandrelEngine
    from kinoforge.upscalers.spandrel import _engine as spandrel_mod

    engine = SpandrelEngine()
    captured: dict[str, object] = {}

    def fake_http(
        *, method: str, url: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        if method == "POST":
            assert payload is not None
            captured.update(payload)
            return {"job_id": "j-file"}
        return {
            "state": "done",
            "progress": 1.0,
            "result": {
                "filename": "out.mp4",
                "sha256": "z",
                "size": 1,
                "input_resolution": [64, 48],
                "output_resolution": [128, 96],
                "engine_meta": {},
            },
            "error": None,
        }

    with (
        patch.object(
            engine,
            "_upload_source",
            return_value="file:///tmp/kf-uploads/up.mp4",
        ) as upl,
        patch.object(spandrel_mod, "_http_json", side_effect=fake_http),
    ):
        engine.upscale(_instance(), _job(f"file://{mp4}"), _cfg())
    upl.assert_called_once()
    assert upl.call_args.args[1] == mp4
    assert captured["source_url"] == "file:///tmp/kf-uploads/up.mp4"
