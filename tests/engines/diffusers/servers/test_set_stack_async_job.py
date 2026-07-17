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


# ---------------------------------------------------------------------------
# Behaviors ported 2026-07-16 from the pre-8d88e0b synchronous-contract files
# (tests/engines/test_wan_t2v_server_set_stack{,_failures}.py, deleted in the
# same commit). Each drives the job path end-to-end: POST submit -> job ->
# GET status.
# ---------------------------------------------------------------------------


def _seed_row(ref: str, size: int = 100, last_used: str = "x") -> dict[str, Any]:
    """Inventory row shaped like a completed prior download of ``ref``."""
    return {
        "ref": ref,
        "filename": f"{ref.lower()}.s",
        "size_bytes": size,
        "loras_dir_path": f"/loras/{ref.lower()}.s",
        "downloaded_at_local": last_used,
        "last_used_at_local": last_used,
        "adapter_name": f"lora_0_{ref.lower()}",
        "branch": "auto",
    }


class _EvictSpyPipe:
    """Minimal pipe surface for eviction paths: records adapter deletes."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_adapters(self, names: list[str] | str) -> None:
        self.deleted.extend([names] if isinstance(names, str) else list(names))


def _target(ref: str) -> dict[str, Any]:
    return {"ref": ref, "strength": 1.0, "branch": "auto"}


def _install_sync_job(
    monkeypatch: pytest.MonkeyPatch, download_log: list[str]
) -> list[list[Any]]:
    """Common stubs: sync to_thread, recording download, recording apply.

    Returns the apply-call log (each element is the LoraTarget list passed
    to ``_replace_adapter_stack``).
    """
    monkeypatch.setattr(asyncio, "to_thread", _direct)

    def _fake_download(spec: srv.ArtifactDownloadSpec, d: Any) -> tuple[str, int]:
        download_log.append(spec.filename)
        return (f"/loras/{spec.filename}", 100)

    monkeypatch.setattr(srv, "_download_one", _fake_download)
    apply_calls: list[list[Any]] = []
    monkeypatch.setattr(
        srv, "_replace_adapter_stack", lambda target: apply_calls.append(list(target))
    )
    monkeypatch.setattr(srv, "_disk_free_bytes", lambda _: 10_000_000)
    return apply_calls


def test_idempotent_same_stack_skips_download_but_reapplies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-submitting the identical stack downloads nothing yet still applies.

    Bug caught (download half): from-disk dedup broken — the job re-fetches
    a ref that is already in inventory, re-paying a 350 MB pull per warm
    re-attach. Bug caught (apply half): a 'same stack, nothing to do'
    short-circuit skips _replace_adapter_stack, leaving a pipe whose
    adapters were dropped out-of-band (e.g. by a Wan re-promotion) silently
    LoRA-less while the job reports done.
    """
    downloads: list[str] = []
    apply_calls = _install_sync_job(monkeypatch, downloads)
    body = {"target": [_target("A")], "download_specs": {"A": _spec()}}

    first = client.post("/lora/set_stack", json=body)
    assert (
        client.get(f"/lora/set_stack/status/{first.json()['job_id']}").json()["state"]
        == "done"
    )
    assert downloads == ["f.safetensors"]
    assert len(apply_calls) == 1

    second = client.post("/lora/set_stack", json=body)
    status = client.get(f"/lora/set_stack/status/{second.json()['job_id']}").json()
    assert status["state"] == "done"
    assert downloads == ["f.safetensors"]  # no re-download
    assert len(apply_calls) == 2  # but the stack WAS re-applied
    assert [e["ref"] for e in status["inventory"]] == ["A"]


