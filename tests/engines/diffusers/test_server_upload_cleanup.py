"""Server-side cleanup: unlink uploaded input after /upscale finishes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def srv_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """Return wan_t2v_server with _UPLOAD_DIR + ARTIFACT_DIR redirected to tmp_path.

    Pointing ARTIFACT_DIR at tmp_path keeps ``_download_to_local_temp``'s
    ``shutil.copyfile`` from polluting the real /workspace/artifacts during
    unit tests.
    """
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setattr(srv, "_UPLOAD_DIR", tmp_path / "kf-uploads")
    monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")
    (tmp_path / "kf-uploads").mkdir()
    (tmp_path / "artifacts").mkdir()
    return srv


def _make_req(srv_module: object, source_path: Path) -> object:
    """Build a minimal UpscaleRequest pointing at source_path."""
    return srv_module.UpscaleRequest(  # type: ignore[attr-defined]
        source_url=f"file://{source_path}",
        source_filename=source_path.name,
        scale="2x",
        engine="spandrel-realesrgan-fp16",
        spandrel=srv_module.SpandrelParams(arch="realesrgan", precision="fp16"),  # type: ignore[attr-defined]
        job_id=f"job-{source_path.stem}",
    )


def _mock_pipe_returning(out_path: Path) -> MagicMock:
    """MagicMock pipe whose ``.upscale(...)`` returns ``out_path``."""
    pipe = MagicMock()
    pipe.upscale.return_value = out_path
    return pipe


def test_cleanup_unlinks_upload_dir_source(srv_module: object, tmp_path: Path) -> None:
    """Source under _UPLOAD_DIR is unlinked after /upscale completes.

    Bug caught: warm-reuse smokes accumulate uploaded mp4s on the pod across
    repeats → disk fills, future uploads 507, operator must SSH to clean.
    """
    upload_dir = srv_module._UPLOAD_DIR  # type: ignore[attr-defined]
    src = upload_dir / "abcd1234.mp4"
    src.write_bytes(b"x" * 1024)

    out_path = tmp_path / "out.mp4"
    out_path.write_bytes(b"y" * 1024)

    req = _make_req(srv_module, src)
    srv_module._upscale_jobs[req.job_id] = {  # type: ignore[attr-defined]
        "state": "queued",
        "progress": 0.0,
        "result": None,
        "error": None,
    }

    pipe = _mock_pipe_returning(out_path)
    with patch.object(
        srv_module,
        "_ensure_on_gpu",
        new=AsyncMock(
            return_value={
                "name": "spandrel-realesrgan-fp16",
                "pipe": pipe,
                "vram_bytes": 0,
                "last_used_monotonic": 0.0,
                "on_device": "cuda",
            }
        ),
    ):
        asyncio.run(srv_module._run_upscale_job(req.job_id, req))  # type: ignore[attr-defined]

    assert not src.exists(), "upload should be unlinked"


def test_cleanup_skips_sibling_source(srv_module: object, tmp_path: Path) -> None:
    """Source outside _UPLOAD_DIR (operator pre-staged) is NOT touched.

    Bug caught: cleanup walks anything that looks like a file:// path and
    deletes the operator's pre-staged input — losing the source file the
    operator wanted to retain for cross-engine comparison.
    """
    sibling = tmp_path / "sibling.mp4"
    sibling.write_bytes(b"z" * 1024)

    out_path = tmp_path / "out.mp4"
    out_path.write_bytes(b"y" * 1024)

    req = _make_req(srv_module, sibling)
    srv_module._upscale_jobs[req.job_id] = {  # type: ignore[attr-defined]
        "state": "queued",
        "progress": 0.0,
        "result": None,
        "error": None,
    }

    pipe = _mock_pipe_returning(out_path)
    with patch.object(
        srv_module,
        "_ensure_on_gpu",
        new=AsyncMock(
            return_value={
                "name": "spandrel-realesrgan-fp16",
                "pipe": pipe,
                "vram_bytes": 0,
                "last_used_monotonic": 0.0,
                "on_device": "cuda",
            }
        ),
    ):
        asyncio.run(srv_module._run_upscale_job(req.job_id, req))  # type: ignore[attr-defined]

    assert sibling.exists(), "sibling source must survive cleanup"


def test_cleanup_runs_on_failure(srv_module: object, tmp_path: Path) -> None:
    """Pipe.upscale raising → upload still deleted in finally.

    Bug caught: cleanup wired in the success branch only → a failed upscale
    leaves the upload behind, same disk-fill regression as repeats.
    """
    upload_dir = srv_module._UPLOAD_DIR  # type: ignore[attr-defined]
    src = upload_dir / "fail1234.mp4"
    src.write_bytes(b"x" * 1024)

    req = _make_req(srv_module, src)
    srv_module._upscale_jobs[req.job_id] = {  # type: ignore[attr-defined]
        "state": "queued",
        "progress": 0.0,
        "result": None,
        "error": None,
    }

    pipe = MagicMock()
    pipe.upscale.side_effect = RuntimeError("boom")
    with patch.object(
        srv_module,
        "_ensure_on_gpu",
        new=AsyncMock(
            return_value={
                "name": "spandrel-realesrgan-fp16",
                "pipe": pipe,
                "vram_bytes": 0,
                "last_used_monotonic": 0.0,
                "on_device": "cuda",
            }
        ),
    ):
        asyncio.run(srv_module._run_upscale_job(req.job_id, req))  # type: ignore[attr-defined]

    assert not src.exists(), "upload should be unlinked even on upscale failure"
    assert srv_module._upscale_jobs[req.job_id]["state"] == "error"  # type: ignore[attr-defined]
