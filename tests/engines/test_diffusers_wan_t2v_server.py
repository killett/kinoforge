"""Tests for kinoforge.engines.diffusers.servers.wan_t2v_server.

The server is a FastAPI app that wraps diffusers.WanPipeline. To avoid
loading the real pipeline (which downloads ~63 GB of weights), tests
patch the module-level pipeline factory with a Fake that returns a
3-frame uint8 tensor in <50 ms.

Three test classes mirror the implementation tasks:
  TestHealth     — Task 4
  TestGenerate   — Task 5
  TestArtifacts  — Task 6
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Import a fresh copy of the server module with a fake pipeline.

    The module imports diffusers at module load only via a lazy seam
    (`_load_pipeline()`), so we can patch that seam BEFORE the startup
    event fires.

    Yields (module, client) — the module so tests can inspect
    `ready`/`pipe`/`jobs`, the TestClient so tests can hit endpoints.
    """
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    importlib.reload(srv)

    fake_frames = np.zeros((3, 8, 8, 3), dtype=np.uint8)
    fake_frames[0, :, :, 0] = 255
    fake_frames[1, :, :, 1] = 255
    fake_frames[2, :, :, 2] = 255

    class FakePipeOutput:
        def __init__(self, frames: np.ndarray) -> None:
            self.frames = [frames]

    class FakePipe:
        def __init__(self) -> None:
            self.model_id = "fake-wan"

        def __call__(self, **kwargs: Any) -> FakePipeOutput:
            time.sleep(0.05)
            return FakePipeOutput(fake_frames)

        def to(self, device: str) -> FakePipe:
            return self

    monkeypatch.setattr(srv, "_load_pipeline", lambda: FakePipe())
    monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")

    with TestClient(srv.app) as client:
        for _ in range(50):
            if srv.ready.is_set():
                break
            time.sleep(0.01)
        yield srv, client


