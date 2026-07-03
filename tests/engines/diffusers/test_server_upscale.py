"""Tests for /upscale + /upscale/status/{id} server endpoints (T12).

The endpoints live in ``wan_t2v_server`` so SeedVR2 upscale and Wan T2V
generation share one process — ``_LOADED`` (the LRU model registry from
T11) is the substrate that lets both pipelines co-reside on one pod.

Tests patch ``_ensure_on_gpu`` to bypass the real CUDA load; the fake
pipe's ``upscale`` is a MagicMock returning a real tmp file the server
can sha256 + ffprobe (those helpers are also patched).
"""

from __future__ import annotations

import asyncio
import importlib
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from kinoforge.core.scale_target import ScaleTarget


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Import a fresh server module with CUDA + sha256 + ffprobe patched.

    Yields ``(srv, client, fake_pipe, out_mp4)`` so each test has a
    handle to the module (for ``_LOADED`` / ``_upscale_jobs`` inspection),
    the TestClient, the MagicMock standing in for the SeedVR2 pipe, and
    the tmp output mp4 that ``pipe.upscale`` returns.

    Critical: ``_load_pipeline`` is short-circuited so the startup
    event does NOT mutate the module-level ``_pipe_arity`` cache — left
    alone, a MagicMock pipe yields ``_detect_moe_arity == 0`` which
    leaks into adjacent tests in this directory that share the module.
    """
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    importlib.reload(srv)

    out_mp4 = tmp_path / "out.mp4"
    out_mp4.write_bytes(b"\x00" * 64)  # arbitrary non-empty payload.

    fake_pipe = MagicMock(name="SeedVR2Pipe")
    fake_pipe.upscale = MagicMock(return_value=out_mp4)
    fake_loaded = {
        "name": "seedvr2-3b-fp8",
        "pipe": fake_pipe,
        "vram_bytes": 10 * 1024**3,
        "last_used_monotonic": 0.0,
        "on_device": "cuda",
    }

    async def _fake_ensure_on_gpu(name: str) -> dict[str, Any]:
        return fake_loaded

    monkeypatch.setattr(srv, "_ensure_on_gpu", _fake_ensure_on_gpu)
    monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(srv, "LORAS_DIR", tmp_path / "loras")
    # Short-circuit startup: return a sentinel that startup assigns to
    # the module-level ``pipe`` but never touches ``_pipe_arity``.
    monkeypatch.setattr(srv, "_load_pipeline", lambda **_kw: MagicMock())
    monkeypatch.setattr(srv, "_pipe_arity", 1)

    with TestClient(srv.app) as client:
        # /upscale doesn't gate on ready.is_set(); no wait loop needed.
        yield srv, client, fake_pipe, out_mp4


def _wait_for_state(
    client: TestClient, job_id: str, *, target: set[str], timeout_s: float = 2.0
) -> dict[str, Any]:
    """Poll status until ``state`` enters ``target`` or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        r = client.get(f"/upscale/status/{job_id}")
        if r.status_code != 200:
            time.sleep(0.01)
            continue
        last = r.json()
        if last.get("state") in target:
            return last
        time.sleep(0.01)
    raise AssertionError(
        f"job {job_id} never reached one of {target}; last seen: {last}"
    )


_VALID_BODY = {
    "source_url": "file:///tmp/in.mp4",
    "source_filename": "in.mp4",
    "scale": "2x",
    "engine": "seedvr2",
    "seedvr2": {"variant": "3B", "precision": "fp8"},
}


