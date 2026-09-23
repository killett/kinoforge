"""Behavior: the HTTP contract in front of the shared LoRA apply seam.

Everything here drives a real ``FastAPI()`` app through ``TestClient`` with the
real router mounted, the real async job, and real bytes landing on a real disk
— only ``urllib.request.urlopen`` is faked, the same seam
``test_lora_apply_core`` fakes. Nothing asserts on a mock.

The contract is NOT this module's to invent: ``DiffusersBackend.set_lora_stack``
and ``_poll_set_stack`` are one client shared with the Wan pod, so the field
names, the job-state names and the error-body keys are what that client reads.
A test here that passes against a renamed field is a test that lets one of the
two pods break silently.
"""

from __future__ import annotations

import asyncio
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kinoforge.engines.diffusers.servers import _lora
from tests.engines.diffusers.servers.test_lora_apply_core import FakePipe, _profile


@dataclass
class _Harness:
    """Everything a test needs to steer one mounted router.

    Attributes:
        client: The HTTP client for the mounted app.
        pipe: The fake pipeline the profile drives.
        loras_dir: Root the router downloads into.
        urls: Every URL ``urlopen`` was asked for, in order — the proof that a
            request was (or was not) allowed to reach the network.
        gate: Held closed to keep a download in flight while the test inspects
            the job record; open by default.
    """

    client: TestClient
    pipe: FakePipe
    loras_dir: Path
    urls: list[str]
    gate: threading.Event


class _FakeResponse:
    """urlopen context manager yielding ``payload`` once, then EOF."""

    def __init__(self, payload: bytes) -> None:
        self._chunks = [payload]

    def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _clean_module_state() -> Iterator[None]:
    """Inventory and the job table are process state — no test inherits another's."""
    _lora._INVENTORY.clear()
    _lora._JOBS.clear()
    yield
    _lora._INVENTORY.clear()
    _lora._JOBS.clear()


def _mount(pipe: FakePipe, profile: _lora.LoraProfile, loras_dir: Path) -> TestClient:
    """Return a client over a real app with the real router mounted.

    Enter it as a context manager: that keeps ONE event loop alive across
    requests, so the ``asyncio.create_task`` job really runs in the background
    and a status poll really observes it progressing.
    """
    app = FastAPI()
    app.include_router(
        _lora.build_lora_router(lambda: pipe, lambda: profile, loras_dir)
    )
    return TestClient(app)


def _single_target_profile(pipe: FakePipe, explain: Any = None) -> _lora.LoraProfile:
    """A one-partition profile whose failure explanation the caller chooses."""
    return _lora.LoraProfile(
        name="fake",
        targets=("transformer",),
        default_target="transformer",
        load=lambda p, path, name, target: p.load(path, name, target),
        module_for=lambda p, target: p.module,
        after_load=lambda p: None,
        explain_load_failure=explain or (lambda exc: None),
    )


def _serve(payload: bytes = b"w" * 64) -> Any:
    """A urlopen stand-in that always serves ``payload``."""
    return lambda req, timeout=None: _FakeResponse(payload)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    """A single-target pod (``targets=("transformer",)``) with a fake network."""
    pipe = FakePipe()
    loras_dir = tmp_path / "loras"
    loras_dir.mkdir()
    urls: list[str] = []
    gate = threading.Event()
    gate.set()

    def fake_urlopen(req: Any, timeout: float | None = None) -> _FakeResponse:
        urls.append(req.full_url)
        gate.wait(timeout=10.0)
        return _FakeResponse(b"w" * 64)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with _mount(pipe, _profile(pipe), loras_dir) as client:
        yield _Harness(
            client=client, pipe=pipe, loras_dir=loras_dir, urls=urls, gate=gate
        )


def _spec(name: str = "f.safetensors") -> dict[str, Any]:
    """One download spec in the shape the orchestrator ships."""
    return {
        "url": f"https://vendor/{name}",
        "headers": {},
        "filename": name,
        "size_hint": 64,
    }


def _submit(client: TestClient, body: dict[str, Any]) -> Any:
    """POST a set_stack request and return the raw response."""
    return client.post("/lora/set_stack", json=body)


