"""POST /interpolate + /interpolate/status/{id} on the embedded server.

Mirrors the /upscale endpoint tests: the job runner is driven directly with a
fake pipe + fake download so the async lock + result-block plumbing is verified
without a GPU. The full pod path is proven live in Task 12.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kinoforge.engines.diffusers.servers import wan_t2v_server as srv


@pytest.fixture
def client() -> TestClient:
    return TestClient(srv.app)


@pytest.fixture(autouse=True)
def _isolate_interpolate_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the module-level interpolate globals pristine across tests.

    ``asyncio.run(_run_interpolate_job(...))`` acquires the module
    ``_interpolate_lock``, binding it to a throwaway loop that is then closed —
    a dead-loop-bound lock leaks process-level asyncio state that flakes a
    later subprocess smoke (the vram-rollback lora test). Swap in a fresh lock
    per test and reset the jobs dict so nothing survives teardown.
    """
    monkeypatch.setattr(srv, "_interpolate_lock", asyncio.Lock())
    monkeypatch.setattr(srv, "_interpolate_jobs", {})


def _req(**over: Any) -> srv.InterpolateRequest:
    base: dict[str, Any] = {
        "source_url": "file:///tmp/in.mp4",
        "source_filename": "in.mp4",
        "target_fps": 60.0,
        "engine": "rife",
        "rife": {"model": "rife49", "precision": "fp16"},
        "job_id": "j-test",
    }
    base.update(over)
    return srv.InterpolateRequest(**base)


def test_unknown_engine_rejected_at_submit(client: TestClient) -> None:
    # Bug caught: an unknown interpolator engine reaching the async runner burns
    # a warm-attach cycle on a job destined to error. Fail fast with 400.
    resp = client.post(
        "/interpolate",
        json={
            "source_url": "file:///tmp/in.mp4",
            "source_filename": "in.mp4",
            "target_fps": 60.0,
            "engine": "nope",
        },
    )
    assert resp.status_code == 400


def test_status_unknown_job_is_404(client: TestClient) -> None:
    resp = client.get("/interpolate/status/does-not-exist")
    assert resp.status_code == 404


def test_run_job_populates_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Bug caught: the runner flips state to "done" before writing result, or
    # drops the runtime's fps/count fields -> a poller sees done with no result.
    result = {
        "filename": "out.mp4",
        "sha256": "0" * 64,
        "size": 4096,
        "input_fps": 16.0,
        "output_fps": 60.0,
        "input_frame_count": 16,
        "output_frame_count": 60,
        "engine_meta": {"model": "rife49"},
    }

    class _FakePipe:
        def interpolate(
            self, local: Path, fps: float, params: dict[str, Any]
        ) -> dict[str, Any]:
            assert fps == 60.0
            return result

    async def fake_ensure(name: str) -> dict[str, Any]:
        assert name == "rife-rife49"
        return {"pipe": _FakePipe()}

    monkeypatch.setattr(srv, "_ensure_on_gpu", fake_ensure)
    monkeypatch.setattr(
        srv, "_download_to_local_temp", lambda url, fn: tmp_path / "in.mp4"
    )
    monkeypatch.setattr(srv, "_maybe_cleanup_upload", lambda url: None)

    jid = "j-ok"
    srv._interpolate_jobs[jid] = {
        "state": "queued",
        "progress": 0.0,
        "result": None,
        "error": None,
    }
    asyncio.run(srv._run_interpolate_job(jid, _req(job_id=jid)))

    job = srv._interpolate_jobs[jid]
    assert job["state"] == "done"
    assert job["result"] == result
    assert job["result"]["output_fps"] == 60.0


def test_run_job_records_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Bug caught: a runtime exception swallowed -> job stuck "running" forever
    # and the client polls until timeout instead of failing fast.
    class _BoomPipe:
        def interpolate(
            self, local: Path, fps: float, params: dict[str, Any]
        ) -> dict[str, Any]:
            raise RuntimeError("cuda oom")

    async def fake_ensure(name: str) -> dict[str, Any]:
        return {"pipe": _BoomPipe()}

    monkeypatch.setattr(srv, "_ensure_on_gpu", fake_ensure)
    monkeypatch.setattr(
        srv, "_download_to_local_temp", lambda url, fn: tmp_path / "in.mp4"
    )
    monkeypatch.setattr(srv, "_maybe_cleanup_upload", lambda url: None)

    jid = "j-err"
    srv._interpolate_jobs[jid] = {
        "state": "queued",
        "progress": 0.0,
        "result": None,
        "error": None,
    }
    asyncio.run(srv._run_interpolate_job(jid, _req(job_id=jid)))

    job = srv._interpolate_jobs[jid]
    assert job["state"] == "error"
    assert "cuda oom" in job["error"]