class TestHealth:
    def test_health_returns_ready_after_startup(self, fresh_server: Any) -> None:
        # Bug caught: ready flag never set, /health forever reports
        # {"ready": false}, orchestrator waits forever.
        srv, client = fresh_server
        assert srv.ready.is_set()
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert body["model"] == srv.MODEL_ID

    def test_health_reports_model_id_from_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Bug caught: model id hardcoded to Wan 2.2 path, blocking the
        # easy upgrade to Wan 2.3 / 3.0 via env var.
        monkeypatch.setenv("WAN_MODEL_ID", "Wan-AI/Wan2.3-T2V-A14B")
        import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

        importlib.reload(srv)
        assert srv.MODEL_ID == "Wan-AI/Wan2.3-T2V-A14B"

    def test_health_default_model_id_is_wan22(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: default drifts away from documented Phase 1 target.
        monkeypatch.delenv("WAN_MODEL_ID", raising=False)
        import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

        importlib.reload(srv)
        # Default model id points at the diffusers-format repo, NOT
        # the native Wan-AI checkpoint layout. See plan amendment
        # 2026-06-19, Task 8 attempt #7.
        assert srv.MODEL_ID == "Wan-AI/Wan2.2-T2V-A14B-Diffusers"


class TestGenerate:
    def test_generate_returns_job_id(self, fresh_server: Any) -> None:
        # Bug caught: /generate fails to return a job_id or returns
        # malformed JSON, breaking DiffusersBackend.submit().
        _srv, client = fresh_server
        r = client.post(
            "/generate",
            json={
                "prompt": "a red panda",
                "width": 8,
                "height": 8,
                "num_frames": 3,
                "fps": 24,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert "job_id" in body
        assert isinstance(body["job_id"], str) and len(body["job_id"]) > 0

    def test_generate_rejected_when_not_ready(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Bug caught: race where /generate accepts jobs before the
        # pipeline is loaded, then crashes the worker with NoneType
        # has no attribute __call__.
        import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

        importlib.reload(srv)
        monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")
        load_event = threading.Event()
        monkeypatch.setattr(srv, "_load_pipeline", lambda: load_event.wait() or None)

        # Don't enter context — startup will not fire ready before request.
        client = TestClient(srv.app)
        try:
            r = client.post(
                "/generate",
                json={
                    "prompt": "x",
                    "width": 8,
                    "height": 8,
                    "num_frames": 3,
                    "fps": 24,
                },
            )
        finally:
            load_event.set()
        assert r.status_code == 503

    def test_status_404_for_unknown_id(self, fresh_server: Any) -> None:
        # Bug caught: /status returns 200 with empty body for unknown
        # ids, confusing the orchestrator's poll loop.
        _srv, client = fresh_server
        r = client.get("/status/never-existed")
        assert r.status_code == 404

    def test_status_progresses_to_done(self, fresh_server: Any) -> None:
        # Bug caught: worker thread not spawned, or status never
        # transitions from "queued" to "done" because the worker
        # writes to the wrong key.
        _srv, client = fresh_server
        job_id = client.post(
            "/generate",
            json={
                "prompt": "x",
                "width": 8,
                "height": 8,
                "num_frames": 3,
                "fps": 24,
            },
        ).json()["job_id"]

        final: dict[str, Any] | None = None
        body: dict[str, Any] = {}
        for _ in range(100):
            r = client.get(f"/status/{job_id}")
            assert r.status_code == 200
            body = r.json()
            if body["status"] == "done":
                final = body
                break
            assert body["status"] in ("queued", "running"), body
            time.sleep(0.05)
        assert final is not None, "status never became done within 5s"
        assert final["filename"].endswith(".mp4")
        assert final["url"].endswith(final["filename"])

    def test_status_error_includes_exception_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Bug caught: worker swallows the exception and leaves
        # status="running" forever, OR returns "error" without the
        # actual message.
        import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

        importlib.reload(srv)
        monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")

        class BoomPipe:
            def __call__(self, **kwargs: Any) -> Any:
                raise RuntimeError("synthetic CUDA OOM")

            def to(self, device: str) -> BoomPipe:
                return self

        monkeypatch.setattr(srv, "_load_pipeline", lambda: BoomPipe())

        with TestClient(srv.app) as client:
            for _ in range(50):
                if srv.ready.is_set():
                    break
                time.sleep(0.01)
            job_id = client.post(
                "/generate",
                json={
                    "prompt": "x",
                    "width": 8,
                    "height": 8,
                    "num_frames": 3,
                    "fps": 24,
                },
            ).json()["job_id"]

            final: dict[str, Any] | None = None
            for _ in range(100):
                body = client.get(f"/status/{job_id}").json()
                if body["status"] == "error":
                    final = body
                    break
                time.sleep(0.05)
            assert final is not None, "status never became error"
            assert "synthetic CUDA OOM" in final["error"]

    def test_worker_survives_one_failing_job(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Bug caught: worker thread dies on first exception, second
        # job sits in "queued" forever, pod hangs until stall_reap.
        import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

        importlib.reload(srv)
        monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")

        attempts = {"count": 0}
        good_frames = np.zeros((3, 8, 8, 3), dtype=np.uint8)
        good_frames[:, :, :, 1] = 200

        class FakePipeOutput:
            def __init__(self, frames: np.ndarray) -> None:
                self.frames = [frames]

        class FlakeyPipe:
            def __call__(self, **kwargs: Any) -> FakePipeOutput:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("first job blows up")
                return FakePipeOutput(good_frames)

            def to(self, device: str) -> FlakeyPipe:
                return self

        monkeypatch.setattr(srv, "_load_pipeline", lambda: FlakeyPipe())

        with TestClient(srv.app) as client:
            for _ in range(50):
                if srv.ready.is_set():
                    break
                time.sleep(0.01)

            job1 = client.post(
                "/generate",
                json={
                    "prompt": "x",
                    "width": 8,
                    "height": 8,
                    "num_frames": 3,
                    "fps": 24,
                },
            ).json()["job_id"]
            for _ in range(100):
                if client.get(f"/status/{job1}").json()["status"] == "error":
                    break
                time.sleep(0.05)

            job2 = client.post(
                "/generate",
                json={
                    "prompt": "y",
                    "width": 8,
                    "height": 8,
                    "num_frames": 3,
                    "fps": 24,
                },
            ).json()["job_id"]
            body2: dict[str, Any] = {}
            for _ in range(100):
                body2 = client.get(f"/status/{job2}").json()
                if body2["status"] == "done":
                    break
                time.sleep(0.05)
            assert body2["status"] == "done", body2


class TestArtifacts:
    def test_artifact_endpoint_returns_mp4(self, fresh_server: Any) -> None:
        # Bug caught: /artifacts not registered, or wrong media_type,
        # or wrong file path.
        _srv, client = fresh_server
        job_id = client.post(
            "/generate",
            json={
                "prompt": "x",
                "width": 8,
                "height": 8,
                "num_frames": 3,
                "fps": 24,
            },
        ).json()["job_id"]
        body: dict[str, Any] = {}
        for _ in range(100):
            body = client.get(f"/status/{job_id}").json()
            if body["status"] == "done":
                break
            time.sleep(0.05)
        assert body["status"] == "done"
        r = client.get(f"/artifacts/{body['filename']}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "video/mp4"
        assert len(r.content) > 0
        # ISO-BMFF magic bytes 4-7 == 'ftyp'
        assert r.content[4:8] == b"ftyp"

    def test_artifact_404_for_unknown_filename(self, fresh_server: Any) -> None:
        _srv, client = fresh_server
        r = client.get("/artifacts/does-not-exist.mp4")
        assert r.status_code == 404

    def test_artifact_rejects_path_traversal(
        self, fresh_server: Any, tmp_path: Any
    ) -> None:
        # Bug caught: unsanitised filename lets a caller read arbitrary
        # files on the pod (e.g. /etc/passwd) via /artifacts/../../etc/passwd.
        _srv, client = fresh_server
        sentinel = tmp_path / "secret.txt"
        sentinel.write_text("DO NOT LEAK")
        r = client.get("/artifacts/../secret.txt")
        # FastAPI normalises the path before the handler, so the path
        # becomes /artifacts/secret.txt → 404. Either 400 or 404 OK;
        # NOT acceptable is 200 with the bytes.
        assert r.status_code in (400, 404)
        r = client.get("/artifacts/%2E%2E%2Fsecret.txt")
        assert r.status_code in (400, 404)