def test_overlap_downloads_only_new_ref(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing A + target [A, B] downloads only B's file.

    Bug caught: the to-download set is computed from the target alone
    (ignoring inventory), so A's 350 MB file is wastefully re-fetched on
    every stack extension.
    """
    downloads: list[str] = []
    _install_sync_job(monkeypatch, downloads)
    srv._inventory[("A", "auto")] = _seed_row("A")

    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [_target("A"), _target("B")],
            "download_specs": {
                "A": dict(_spec(), filename="a.s"),
                "B": dict(_spec(), filename="b.s"),
            },
        },
    )
    status = client.get(f"/lora/set_stack/status/{resp.json()['job_id']}").json()
    assert status["state"] == "done"
    assert downloads == ["b.s"]
    assert set(srv._inventory.keys()) == {("A", "auto"), ("B", "auto")}
    assert sorted(e["ref"] for e in status["inventory"]) == ["A", "B"]


def test_tight_disk_evicts_non_target_then_downloads(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """50 free bytes + 100-byte target: evicting non-target A funds B.

    Bug caught: the plan accounting compares the download size against
    ``initial_free`` instead of ``initial_free + mandatory_freed``, so a
    swap that fits fine after eviction is rejected 507 (or runs into a
    mid-download ENOSPC) even though A's 100 bytes were about to be freed.
    """
    downloads: list[str] = []
    _install_sync_job(monkeypatch, downloads)
    monkeypatch.setattr(srv, "_disk_free_bytes", lambda _: 50)
    pipe = _EvictSpyPipe()
    monkeypatch.setattr(srv, "pipe", pipe)
    srv._inventory[("A", "auto")] = _seed_row(
        "A", last_used="2026-06-20T09:00:00-07:00"
    )

    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [_target("B")],
            "download_specs": {"B": dict(_spec(size=100), filename="b.s")},
        },
    )
    assert resp.status_code == 200  # NOT a sync 507: plan is feasible
    status = client.get(f"/lora/set_stack/status/{resp.json()['job_id']}").json()
    assert status["state"] == "done"
    assert downloads == ["b.s"]
    assert ("A", "auto") not in srv._inventory
    assert ("B", "auto") in srv._inventory
    assert pipe.deleted == ["lora_0_a"]


def test_download_fail_after_eviction_reports_degraded_pod(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download failure AFTER an eviction carries the evicted refs.

    Bug caught: the job's error record drops ``evict_completed``, so the
    client maps the failure to plain LoraSwapDownloadError instead of
    LoraSwapDegradedPodError — the orchestrator then re-uses a pod whose
    previous stack was already torn down (X is gone, nothing replaced it).
    """
    monkeypatch.setattr(asyncio, "to_thread", _direct)
    monkeypatch.setattr(srv, "_disk_free_bytes", lambda _: 10_000_000)
    monkeypatch.setattr(srv, "pipe", _EvictSpyPipe())
    srv._inventory[("X", "auto")] = _seed_row("X", last_used="old")

    def _boom(spec: Any, d: Any) -> tuple[str, int]:
        raise RuntimeError("CivitAI 504")

    monkeypatch.setattr(srv, "_download_one", _boom)
    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [_target("B")],
            "download_specs": {"B": dict(_spec(), filename="b.s")},
        },
    )
    status = client.get(f"/lora/set_stack/status/{resp.json()['job_id']}").json()
    assert status["state"] == "error"
    err = status["error"]
    assert err["status"] == 502
    assert err["error"] == "lora_download_failed"
    assert err["evict_completed"] == ["X"]
    assert err["download_failed"] == "B"
    assert "504" in err["underlying"]


def test_disk_full_mid_download_maps_to_507(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ENOSPC raised inside the download maps to 507 phase:download.

    Bug caught: ENOSPC classified as a generic 502 download failure, so
    the orchestrator's classifier retries a fatal disk-full as if it were
    a transient CivitAI throttle. (The sync submit-time 507 covers only
    plan-time infeasibility; this is the mid-download TOCTOU case.)
    """
    monkeypatch.setattr(asyncio, "to_thread", _direct)
    monkeypatch.setattr(srv, "_disk_free_bytes", lambda _: 10_000_000)

    def _enospc(spec: Any, d: Any) -> tuple[str, int]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(srv, "_download_one", _enospc)
    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [_target("B")],
            "download_specs": {"B": dict(_spec(), filename="b.s")},
        },
    )
    status = client.get(f"/lora/set_stack/status/{resp.json()['job_id']}").json()
    assert status["state"] == "error"
    err = status["error"]
    assert err["status"] == 507
    assert err["error"] == "disk_full"
    assert err["phase"] == "download"
    assert err["download_failed"] == "B"


def test_vram_oom_rollback_restores_previous_stack(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OOM on apply rolls the pod back to the exact pre-swap stack.

    Bug caught: rollback replays the FAILED target (or nothing) instead of
    the snapshot — the job reports done + swap_rejected, the client keeps
    routing generations to the pod, and every render runs with the wrong
    (or no) LoRA stack. The end-state assertions (inventory == pre-swap,
    rollback call argument == snapshot) are what the async_job OOM test
    above cannot see from its empty-prior-state fixture.
    """
    monkeypatch.setattr(asyncio, "to_thread", _direct)
    monkeypatch.setattr(srv, "_disk_free_bytes", lambda _: 10_000_000)

    def _fake_download(spec: srv.ArtifactDownloadSpec, d: Any) -> tuple[str, int]:
        return (f"/loras/{spec.filename}", 100)

    monkeypatch.setattr(srv, "_download_one", _fake_download)

    apply_calls: list[list[Any]] = []

    def _oom_then_record(target: Any) -> None:
        apply_calls.append(list(target))
        if len(apply_calls) == 1:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")

    monkeypatch.setattr(srv, "_replace_adapter_stack", _oom_then_record)
    srv._inventory[("A", "auto")] = _seed_row("A")

    resp = client.post(
        "/lora/set_stack",
        json={
            "target": [_target("A"), _target("B")],
            "download_specs": {
                "A": dict(_spec(), filename="a.s"),
                "B": dict(_spec(), filename="b.s"),
            },
        },
    )
    status = client.get(f"/lora/set_stack/status/{resp.json()['job_id']}").json()
    assert status["state"] == "done"
    assert status["swap_rejected"]["reason"] == "vram_oom"
    assert status["swap_rejected"]["target_refs_dropped"] == ["B"]
    # End state: pod is back on the pre-swap stack, in inventory AND on the
    # pipe (the rollback apply received the snapshot, not the failed target).
    assert [e["ref"] for e in status["inventory"]] == ["A"]
    assert set(srv._inventory.keys()) == {("A", "auto")}
    assert len(apply_calls) == 2
    rollback = apply_calls[1]
    assert [(t.ref, t.branch, t.strength) for t in rollback] == [("A", "auto", 1.0)]
