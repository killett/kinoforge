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
        assert srv.MODEL_ID == "Wan-AI/Wan2.2-T2V-A14B"


# Future TestGenerate (Task 5) + TestArtifacts (Task 6) will append below.
