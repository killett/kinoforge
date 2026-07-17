"""Job-based /lora/set_stack: sync submit-time rejects + async job + status."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kinoforge.engines.diffusers.servers import wan_t2v_server as srv


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient with a single-transformer pipe (Wan 2.1, arity=1).

    Restores ``ready`` and re-clears ``_inventory`` / ``_swap_jobs`` on
    teardown: pytest-randomly is active, so a test elsewhere that depends
    on the not-ready state (or on an empty inventory) must not inherit
    this fixture's leaked module state.
    """
    # Single-transformer pipe (Wan 2.1): arity 1.
    monkeypatch.setattr(srv, "_pipe_arity", 1)
    srv._inventory.clear()
    srv._swap_jobs.clear()
    was_ready = srv.ready.is_set()
    srv.ready.set()
    yield TestClient(srv.app)
    srv._inventory.clear()
    srv._swap_jobs.clear()
    if not was_ready:
        srv.ready.clear()


def _spec(size: int = 10) -> dict[str, Any]:
    return {
        "url": "https://x/f.safetensors",
        "headers": {},
        "filename": "f.safetensors",
        "size_hint": size,
    }


def test_illegal_branch_rejected_400_without_download(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: the doomed branch request downloads 350MB before it can
    400 (today's behavior) — the exact cause of the branch-test 502s."""
    calls: list[Any] = []

    def _spy(*a: Any, **k: Any) -> tuple[str, int]:
        calls.append(a)
        return ("/x", 10)

    monkeypatch.setattr(srv, "_download_one", _spy)
    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [{"ref": "r", "strength": 1.0, "branch": "high_noise"}],
            "download_specs": {"r": _spec()},
        },
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["error"] == "branch_routing"
    assert body["reason"] == "branch_unsupported_single_transformer"
    assert calls == []  # never downloaded


def test_happy_submit_returns_job_id_and_downloads_in_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Submit returns job_id; download happens inside the async job."""
    seen: list[str] = []

    def _fake_download(spec: srv.ArtifactDownloadSpec, d: Any) -> tuple[str, int]:
        seen.append(spec.filename)
        return ("/loras/f.safetensors", 10)

    monkeypatch.setattr(srv, "_download_one", _fake_download)
    monkeypatch.setattr(srv, "_replace_adapter_stack", lambda target: None)
    monkeypatch.setattr(srv, "_disk_free_bytes", lambda _: 10_000_000)
    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [{"ref": "r", "strength": 1.0, "branch": "auto"}],
            "download_specs": {"r": _spec()},
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert job_id
    # TestClient runs the create_task job to completion synchronously within
    # the request's event loop turn; poll the status.
    status = client.get(f"/lora/set_stack/status/{job_id}").json()
    assert status["state"] == "done"
    assert seen == ["f.safetensors"]  # download happened in the job
    assert [e["ref"] for e in status["inventory"]] == ["r"]
    assert status["swap_rejected"] is None


def test_status_unknown_job_404(client: TestClient) -> None:
    """Unknown job_id returns 404."""
    assert client.get("/lora/set_stack/status/nope").status_code == 404


def test_download_failure_surfaces_as_error_state_with_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: a download failure that isn't legible to the client — the
    error payload must carry status=502 so _raise_lora_swap_error routes it."""

    def boom(spec: Any, d: Any) -> tuple[str, int]:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(srv, "_download_one", boom)
    monkeypatch.setattr(srv, "_disk_free_bytes", lambda _: 10_000_000)
    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [{"ref": "r", "strength": 1.0, "branch": "auto"}],
            "download_specs": {"r": _spec()},
        },
    )
    job_id = resp.json()["job_id"]
    status = client.get(f"/lora/set_stack/status/{job_id}").json()
    assert status["state"] == "error"
    assert status["error"]["status"] == 502
    assert status["error"]["error"] == "lora_download_failed"


def test_unfittable_plan_rejected_507_synchronously(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: a doomed-by-disk plan spins up a job + must be polled to
    learn it can never fit; spec requires a synchronous 507 phase:plan at POST.
    """
    calls: list[Any] = []

    def _spy_download(*a: Any, **k: Any) -> tuple[str, int]:
        calls.append(a)
        return ("/x", 10)

    monkeypatch.setattr(srv, "_download_one", _spy_download)
    monkeypatch.setattr(srv, "_disk_free_bytes", lambda p: 5)
    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [{"ref": "r", "strength": 1.0, "branch": "auto"}],
            "download_specs": {
                "r": {
                    "url": "https://x/f",
                    "headers": {},
                    "filename": "f",
                    "size_hint": 10_000,
                }
            },
        },
    )
    assert resp.status_code == 507
    body = resp.json()["detail"]
    assert body["error"] == "disk_full"
    assert body["phase"] == "plan"
    assert calls == []  # never downloaded
    assert srv._swap_jobs == {}  # no job enqueued


async def _direct(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """asyncio.to_thread stand-in that calls fn synchronously.

    The TestClient event loop does not drain multi-step asyncio.to_thread chains
    inside background tasks before the next HTTP request.  Replacing to_thread
    with a direct coroutine call keeps the full _run_swap_job coroutine
    sequential within a single event loop pass, so the POST response is not
    returned until the job (including OOM rollback) has fully completed.
    """
    return fn(*args, **kwargs)


def test_vram_oom_produces_done_swap_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: a VRAM-OOM during set_adapters silently surfaces as an
    ``error`` job state instead of ``done`` + ``swap_rejected.reason=vram_oom``,
    making it indistinguishable from a download failure and preventing the
    client from mapping the result to LoraSwapVramOomError.
    """

    def _fake_download(spec: srv.ArtifactDownloadSpec, d: Any) -> tuple[str, int]:
        return ("/loras/f.safetensors", 10)

    call_count = [0]

    def _oom_then_succeed(target: Any) -> None:
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        # Second call is the rollback — succeed silently.

    monkeypatch.setattr(srv, "_download_one", _fake_download)
    monkeypatch.setattr(srv, "_replace_adapter_stack", _oom_then_succeed)
    monkeypatch.setattr(srv, "_disk_free_bytes", lambda _: 10_000_000)
    # Bypass asyncio.to_thread: the TestClient event loop does not drain
    # multi-step to_thread chains (download + OOM + rollback) in one pass.
    # Patching at the asyncio module level works because wan_t2v_server does
    # ``import asyncio`` and resolves ``asyncio.to_thread`` through the module
    # object, which is the same object we patch here.
    monkeypatch.setattr(asyncio, "to_thread", _direct)

    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [{"ref": "r", "strength": 1.0, "branch": "auto"}],
            "download_specs": {"r": _spec()},
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    status = client.get(f"/lora/set_stack/status/{job_id}").json()
    assert status["state"] == "done"
    assert status["swap_rejected"] is not None
    assert status["swap_rejected"]["reason"] == "vram_oom"
    # "r" was not in inventory before the job ran (empty inventory fixture),
    # so it is a newly-downloaded ref that gets dropped on OOM rollback.
    assert status["swap_rejected"]["target_refs_dropped"] == ["r"]