class TestPostUpscale:
    def test_post_upscale_returns_job_id(self, fresh_server: Any) -> None:
        # Bug caught: route missing OR response key renamed; CLI loses
        # the handle and the upscale becomes un-pollable.
        _srv, client, _pipe, _out = fresh_server
        r = client.post("/upscale", json=_VALID_BODY)
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body.get("job_id"), str) and body["job_id"]

    def test_post_upscale_spandrel_engine_accepted(self, fresh_server: Any) -> None:
        # Bug caught: route gates on engine=="seedvr2" only and silently 400s
        # spandrel jobs even though the loader (_load_model_to_gpu) has a
        # spandrel-* branch. Client's SpandrelEngine.upscale posts
        # engine="spandrel"; this test pins the route's accept-list.
        _srv, client, _pipe, _out = fresh_server
        body = {
            "source_url": "https://example.invalid/in.mp4",
            "source_filename": "in.mp4",
            "scale": "2x",
            "engine": "spandrel",
            "spandrel": {"arch": "realesrgan", "precision": "fp16"},
        }
        r = client.post("/upscale", json=body)
        assert r.status_code == 200, r.text
        assert isinstance(r.json().get("job_id"), str) and r.json()["job_id"]

    def test_run_upscale_job_spandrel_builds_arch_precision_model_name(
        self,
        fresh_server: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Bug caught: _run_upscale_job hardcodes "seedvr2-{variant}-{precision}"
        # for every engine; a spandrel job would request the wrong pipeline
        # from _ensure_on_gpu (or NoneType.variant AttributeError if seedvr2
        # block is None). Pins the spandrel branch builds the slug from
        # req.spandrel.arch + precision, matching the loader's parser.
        srv, _client, fake_pipe, out_mp4 = fresh_server

        seen_names: list[str] = []

        async def spy_ensure(name: str) -> dict[str, Any]:
            seen_names.append(name)
            return {
                "name": name,
                "pipe": fake_pipe,
                "vram_bytes": 0,
                "last_used_monotonic": 0.0,
                "on_device": "cuda",
            }

        monkeypatch.setattr(srv, "_ensure_on_gpu", spy_ensure)

        src_mp4 = tmp_path / "in.mp4"
        src_mp4.write_bytes(b"\x00" * 64)

        req = srv.UpscaleRequest(
            source_url=f"file://{src_mp4}",
            source_filename=src_mp4.name,
            scale="2x",
            engine="spandrel",
            spandrel=srv.SpandrelParams(arch="realesrgan", precision="fp16"),
        )
        srv._upscale_jobs["job-spandrel-1"] = {
            "state": "queued",
            "progress": 0.0,
            "result": None,
            "error": None,
        }

        asyncio.run(srv._run_upscale_job("job-spandrel-1", req))

        assert seen_names == ["spandrel-realesrgan-fp16"], seen_names
        assert srv._upscale_jobs["job-spandrel-1"]["state"] == "done", (
            srv._upscale_jobs["job-spandrel-1"]
        )

    def test_post_upscale_unsupported_engine_400(self, fresh_server: Any) -> None:
        # Bug caught: server silently accepts an unknown engine and enqueues
        # a job that dies asynchronously, leaving the caller with a job_id
        # whose status will only ever report "error" — wasting one warm-pod
        # attach cycle. The 400 surface lets the caller fail fast at submit
        # time. (Kept literal unknown token since flashvsr was promoted to
        # a supported engine in the FlashVSR v1 default upscaler rollout.)
        _srv, client, _pipe, _out = fresh_server
        bad = {**_VALID_BODY, "engine": "does_not_exist"}
        r = client.post("/upscale", json=bad)
        assert r.status_code == 400, r.text
        assert "unsupported engine" in r.json().get("detail", "").lower()

    def test_post_upscale_flashvsr_engine_accepted(self, fresh_server: Any) -> None:
        # Bug caught: allowlist regression drops flashvsr → cfg.upscale.engine
        # == "flashvsr" always 400s at submit time. Pins engine=="flashvsr"
        # into the allowlist beside seedvr2 + spandrel.
        _srv, client, _pipe, _out = fresh_server
        body = {
            "source_url": "https://example.invalid/in.mp4",
            "source_filename": "in.mp4",
            "scale": "2x",
            "engine": "flashvsr",
            "flashvsr": {
                "weights_bundle": "hf:JunhaoZhuang/FlashVSR-v1.1",
                "precision": "fp16",
            },
        }
        r = client.post("/upscale", json=body)
        assert r.status_code == 200, r.text
        assert isinstance(r.json().get("job_id"), str) and r.json()["job_id"]

    def test_run_upscale_job_flashvsr_builds_precision_slug(
        self,
        fresh_server: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Bug caught: _run_upscale_job falls through to the else branch
        # (seedvr2) for req.engine=="flashvsr" → attempts to build
        # "seedvr2-3b-fp8" and reaches for req.seedvr2 which is None →
        # AttributeError. Pins the flashvsr branch builds the slug from
        # req.flashvsr.precision, matching the loader's parser.
        srv, _client, fake_pipe, _out = fresh_server

        seen_names: list[str] = []

        async def spy_ensure(name: str) -> dict[str, Any]:
            seen_names.append(name)
            return {
                "name": name,
                "pipe": fake_pipe,
                "vram_bytes": 0,
                "last_used_monotonic": 0.0,
                "on_device": "cuda",
            }

        monkeypatch.setattr(srv, "_ensure_on_gpu", spy_ensure)

        src_mp4 = tmp_path / "in.mp4"
        src_mp4.write_bytes(b"\x00" * 64)

        req = srv.UpscaleRequest(
            source_url=f"file://{src_mp4}",
            source_filename=src_mp4.name,
            scale="4x",
            engine="flashvsr",
            flashvsr=srv.FlashVSRParams(
                weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
                precision="bfloat16",
            ),
        )
        srv._upscale_jobs["job-flashvsr-1"] = {
            "state": "queued",
            "progress": 0.0,
            "result": None,
            "error": None,
        }

        asyncio.run(srv._run_upscale_job("job-flashvsr-1", req))

        assert seen_names == ["flashvsr-wan21-bfloat16"], seen_names
        assert srv._upscale_jobs["job-flashvsr-1"]["state"] == "done", (
            srv._upscale_jobs["job-flashvsr-1"]
        )

    def test_flashvsr_weights_dir_env_override(
        self, fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Bug caught: _flashvsr_weights_dir hardcodes /workspace/models/flashvsr
        # → unit tests can't write there, and operators wanting to point at a
        # different mount lose the environment escape-hatch. Pins the env-var
        # contract so future refactors don't drop the override.
        srv, _client, _pipe, _out = fresh_server
        monkeypatch.setenv("KINOFORGE_FLASHVSR_WEIGHTS_DIR", str(tmp_path / "flash"))
        assert srv._flashvsr_weights_dir() == tmp_path / "flash"
        monkeypatch.delenv("KINOFORGE_FLASHVSR_WEIGHTS_DIR")
        assert srv._flashvsr_weights_dir() == Path("/workspace/models/flashvsr")


class TestUpscaleStatus:
    def test_get_upscale_status_unknown_404(self, fresh_server: Any) -> None:
        # Bug caught: handler returns {} or null with 200, indistinguishable
        # from a job that hasn't started yet — caller would poll forever.
        # Detail check prevents the test passing against FastAPI's default
        # "Not Found" (route-missing) — must come from the handler's
        # explicit raise so we know the lookup logic ran.
        _srv, client, _pipe, _out = fresh_server
        r = client.get("/upscale/status/does-not-exist")
        assert r.status_code == 404
        assert "unknown" in r.json().get("detail", "").lower()

    def test_post_upscale_initial_status_payload_shape(
        self, fresh_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: payload missing the four-key contract the CLI
        # poll loop reads (state / progress / result / error). Asserts
        # the registration race is closed: the moment POST returns,
        # GET status MUST already find the job in one of the lifecycle
        # states (queued / running / done — error excluded because the
        # download path is patched so no exception can fire here).
        srv, client, _pipe, _out = fresh_server
        monkeypatch.setattr(srv, "_sha256_file", lambda _p: "deadbeef")
        monkeypatch.setattr(srv, "_probe_resolution", lambda _p: (1920, 1080))
        monkeypatch.setattr(
            srv,
            "_download_to_local_temp",
            lambda _url, _name: Path("/tmp/in.mp4"),
        )
        post = client.post("/upscale", json=_VALID_BODY)
        job_id = post.json()["job_id"]
        r = client.get(f"/upscale/status/{job_id}")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"state", "progress", "result", "error"}
        assert body["state"] in {"queued", "running", "done"}


class TestUpscaleExecution:
    def test_upscale_passes_parsed_scaletarget_to_pipe(
        self, fresh_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: server forwards the raw "2x" string to the pipe,
        # which expects a ScaleTarget instance — call would crash in
        # production. Asserts the parse seam fires server-side, not
        # client-side. Expected value (factor=2.0) computed by hand
        # from ScaleTarget grammar: "2x" → factor 2.0.
        srv, client, pipe, _out = fresh_server
        monkeypatch.setattr(srv, "_sha256_file", lambda _p: "deadbeef")
        monkeypatch.setattr(srv, "_probe_resolution", lambda _p: (1920, 1080))
        monkeypatch.setattr(
            srv,
            "_download_to_local_temp",
            lambda _url, _name: Path("/tmp/in.mp4"),
        )

        post = client.post("/upscale", json=_VALID_BODY)
        job_id = post.json()["job_id"]
        _wait_for_state(client, job_id, target={"done", "error"})

        assert pipe.upscale.called, "pipe.upscale never invoked"
        call = pipe.upscale.call_args
        # 2nd positional arg is the ScaleTarget.
        scale_arg = call.args[1] if len(call.args) >= 2 else call.kwargs.get("scale")
        assert isinstance(scale_arg, ScaleTarget), f"got {type(scale_arg)}"
        assert scale_arg.kind == "factor"
        assert scale_arg.value == 2.0

    def test_upscale_done_result_block_full_shape(
        self, fresh_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: result block missing required keys the matcher
        # / ledger / successful-generations log consumes — silent
        # downstream KeyError or empty cells in evidence files.
        srv, client, _pipe, out_mp4 = fresh_server
        monkeypatch.setattr(srv, "_sha256_file", lambda _p: "abc123" * 10 + "ab")
        monkeypatch.setattr(srv, "_probe_resolution", lambda _p: (1920, 1080))
        monkeypatch.setattr(
            srv,
            "_download_to_local_temp",
            lambda _url, _name: Path("/tmp/in.mp4"),
        )

        post = client.post("/upscale", json=_VALID_BODY)
        job_id = post.json()["job_id"]
        body = _wait_for_state(client, job_id, target={"done"})

        assert body["state"] == "done"
        assert body["progress"] == 1.0
        result = body["result"]
        assert set(result.keys()) >= {
            "filename",
            "sha256",
            "size",
            "input_resolution",
            "output_resolution",
            "engine_meta",
        }
        assert result["filename"] == out_mp4.name
        assert result["size"] == out_mp4.stat().st_size

    def test_upscale_pipe_exception_marks_state_error(
        self, fresh_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: handler lacks try/except → exception kills the
        # asyncio task silently → job stays "queued" forever, CLI
        # polling loop spins until its outer timeout. Asserts the
        # error message is preserved so the caller can act on it.
        srv, client, pipe, _out = fresh_server
        monkeypatch.setattr(
            srv,
            "_download_to_local_temp",
            lambda _url, _name: Path("/tmp/in.mp4"),
        )
        pipe.upscale.side_effect = RuntimeError("CUDA OOM at tile 17")

        post = client.post("/upscale", json=_VALID_BODY)
        job_id = post.json()["job_id"]
        body = _wait_for_state(client, job_id, target={"error"})

        assert body["state"] == "error"
        assert "CUDA OOM at tile 17" in (body.get("error") or "")


class TestUpscaleConcurrency:
    def test_upscale_lock_is_asyncio_lock(self, fresh_server: Any) -> None:
        # Bug caught: someone replaces _upscale_lock with a
        # threading.Lock (incompatible with `async with`), or removes
        # it entirely. Either regression would surface only under
        # concurrent /upscale traffic, when the model registry is
        # already mid-mutation by an in-flight job.
        srv, _client, _pipe, _out = fresh_server
        assert hasattr(srv, "_upscale_lock"), "module missing _upscale_lock"
        assert isinstance(srv._upscale_lock, asyncio.Lock)

    def test_pipe_call_dispatched_via_asyncio_to_thread(
        self, fresh_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: regression of wan_server_async_blocking memory —
        # if pipe.upscale (sync, blocking CUDA call) runs directly in
        # the event loop, /health stalls for the full upscale duration
        # and RunPod's proxy 502s. Asserts the heavy call goes through
        # asyncio.to_thread.
        srv, client, pipe, _out = fresh_server
        monkeypatch.setattr(srv, "_sha256_file", lambda _p: "deadbeef")
        monkeypatch.setattr(srv, "_probe_resolution", lambda _p: (1920, 1080))
        monkeypatch.setattr(
            srv,
            "_download_to_local_temp",
            lambda _url, _name: Path("/tmp/in.mp4"),
        )

        seen: list[Any] = []
        real_to_thread = asyncio.to_thread

        async def tracking_to_thread(fn: Any, *args: Any, **kw: Any) -> Any:
            seen.append(fn)
            return await real_to_thread(fn, *args, **kw)

        monkeypatch.setattr(asyncio, "to_thread", tracking_to_thread)

        post = client.post("/upscale", json=_VALID_BODY)
        job_id = post.json()["job_id"]
        _wait_for_state(client, job_id, target={"done", "error"})

        # pipe.upscale (a MagicMock) must be one of the callables passed
        # through to_thread; identity check, not name check.
        assert pipe.upscale in seen, (
            f"pipe.upscale not dispatched via asyncio.to_thread; saw {seen!r}"
        )