def _poll(client: TestClient, job_id: str, timeout_s: float = 10.0) -> dict[str, Any]:
    """Poll the status endpoint until the job reaches a terminal state."""
    deadline = time.monotonic() + timeout_s
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/lora/set_stack/status/{job_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["state"] in ("done", "error"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished; last record {body}")


def _apply(harness: _Harness, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Submit a stack of entries and poll it to a terminal record."""
    resp = _submit(
        harness.client,
        {
            "target": entries,
            "download_specs": {
                e["ref"]: _spec(f"{e['ref'].replace(':', '_')}.safetensors")
                for e in entries
            },
        },
    )
    assert resp.status_code == 200, resp.text
    return _poll(harness.client, resp.json()["job_id"])


# ---------------------------------------------------------------------------
# Submit / status lifecycle
# ---------------------------------------------------------------------------


def test_submit_returns_a_job_id_and_leaves_the_work_for_the_background(
    harness: _Harness,
) -> None:
    """The POST returns before the download finishes.

    Catches a handler that awaits the whole swap inline. A ~1.4 GB fetch behind
    a provider proxy takes minutes; the proxy gives up long before, and the
    controller gets a 502 for a swap that is actually still succeeding. The
    gate holds the download open, so a synchronous implementation could not
    even return from the POST — it would hang here rather than pass.
    """
    harness.gate.clear()
    resp = _submit(
        harness.client,
        {
            "target": [{"ref": "civitai:1@1", "strength": 0.8}],
            "download_specs": {"civitai:1@1": _spec()},
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert job_id

    mid = harness.client.get(f"/lora/set_stack/status/{job_id}").json()
    assert mid["state"] in ("queued", "running")
    assert mid["inventory"] is None
    assert _lora.inventory_snapshot() == []

    harness.gate.set()
    assert _poll(harness.client, job_id)["state"] == "done"


def test_done_record_carries_the_inventory_and_free_bytes(harness: _Harness) -> None:
    """A finished job reports what is loaded and how much disk is left.

    Catches a ``done`` record with no result: the client returns
    ``{inventory: None, free_bytes: None}`` and every downstream consumer —
    the CLI renderer, the ref-redaction registration — silently sees an empty
    pod holding a full stack.
    """
    done = _apply(
        harness,
        [
            {"ref": "civitai:1@1", "strength": 0.8},
            {"ref": "civitai:2@2", "strength": 0.4},
        ],
    )
    assert done["state"] == "done"
    assert done["swap_rejected"] is None
    assert done["error"] is None
    assert [row["ref"] for row in done["inventory"]] == ["civitai:1@1", "civitai:2@2"]
    assert [row["adapter_name"] for row in done["inventory"]] == ["lora_0", "lora_1"]
    assert [row["last_strength"] for row in done["inventory"]] == [0.8, 0.4]
    assert [row["target"] for row in done["inventory"]] == ["transformer"] * 2
    assert [row["size_bytes"] for row in done["inventory"]] == [64, 64]
    assert isinstance(done["free_bytes"], int)
    assert done["free_bytes"] > 0


def test_the_status_endpoint_serves_the_stored_record_verbatim(
    harness: _Harness,
) -> None:
    """What a poll reads is exactly what the job wrote — result and all.

    Catches a status handler that reconstructs a payload of its own (from the
    live inventory, say) instead of serving the record: the job's result and
    the reply then drift, and a ``done`` reply can describe a stack the job
    never actually finished applying.
    """
    done = _apply(harness, [{"ref": "civitai:1@1", "strength": 1.0}])
    stored = _lora._JOBS[next(iter(_lora._JOBS))]
    assert stored["state"] == "done"
    assert stored["inventory"] is not None
    assert stored["free_bytes"] is not None
    assert done == stored


def test_unknown_job_id_is_404(harness: _Harness) -> None:
    """An id the pod never issued is a 404, not an empty 200.

    Catches returning ``None``/``{}`` for an unknown id: the client's poll loop
    reads ``state`` off it forever and only escapes on its wall-clock budget,
    turning an instant, legible failure into minutes of paid GPU idling.
    """
    assert harness.client.get("/lora/set_stack/status/s-nope").status_code == 404


def test_a_second_stack_replaces_the_first(harness: _Harness) -> None:
    """Two jobs, two records, and the pod holds only the second stack.

    Catches a job table keyed by anything but the job id (the second submit
    would overwrite the first's record), and an apply that accumulates rather
    than replaces.
    """
    first = _apply(harness, [{"ref": "civitai:1@1", "strength": 1.0}])
    second = _apply(harness, [{"ref": "civitai:2@2", "strength": 0.5}])
    assert first["state"] == second["state"] == "done"
    assert len(_lora._JOBS) == 2
    assert [row["ref"] for row in second["inventory"]] == ["civitai:2@2"]
    inv = harness.client.get("/lora/inventory").json()
    assert [row["ref"] for row in inv["inventory"]] == ["civitai:2@2"]


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def test_inventory_is_empty_with_free_bytes_before_any_stack(
    harness: _Harness,
) -> None:
    """A fresh pod answers the inventory read, it does not 404 or 500.

    Catches an inventory endpoint that only works once a stack exists — the
    orchestrator reads it to decide whether a warm pod needs a swap at all.
    """
    body = harness.client.get("/lora/inventory").json()
    assert body["inventory"] == []
    assert isinstance(body["free_bytes"], int)
    assert body["free_bytes"] > 0


def test_inventory_matches_the_done_record_row_for_row(harness: _Harness) -> None:
    """The two ways to read the stack cannot disagree.

    Catches the endpoint building its rows from a different source than the job
    record's — the two then drift, and which one is true depends on which the
    caller happened to read.
    """
    done = _apply(
        harness,
        [
            {"ref": "civitai:1@1", "strength": 1.0},
            {"ref": "civitai:1@1", "strength": 0.25},
        ],
    )
    body = harness.client.get("/lora/inventory").json()
    assert body["inventory"] == done["inventory"]
    # Same ref twice is two adapters, so two rows — not one collapsed row.
    assert [row["adapter_name"] for row in body["inventory"]] == ["lora_0", "lora_1"]


# ---------------------------------------------------------------------------
# Target legality — the synchronous gate
# ---------------------------------------------------------------------------


def test_illegal_target_is_rejected_before_download(harness: _Harness) -> None:
    """Target legality is checked on the synchronous submit path.

    Catches a legality gate that runs inside the job, after 1.4 GB has already
    come down the wire.
    """
    resp = _submit(
        harness.client,
        {
            "target": [{"ref": "civitai:1@1", "target": "high_noise"}],
            "download_specs": {"civitai:1@1": _spec()},
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "lora_target_unsupported"
    assert detail["target"] == "high_noise"
    assert detail["legal"] == ["transformer"]
    assert harness.urls == []
    assert _lora._JOBS == {}


def test_deprecated_branch_is_routed_through_target_legality(
    harness: _Harness,
) -> None:
    """``branch`` maps onto ``target`` and is then judged by the same gate.

    The shared client still ships ``branch`` on every entry, so a gate that
    only reads ``target`` would wave a Wan-vocabulary request straight through
    to a pipeline that has no such partition — and the failure would land deep
    inside diffusers, after the download.
    """
    resp = _submit(
        harness.client,
        {
            "target": [{"ref": "civitai:1@1", "branch": "h"}],
            "download_specs": {"civitai:1@1": _spec()},
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["target"] == "high_noise"
    assert harness.urls == []


def test_branch_auto_is_legal_when_the_profile_has_a_default(
    harness: _Harness,
) -> None:
    """``branch: auto`` means "the pod decides", which a single-target pod can.

    Positive control for the gate above: over-tightening it to reject every
    entry that names no target would refuse the shape the client sends by
    default, i.e. every request.
    """
    done = _apply(harness, [{"ref": "civitai:1@1", "branch": "auto"}])
    assert done["state"] == "done"
    assert done["inventory"][0]["target"] == "transformer"


def test_unnamed_target_is_rejected_before_download_when_there_is_no_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A two-partition pod refuses to guess, and refuses it at the POST.

    Catches leaving this case to ``apply_stack``: ``TargetRequired`` there
    fires only after both partitions' worth of bytes are on disk, and reaches
    the controller as an unexplained 500 instead of a 400 naming the choices.
    """
    pipe = FakePipe()
    loras_dir = tmp_path / "loras"
    loras_dir.mkdir()
    urls: list[str] = []

    def spy(req: Any, timeout: float | None = None) -> _FakeResponse:
        urls.append(req.full_url)
        return _FakeResponse(b"w" * 32)

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    dual = _lora.LoraProfile(
        name="dual",
        targets=("transformer", "transformer_ref"),
        default_target=None,
        load=lambda p, path, name, target: p.load(path, name, target),
        module_for=lambda p, target: p.module,
        after_load=lambda p: None,
        explain_load_failure=lambda exc: None,
    )
    with _mount(pipe, dual, loras_dir) as client:
        resp = _submit(
            client,
            {
                "target": [{"ref": "civitai:1@1"}],
                "download_specs": {"civitai:1@1": _spec()},
            },
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "lora_target_unsupported"
    assert detail["target"] is None
    assert detail["legal"] == ["transformer", "transformer_ref"]
    assert urls == []


# ---------------------------------------------------------------------------
# Failure bodies
# ---------------------------------------------------------------------------


def _doomed_body(name: str) -> dict[str, Any]:
    """A one-entry request whose file name makes ``FakePipe`` fail the load."""
    return {
        "target": [{"ref": "civitai:9@9"}],
        "download_specs": {
            "civitai:9@9": {
                "url": f"https://vendor/{name}",
                "headers": {},
                "filename": name,
                "size_hint": 32,
            }
        },
    }


def test_format_failure_surfaces_the_hint_at_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pruned-checkpoint LoRA ends the job non-retryably with the hint.

    Catches it falling into the generic 502 download path, where the proxy
    retry budget burns three times on a file that can never load.
    """
    pipe = FakePipe(fail_on="doomed")
    loras_dir = tmp_path / "loras"
    loras_dir.mkdir()
    monkeypatch.setattr(urllib.request, "urlopen", _serve(b"w" * 32))
    profile = _single_target_profile(
        pipe,
        explain=lambda exc: (
            "use lightx2v/Minimax-h3-Turbo instead"
            if "size mismatch" in str(exc)
            else None
        ),
    )
    with _mount(pipe, profile, loras_dir) as client:
        resp = _submit(client, _doomed_body("doomed.safetensors"))
        job = _poll(client, resp.json()["job_id"])

    assert job["state"] == "error"
    assert job["error"]["status"] == 400
    assert job["error"]["error"] == "lora_format_unsupported"
    assert "lightx2v" in job["error"]["hint"]
    assert job["error"]["ref"] == "civitai:9@9"
    # Rolled back: the pod is genuinely LoRA-free, and says so.
    assert _lora.inventory_snapshot() == []


def test_unexplained_load_failure_is_a_500_lora_load_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load the profile cannot explain is a 500, not a 400.

    Catches flattening every load failure into the non-retryable 400: a
    transient CUDA fault would then be reported as "this file can never load",
    and the run fails permanently on a condition a retry would have cleared.
    """
    pipe = FakePipe(fail_on="doomed")
    loras_dir = tmp_path / "loras"
    loras_dir.mkdir()
    monkeypatch.setattr(urllib.request, "urlopen", _serve(b"w" * 32))
    with _mount(pipe, _single_target_profile(pipe), loras_dir) as client:
        resp = _submit(client, _doomed_body("doomed.safetensors"))
        job = _poll(client, resp.json()["job_id"])

    assert job["state"] == "error"
    assert job["error"]["status"] == 500
    assert job["error"]["error"] == "lora_load_failed"
    assert job["error"]["ref"] == "civitai:9@9"
    assert "hint" not in job["error"]


def test_download_failure_names_the_ref_and_leaves_the_stack_alone(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed fetch is a 502 the client can route and retry.

    Catches two things at once: a body without ``download_failed`` (the client
    raises ``LoraSwapDownloadError(ref="")`` and the operator cannot tell which
    LoRA died), and a non-empty ``evict_completed``, which the client reads as
    "the pod is in a half-state" and escalates to a degraded-pod error that
    tears down a perfectly healthy pod.
    """
    assert _apply(harness, [{"ref": "civitai:1@1", "strength": 1.0}])["state"] == "done"

    def boom(req: Any, timeout: float | None = None) -> _FakeResponse:
        raise OSError("connection reset by peer")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    resp = _submit(
        harness.client,
        {
            "target": [{"ref": "civitai:7@7"}],
            "download_specs": {"civitai:7@7": _spec("other.safetensors")},
        },
    )
    job = _poll(harness.client, resp.json()["job_id"])

    assert job["state"] == "error"
    assert job["error"]["status"] == 502
    assert job["error"]["error"] == "lora_download_failed"
    assert job["error"]["download_failed"] == "civitai:7@7"
    assert job["error"]["evict_completed"] == []
    assert "connection reset by peer" in job["error"]["underlying"]
    # The apply never ran, so the pod still holds exactly what it held before —
    # which is what makes this failure safe for the client to retry.
    inv = harness.client.get("/lora/inventory").json()
    assert [row["ref"] for row in inv["inventory"]] == ["civitai:1@1"]


def test_a_ref_with_no_download_spec_is_a_typed_failure_not_a_crash(
    harness: _Harness,
) -> None:
    """A spec-less ref ends the job with a body naming it.

    Catches a bare ``download_specs[ref]``: the ``KeyError`` escapes into the
    job as an unhandled crash, the record keeps ``state="running"`` forever,
    and the controller only learns anything when its poll budget expires.
    """
    resp = _submit(
        harness.client,
        {"target": [{"ref": "civitai:5@5"}], "download_specs": {}},
    )
    job = _poll(harness.client, resp.json()["job_id"])
    assert job["state"] == "error"
    assert job["error"]["status"] == 502
    assert job["error"]["download_failed"] == "civitai:5@5"


def test_a_raising_rollback_still_ends_the_job_with_an_error_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback's own failure must not escape as an unhandled crash.

    ``apply_stack`` unloads on its way out of a failed load; if that unload
    raises, the secondary exception REPLACES the ``LoraLoadError``. A handler
    that only catches ``LoraLoadError`` leaves the job pinned at ``running``
    with no error body — the pod is in its worst state and says nothing, and
    the controller burns its whole poll budget finding out.
    """

    class _RollbackExplodes(FakePipe):
        """Fails the load, then fails the unload that rolls it back."""

        def unload_lora_weights(self) -> None:
            super().unload_lora_weights()
            if self.unload_count > 1:
                raise RuntimeError("CUDA error: unspecified launch failure")

    pipe = _RollbackExplodes(fail_on="doomed")
    loras_dir = tmp_path / "loras"
    loras_dir.mkdir()
    monkeypatch.setattr(urllib.request, "urlopen", _serve(b"w" * 32))
    with _mount(pipe, _profile(pipe), loras_dir) as client:
        resp = _submit(client, _doomed_body("doomed.safetensors"))
        job = _poll(client, resp.json()["job_id"])

    assert job["state"] == "error"
    assert job["error"]["status"] == 500
    assert job["error"]["error"] == "lora_swap_failed"
    assert "unspecified launch failure" in job["error"]["underlying"]


# ---------------------------------------------------------------------------
# The event loop
# ---------------------------------------------------------------------------


def test_the_blocking_load_does_not_run_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apply_stack`` runs in a worker thread, never on the loop.

    This is a filed, previously-diagnosed failure: synchronous work inside an
    ``async def`` handler blocks the event loop, ``/health`` stops answering,
    and the provider proxy returns 502 for a pod that is perfectly alive. The
    probe is the loop itself — ``get_running_loop`` raises off-loop — so
    inlining the call is the only way to fail this.
    """
    on_loop: list[bool] = []
    pipe = FakePipe()
    loras_dir = tmp_path / "loras"
    loras_dir.mkdir()
    monkeypatch.setattr(urllib.request, "urlopen", _serve(b"w" * 32))

    def _record_then_load(p: Any, path: str, name: str, target: str) -> None:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        p.load(path, name, target)

    profile = _lora.LoraProfile(
        name="fake",
        targets=("transformer",),
        default_target="transformer",
        load=_record_then_load,
        module_for=lambda p, target: p.module,
        after_load=lambda p: None,
        explain_load_failure=lambda exc: None,
    )
    with _mount(pipe, profile, loras_dir) as client:
        resp = _submit(
            client,
            {
                "target": [{"ref": "civitai:1@1"}],
                "download_specs": {"civitai:1@1": _spec()},
            },
        )
        job = _poll(client, resp.json()["job_id"])

    assert job["state"] == "done"
    assert on_loop == [False]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (
            {"target": [{"ref": "r:a", "strengh": 1.0}], "download_specs": {}},
            "a misspelled field must not be silently dropped",
        ),
        (
            {"target": [{"ref": "r:a", "strength": 9.0}], "download_specs": {}},
            "strength is bounded to the a1111 range on both sides of the wire",
        ),
        (
            {"target": [{"ref": ""}], "download_specs": {}},
            "an empty ref names nothing and would download nothing",
        ),
        (
            {
                "target": [{"ref": "r:a", "branch": "h", "target": "transformer"}],
                "download_specs": {},
            },
            "branch and target disagreeing is ambiguous routing, not a default",
        ),
    ],
)
def test_malformed_requests_are_refused_at_422(
    harness: _Harness, body: dict[str, Any], why: str
) -> None:
    """The pod validates the wire, it does not trust the controller.

    Catches a schema that drops ``extra="forbid"`` or the strength bounds: a
    typo'd field crosses intact and is ignored, and the operator sees a LoRA
    applied at a strength they did not ask for with no error anywhere.
    """
    assert _submit(harness.client, body).status_code == 422, why
    assert harness.urls == []
