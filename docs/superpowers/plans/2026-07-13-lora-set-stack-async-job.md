# Job-based `/lora/set_stack` (async submit + poll) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LoRA swaps resilient to slow/variable networks by turning `/lora/set_stack` into a job-based endpoint (POST returns `job_id`, client polls status), matching the server's existing `/generate` · `/upscale` · `/interpolate` pattern, and fix the harness defect that made 3 weeks of failures look like dead pods.

**Architecture:** Hybrid. At POST, cheap/instant rejections stay synchronous HTTP 4xx (request-shape 422; branch-legality 400, hoisted before download; plan-time disk 507). The long path — download + load + rollback — runs in an `asyncio.create_task` job under the existing `_swap_lock`; a new `GET /lora/set_stack/status/{job_id}` reports terminal state. The `DiffusersEngine.set_lora_stack` client submits + polls **internally**, keeping its signature + raised exceptions identical, so its callers (warm-reuse matcher, `pod_lock`, grid executor) are untouched.

**Tech Stack:** Python 3.13, FastAPI (in-pod server `wan_t2v_server.py`), pytest + FastAPI `TestClient`, urllib-based client transport with `retry_proxy_call` proxy-retry, RunPod live smoke.

**User decisions (already made):**
- "0 + the logging half of 2, with 1a as an optional follow-on feature" — job-based set_stack + harness logging fix now; LoRA prefetch deferred.
- Rejection semantics: **Hybrid** (sync 4xx for cheap rejections, async job for download+load+rollback). Not uniform-all-via-status.
- Client hides async behind the unchanged `set_lora_stack` signature; callers unchanged.
- Prefetch (1a) is out of scope — separate follow-on spec.

**Spec:** `docs/superpowers/specs/2026-07-13-lora-set-stack-async-job-design.md`

---

## File structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` | Server: shared branch gate; split `set_stack` → submit + `_run_swap_job`; `_swap_jobs` registry; status route | 0, 1 |
| `src/kinoforge/engines/diffusers/__init__.py` | Client: `set_lora_stack` submit+poll internals + `_poll_set_stack` helper | 2 |
| `tests/engines/test_resolve_transformer.py` | Unit: `_check_branch_legal` + resolver parity | 0 |
| `tests/engines/diffusers/servers/test_set_stack_async_job.py` | Unit: submit/job/status behavior | 1 |
| `tests/engines/test_diffusers_set_lora_stack.py` | Unit: client submit+poll terminal mapping | 2 |
| `tests/_smoke_harness/matrix.py` + `tests/_smoke_harness/test_matrix.py` | Harness: submit+poll driver + URLError/HTTPError split (logging fix) | 3 |
| `tests/engines/diffusers/_golden_provision.json` | Regenerated after each server edit (embedded module bytes change) | 0, 1 |
| `tests/smoke/live_wan21/test_branch_routing.py` + `test_lora_swap_matrix.py` | Live smoke updated to submit+poll; RED scaffold before spend | 4 |

---

## Task 0: Extract shared branch-legality gate `_check_branch_legal`

**Goal:** Extract the branch-legality decision out of `_resolve_transformer` into a pure `_check_branch_legal(branch, arity)` so submit-time (Task 1) and load-time reject identically — a request that passes submit can never fail the branch gate at load. Behavior-preserving refactor.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (near `_resolve_transformer`, ~line 626)
- Test: `tests/engines/test_resolve_transformer.py`
- Regenerate: `tests/engines/diffusers/_golden_provision.json`

**Acceptance Criteria:**
- [ ] `_check_branch_legal(branch, arity)` raises `BranchUnsupportedOnSingleTransformer` (arity 1 + explicit branch), `BranchAutoNotAllowedOnMoE` (arity>1 + auto), `BranchUnknown` (arity>1 + off-Literal), and returns None for every legal pair.
- [ ] `_resolve_transformer` still returns the same transformer and raises the same exceptions as before (parity).
- [ ] Golden provision test passes (regenerated; diff is embed-content-only).

**Verify:** `pixi run pytest tests/engines/test_resolve_transformer.py tests/engines/diffusers/test_render_provision_split.py -q` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests** — append to `tests/engines/test_resolve_transformer.py`:

```python
import pytest

from kinoforge.engines.diffusers.servers.wan_t2v_server import (
    BranchAutoNotAllowedOnMoE,
    BranchUnknown,
    BranchUnsupportedOnSingleTransformer,
    _check_branch_legal,
)


def test_check_branch_legal_arity1_allows_auto():
    # Bug caught: a regression that rejects auto on Wan 2.1 breaks every
    # single-transformer deployment.
    _check_branch_legal("auto", 1)  # must not raise


def test_check_branch_legal_arity1_rejects_explicit():
    # Bug caught: single-transformer pipe silently accepts high_noise
    # (Q5 strict-reject violation).
    with pytest.raises(BranchUnsupportedOnSingleTransformer):
        _check_branch_legal("high_noise", 1)


def test_check_branch_legal_moe_rejects_auto():
    with pytest.raises(BranchAutoNotAllowedOnMoE):
        _check_branch_legal("auto", 2)


def test_check_branch_legal_moe_allows_explicit():
    _check_branch_legal("high_noise", 2)
    _check_branch_legal("low_noise", 2)


def test_check_branch_legal_moe_rejects_unknown():
    with pytest.raises(BranchUnknown):
        _check_branch_legal("sideways", 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run pytest tests/engines/test_resolve_transformer.py -k check_branch_legal -q`
Expected: FAIL — `ImportError: cannot import name '_check_branch_legal'`

- [ ] **Step 3: Add `_check_branch_legal` and route `_resolve_transformer` through it.** In `wan_t2v_server.py`, insert `_check_branch_legal` immediately above `_resolve_transformer` and replace the resolver body:

```python
def _check_branch_legal(branch: str, arity: int) -> None:
    """Raise if ``branch`` is illegal for a pipeline with ``arity`` transformers.

    Pure legality gate — no pipe object needed. Shared by the submit-time
    hoist in ``set_stack`` and the load-time ``_resolve_transformer`` dispatch,
    so a request that passes the submit-time check can never be rejected by the
    branch gate at load (spec §Risks — branch-gate hoist).

    Raises:
        BranchUnsupportedOnSingleTransformer: explicit branch on ``arity == 1``.
        BranchAutoNotAllowedOnMoE: ``branch == "auto"`` on ``arity > 1``.
        BranchUnknown: off-Literal value on ``arity > 1``.
    """
    if arity == 1:
        if branch != "auto":
            raise BranchUnsupportedOnSingleTransformer(branch=branch, arity=arity)
        return
    if branch == "auto":
        raise BranchAutoNotAllowedOnMoE(arity=arity)
    if branch not in ("high_noise", "low_noise"):
        raise BranchUnknown(branch=branch)


def _resolve_transformer(pipe_obj: Any, branch: str) -> Any:  # noqa: ANN401
    """Map ``(pipe_obj, branch)`` to the target transformer attribute.

    Single dispatch point — every LoRA-load call site (``/lora/set_stack``
    handler, cold-boot loop, VRAM-OOM rollback) routes through this helper.
    Legality is delegated to ``_check_branch_legal`` so submit-time and
    load-time reject identically.
    """
    arity = _pipe_arity
    _check_branch_legal(branch, arity)
    if arity == 1:
        return pipe_obj.transformer
    if branch == "high_noise":
        return pipe_obj.transformer
    return pipe_obj.transformer_2  # low_noise — the only legal remainder
```

- [ ] **Step 4: Run tests to verify pass (new + existing resolver parity)**

Run: `pixi run pytest tests/engines/test_resolve_transformer.py -q`
Expected: PASS (new gate tests + all existing resolver tests still green)

- [ ] **Step 5: Regenerate the golden provision script** (the embedded `wan_t2v_server.py` bytes changed):

```bash
pixi run python -c "
from kinoforge.core.config import load_config
from kinoforge.engines.diffusers import DiffusersEngine
import json, pathlib
p = pathlib.Path('tests/engines/diffusers/_golden_provision.json')
out = {}
for name in json.loads(p.read_text()):
    out[name] = DiffusersEngine().render_provision(load_config(name).model_dump()).script
p.write_text(json.dumps(out, indent=0))
print('regenerated', {k: len(v) for k, v in out.items()})
"
```

Run: `pixi run pytest tests/engines/diffusers/test_render_provision_split.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_resolve_transformer.py tests/engines/diffusers/_golden_provision.json
git commit -m "refactor(wan-server): extract _check_branch_legal shared gate (submit/load parity)"
```

---

## Task 1: Split `set_stack` into submit + `_run_swap_job` + status endpoint

**Goal:** POST `/lora/set_stack` validates synchronously (shape / hoisted branch-legality / plan-time disk) then enqueues an `asyncio.create_task` job and returns `{"job_id"}`; the download + load + rollback body runs in `_run_swap_job` under `_swap_lock`, writing a terminal record to `_swap_jobs`; `GET /lora/set_stack/status/{job_id}` returns it.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (`set_stack` at ~1475-1778; add `_swap_jobs` near the other job registries ~line 94)
- Test: `tests/engines/diffusers/servers/test_set_stack_async_job.py` (new)
- Regenerate: `tests/engines/diffusers/_golden_provision.json`

**Acceptance Criteria:**
- [ ] POST with an illegal branch returns HTTP 400 with the same body as before (`branch_unsupported_single_transformer` / `branch_auto_disallowed_on_moe`), **without** invoking `_download_one`.
- [ ] POST with a plan that cannot fit even after full eviction returns HTTP 507 `phase:"plan"` synchronously.
- [ ] POST on the happy path returns HTTP 200 `{"job_id": <hex>}` and `_download_one` runs inside the job, not the request.
- [ ] `_run_swap_job` writes payload then flips `state` last; terminal states are `done` (with inventory/free_bytes/optional swap_rejected) or `error` (with `status`+structured body).
- [ ] `GET /lora/set_stack/status/{job_id}` returns the record; unknown id → 404.
- [ ] All existing set_stack tests (`tests/engines/diffusers/servers/test_set_stack_swap_gaps.py`, `tests/engines/test_lora_http_branch_surface.py`, `tests/engines/test_vram_rollback_branch.py`) updated to the submit+poll shape and passing.
- [ ] Golden provision test passes (regenerated).

**Verify:** `pixi run pytest tests/engines/diffusers/servers/ tests/engines/test_lora_http_branch_surface.py tests/engines/test_vram_rollback_branch.py tests/engines/diffusers/test_render_provision_split.py -q` → all pass

**Steps:**

- [ ] **Step 1: Add the `_swap_jobs` registry.** Near the other in-process registries (~line 94, beside `jobs`/`_upscale_jobs`):

```python
# Async /lora/set_stack jobs. Keyed by job_id; mutated by _run_swap_job.
# In-memory + volatile like _upscale_jobs / _interpolate_jobs — a pod
# restart implies cold-boot, so lost job state is acceptable.
_swap_jobs: dict[str, dict[str, Any]] = {}
```

- [ ] **Step 2: Write the failing tests** — `tests/engines/diffusers/servers/test_set_stack_async_job.py` (new). These use FastAPI `TestClient` with `_download_one` and `_replace_adapter_stack` monkeypatched so no real GPU/network is touched:

```python
"""Job-based /lora/set_stack: sync submit-time rejects + async job + status."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from kinoforge.engines.diffusers.servers import wan_t2v_server as srv


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Single-transformer pipe (Wan 2.1): arity 1.
    monkeypatch.setattr(srv, "_pipe_arity", 1)
    srv._inventory.clear()
    srv._swap_jobs.clear()
    srv.ready.set()
    return TestClient(srv.app)


def _spec(size: int = 10) -> dict[str, Any]:
    return {"url": "https://x/f.safetensors", "headers": {}, "filename": "f.safetensors", "size_hint": size}


def test_illegal_branch_rejected_400_without_download(client, monkeypatch):
    # Bug caught: the doomed branch request downloads 350MB before it can
    # 400 (today's behavior) — the exact cause of the branch-test 502s.
    calls: list[Any] = []
    monkeypatch.setattr(srv, "_download_one", lambda *a, **k: calls.append(a) or ("/x", 10))
    resp = client.post(
        "/lora/set_stack",
        json={"target": [{"ref": "r", "strength": 1.0, "branch": "high_noise"}],
              "download_specs": {"r": _spec()}},
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["error"] == "branch_routing"
    assert body["reason"] == "branch_unsupported_single_transformer"
    assert calls == []  # never downloaded


def test_happy_submit_returns_job_id_and_downloads_in_job(client, monkeypatch):
    seen: list[Any] = []
    monkeypatch.setattr(srv, "_download_one", lambda spec, d: seen.append(spec.filename) or ("/loras/f.safetensors", 10))
    monkeypatch.setattr(srv, "_replace_adapter_stack", lambda target: None)
    resp = client.post(
        "/lora/set_stack",
        json={"target": [{"ref": "r", "strength": 1.0, "branch": "auto"}],
              "download_specs": {"r": _spec()}},
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


def test_status_unknown_job_404(client):
    assert client.get("/lora/set_stack/status/nope").status_code == 404


def test_download_failure_surfaces_as_error_state_with_status(client, monkeypatch):
    # Bug caught: a download failure that isn't legible to the client — the
    # error payload must carry status=502 so _raise_lora_swap_error routes it.
    def boom(spec, d):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(srv, "_download_one", boom)
    resp = client.post(
        "/lora/set_stack",
        json={"target": [{"ref": "r", "strength": 1.0, "branch": "auto"}],
              "download_specs": {"r": _spec()}},
    )
    job_id = resp.json()["job_id"]
    status = client.get(f"/lora/set_stack/status/{job_id}").json()
    assert status["state"] == "error"
    assert status["error"]["status"] == 502
    assert status["error"]["error"] == "lora_download_failed"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pixi run pytest tests/engines/diffusers/servers/test_set_stack_async_job.py -q`
Expected: FAIL — the status route + `{job_id}` submit shape don't exist yet.

- [ ] **Step 4: Refactor the handler.** Rename the current `async def set_stack(...)` body to `async def _run_swap_job(job_id: str, req: SetStackRequest) -> None`, and write a new thin `set_stack` submit endpoint above it. Concretely:

  1. **New submit endpoint** (replaces the `@app.post("/lora/set_stack")` signature). Synchronous validation, then enqueue:

```python
@app.post("/lora/set_stack")
async def set_stack(req: SetStackRequest) -> dict[str, str]:
    """Validate synchronously, enqueue the swap job, return its id.

    Cheap/instant rejections stay synchronous HTTP 4xx here so the client's
    ``_raise_lora_swap_error`` translation and the branch-routing smoke's
    ``pytest.raises(HTTPError, 400)`` are unchanged, and a doomed request never
    pays a download. The long path (download + load + rollback) runs in
    ``_run_swap_job`` and is polled via ``/lora/set_stack/status/{job_id}``.
    """
    # Branch-legality, hoisted before any download. Pure function of the
    # cached pipeline arity + each target branch (a swap never changes arity).
    for t in req.target:
        try:
            _check_branch_legal(t.branch, _pipe_arity)
        except BranchAutoNotAllowedOnMoE as e:
            raise HTTPException(status_code=400, detail={
                "error": "branch_routing",
                "reason": "branch_auto_disallowed_on_moe", "arity": e.arity}) from e
        except BranchUnsupportedOnSingleTransformer as e:
            raise HTTPException(status_code=400, detail={
                "error": "branch_routing",
                "reason": "branch_unsupported_single_transformer",
                "branch": e.branch, "arity": e.arity}) from e
        except BranchUnknown as e:
            raise HTTPException(status_code=500, detail={
                "error": "branch_routing", "reason": "branch_unknown",
                "branch": e.branch}) from e

    job_id = f"s-{uuid.uuid4().hex}"
    _swap_jobs[job_id] = {
        "state": "queued", "inventory": None, "free_bytes": None,
        "swap_rejected": None, "error": None,
    }
    asyncio.create_task(_run_swap_job(job_id, req))
    return {"job_id": job_id}
```

  2. **Job runner.** Rename the existing handler body to `_run_swap_job(job_id, req)`. Wrap the existing `async with _swap_lock:` block in a `try/except` that records terminal state. Replace every `raise HTTPException(...)` that today left the handler with a `_swap_jobs[job_id]` write. The branch-load excepts (`BranchAutoNotAllowedOnMoE` etc. at old lines ~1688-1710) are now unreachable at load (hoisted to submit) — keep them as a defensive `error`-state write (status 500, `reason:"branch_gate_load_miss"`) rather than deleting, so a hoist/gate drift surfaces loudly. Terminal writes (payload first, `state` last):

```python
async def _run_swap_job(job_id: str, req: SetStackRequest) -> None:
    """Run one declarative LoRA swap; record terminal state into _swap_jobs.

    Body is the pre-async set_stack handler verbatim (pending-entry seeding,
    evict plan, evict, download loop, _replace_adapter_stack, VRAM-OOM
    rollback) minus the hoisted branch gate. Writes payload then flips
    ``state`` last so a poller that sees a terminal state always sees a
    populated payload (matches _run_upscale_job's ordering).
    """
    _swap_jobs[job_id]["state"] = "running"
    try:
        async with _swap_lock:
            # ---- BEGIN verbatim pre-async body (target_keys, seeding,
            # evict plan, plan-time disk check, evict loop, download loop,
            # _replace_adapter_stack, VRAM-OOM rollback) ----
            #   * The plan-time disk 507 raise stays (HTTPException) but is
            #     caught below and recorded as error state=507 phase=plan.
            #   * The download-loop 502/507 raises likewise recorded.
            #   * On the success return, instead of `return SetStackResponse(...)`
            #     write the done payload:
            _swap_jobs[job_id]["inventory"] = [e.model_dump() for e in _inventory_snapshot()]
            _swap_jobs[job_id]["free_bytes"] = _disk_free_bytes(LORAS_DIR)
            _swap_jobs[job_id]["swap_rejected"] = None  # or the vram_oom dict
            _swap_jobs[job_id]["state"] = "done"
    except HTTPException as he:
        # 507/502/500 structured bodies from the body above. Stamp the
        # HTTP-equivalent status so the client reuses _raise_lora_swap_error.
        detail = he.detail if isinstance(he.detail, dict) else {"error": str(he.detail)}
        _swap_jobs[job_id]["error"] = {**detail, "status": he.status_code}
        _swap_jobs[job_id]["state"] = "error"
    except Exception as e:  # noqa: BLE001 — poller must always terminate
        _log.exception("swap job %s failed", job_id)
        _swap_jobs[job_id]["error"] = {"error": "lora_swap_failed", "status": 500, "underlying": str(e)}
        _swap_jobs[job_id]["state"] = "error"
```

  For the **VRAM-OOM / set_adapters_value_error** branch (today `return SetStackResponse(..., swap_rejected=SwapRejectedDetails(...))`), record a non-error `done` with the reject dict instead of returning:

```python
            # inside the (RuntimeError, ValueError) handler, replacing the
            # `return SetStackResponse(... swap_rejected=...)`:
            _swap_jobs[job_id]["inventory"] = [e.model_dump() for e in _inventory_snapshot()]
            _swap_jobs[job_id]["free_bytes"] = _disk_free_bytes(LORAS_DIR)
            _swap_jobs[job_id]["swap_rejected"] = {
                "reason": "vram_oom" if is_oom else "set_adapters_value_error",
                "target_refs_dropped": dropped,
            }
            _swap_jobs[job_id]["state"] = "done"
            return
```

  3. **Status endpoint** (beside the handler):

```python
@app.get("/lora/set_stack/status/{job_id}")
def set_stack_status(job_id: str) -> dict[str, Any]:
    """Return the swap job record; 404 if unknown. Mirrors /upscale/status."""
    payload = _swap_jobs.get(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    return payload
```

- [ ] **Step 5: Update existing set_stack server tests** to the submit+poll shape. In `tests/engines/diffusers/servers/test_set_stack_swap_gaps.py`, `tests/engines/test_lora_http_branch_surface.py`, `tests/engines/test_vram_rollback_branch.py`: every assertion that read the POST response body for `inventory`/`swap_rejected` now (a) reads `job_id` from the 200, then (b) GETs `/lora/set_stack/status/{job_id}` and asserts on `state` + `inventory` + `swap_rejected`. Illegal-branch tests keep asserting the synchronous 400 on the POST directly. (The branch-load defensive excepts are covered by a new negative test only if reachable; skip otherwise.)

- [ ] **Step 6: Run the full server test set to verify pass**

Run: `pixi run pytest tests/engines/diffusers/servers/ tests/engines/test_lora_http_branch_surface.py tests/engines/test_vram_rollback_branch.py -q`
Expected: PASS

- [ ] **Step 7: Regenerate golden + verify**

```bash
pixi run python -c "
from kinoforge.core.config import load_config
from kinoforge.engines.diffusers import DiffusersEngine
import json, pathlib
p = pathlib.Path('tests/engines/diffusers/_golden_provision.json')
out = {n: DiffusersEngine().render_provision(load_config(n).model_dump()).script for n in json.loads(p.read_text())}
p.write_text(json.dumps(out, indent=0)); print('regenerated')
"
pixi run pytest tests/engines/diffusers/test_render_provision_split.py -q
```
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/ 
git commit -m "feat(wan-server): job-based /lora/set_stack (submit + async job + status endpoint)"
```

---

## Task 2: Client `set_lora_stack` submits + polls internally

**Goal:** `DiffusersEngine.set_lora_stack` posts, reads `{job_id}`, polls `GET /lora/set_stack/status/{job_id}` (mirroring the existing `result()` poll), and maps terminal states back to today's exact return value / raised exceptions. Signature + callers unchanged.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py` (`set_lora_stack` ~611-700; add `_poll_set_stack` helper)
- Test: `tests/engines/test_diffusers_set_lora_stack.py`

**Acceptance Criteria:**
- [ ] `set_lora_stack` returns `{inventory, free_bytes, swap_rejected}` when the job reaches `done` with no reject.
- [ ] Job `done` + `swap_rejected.reason=="vram_oom"` → raises `LoraSwapVramOomError` with `dropped_refs`.
- [ ] Job `error` with `status:502` + empty `evict_completed` → `LoraSwapDownloadError`; non-empty → `LoraSwapDegradedPodError`; `status:507` → `LoraSwapDiskFullError` (via unchanged `_raise_lora_swap_error`).
- [ ] Poll wall-clock timeout → `LoraSwapPodUnreachableError`.
- [ ] Submit-time 507 (sync) still → `LoraSwapDiskFullError`; transport failure → `LoraSwapPodUnreachableError`.

**Verify:** `pixi run pytest tests/engines/test_diffusers_set_lora_stack.py -q` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests** — extend `tests/engines/test_diffusers_set_lora_stack.py`. Drive the engine with stub `http_post`/`http_get` that return submit `{job_id}` then a scripted sequence of status payloads:

```python
def _engine(http_post, http_get):
    from kinoforge.engines.diffusers import DiffusersEngine
    from tests.engines._helpers import make_probe_profile  # existing helper
    return DiffusersEngine(
        http_post=http_post, http_get=http_get,
        base_url="http://pod:8000", probe_profile=make_probe_profile(),
        sleep=lambda s: None, poll_interval_s=0.0,
    )


def _entry(ref):
    from kinoforge.core.lora import LoraEntry
    return LoraEntry(ref=ref, strength=1.0, branch="auto")


def test_set_lora_stack_done_returns_inventory():
    # Bug caught: client fails to reconstruct the result dict from status.
    posts = [{"job_id": "s-1"}]
    gets = [{"state": "running", "inventory": None, "free_bytes": None, "swap_rejected": None, "error": None},
            {"state": "done", "inventory": [{"ref": "r"}], "free_bytes": 5, "swap_rejected": None, "error": None}]
    eng = _engine(lambda u, b: posts.pop(0), lambda u: gets.pop(0))
    out = eng.set_lora_stack(pod_id="p", active_stack=[_entry("r")], download_specs={"r": {}})
    assert out["inventory"] == [{"ref": "r"}]
    assert out["free_bytes"] == 5


def test_set_lora_stack_vram_oom_raises():
    from kinoforge.core.errors import LoraSwapVramOomError
    posts = [{"job_id": "s-2"}]
    gets = [{"state": "done", "inventory": [], "free_bytes": 5,
             "swap_rejected": {"reason": "vram_oom", "target_refs_dropped": ["r"]}, "error": None}]
    eng = _engine(lambda u, b: posts.pop(0), lambda u: gets.pop(0))
    with pytest.raises(LoraSwapVramOomError):
        eng.set_lora_stack(pod_id="p", active_stack=[_entry("r")], download_specs={"r": {}})


def test_set_lora_stack_error_download_maps_to_download_error():
    from kinoforge.core.errors import LoraSwapDownloadError
    posts = [{"job_id": "s-3"}]
    gets = [{"state": "error", "inventory": None, "free_bytes": None, "swap_rejected": None,
             "error": {"error": "lora_download_failed", "status": 502, "evict_completed": [],
                       "download_failed": "r", "underlying": "reset"}}]
    eng = _engine(lambda u, b: posts.pop(0), lambda u: gets.pop(0))
    with pytest.raises(LoraSwapDownloadError):
        eng.set_lora_stack(pod_id="p", active_stack=[_entry("r")], download_specs={"r": {}})
```

- [ ] **Step 2: Run to verify fail**

Run: `pixi run pytest tests/engines/test_diffusers_set_lora_stack.py -k "done_returns or vram_oom_raises or download_maps" -q`
Expected: FAIL (client still expects a synchronous inventory response).

- [ ] **Step 3: Rewrite `set_lora_stack` internals + add `_poll_set_stack`.** Replace the body from the `resp = retry_proxy_call(...)` submit through the `return resp` tail with:

```python
        try:
            submit = retry_proxy_call(
                "diffusers.lora.set_stack", url,
                lambda: self._http_post(url, body), self._sleep, RUNPOD_PROXY_POLICY,
            )
        except Exception as e:
            status = getattr(e, "status", None)
            body_attr = getattr(e, "body", None)
            if status is not None and isinstance(body_attr, dict):
                # Submit-time sync rejection (507 disk / defensive branch).
                self._raise_lora_swap_error(int(status), body_attr, pod_id)
            raise LoraSwapPodUnreachableError(pod_id=pod_id, underlying=str(e)) from e

        job_id = str(submit["job_id"])
        result = self._poll_set_stack(job_id, pod_id)
        sr = result.get("swap_rejected")
        if isinstance(sr, dict) and sr.get("reason") == "vram_oom":
            raise LoraSwapVramOomError(
                pod_id=pod_id, dropped_refs=list(sr.get("target_refs_dropped", [])))
        return result
```

  Add the poll helper (mirrors `result()` transient absorption, keyed to the swap status payload):

```python
    def _poll_set_stack(self, job_id: str, pod_id: str) -> dict[str, Any]:
        """Poll /lora/set_stack/status/{job_id} to a terminal state.

        Returns the reconstructed ``{inventory, free_bytes, swap_rejected}``
        dict on ``done``; raises the LoraSwap* exception matching the ``error``
        payload; raises LoraSwapPodUnreachableError on wall-clock timeout.
        """
        from kinoforge.core.errors import LoraSwapPodUnreachableError
        from kinoforge.engines._proxy_retry import (
            RUNPOD_PROXY_POLICY, interpoll_wait, retry_proxy_call,
        )

        url = f"{self._base_url}/lora/set_stack/status/{job_id}"
        start = time.monotonic()
        while True:
            if time.monotonic() - start > self._poll_timeout_s:
                raise LoraSwapPodUnreachableError(
                    pod_id=pod_id,
                    underlying=f"set_stack job {job_id} poll timed out",
                )
            data = retry_proxy_call(
                "diffusers.lora.set_stack.status", url,
                lambda: self._http_get(url), self._sleep, RUNPOD_PROXY_POLICY,
            )
            state = data.get("state")
            if state == "done":
                return {
                    "inventory": data.get("inventory"),
                    "free_bytes": data.get("free_bytes"),
                    "swap_rejected": data.get("swap_rejected"),
                }
            if state == "error":
                err = data.get("error") or {}
                self._raise_lora_swap_error(int(err.get("status", 500)), err, pod_id)
            interpoll_wait(self._poll_interval_s, None, self._sleep)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pixi run pytest tests/engines/test_diffusers_set_lora_stack.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/engines/diffusers/__init__.py tests/engines/test_diffusers_set_lora_stack.py
git commit -m "feat(diffusers-client): set_lora_stack submits + polls the async swap job"
```

---

## Task 3: Harness submit+poll driver + logging fix

**Goal:** `matrix.run_matrix` drives set_stack via submit+poll on the status endpoint; the `except urllib.error.URLError` swallow that also caught `HTTPError` is split so real HTTP errors surface distinctly; failures assert with the actual `status.error` payload, not a blind `last observed []`.

**Files:**
- Modify: `tests/_smoke_harness/matrix.py` (the `run_matrix` set_stack call + retire `_wait_for_inventory_convergence`)
- Test: `tests/_smoke_harness/test_matrix.py`

**Acceptance Criteria:**
- [ ] `run_matrix` submits set_stack, polls `/lora/set_stack/status/{job_id}` to terminal, then asserts `/lora/inventory == expected`.
- [ ] A job `error` payload surfaces the real `error` string in the raised `AssertionError` (not `last observed []`).
- [ ] A genuine `HTTPError` from the status GET is NOT swallowed as a transient `URLError` (they are caught in separate branches).
- [ ] Existing `test_matrix.py` cases updated; the obsolete `test_run_matrix_recovers_from_proxy_502_via_inventory_poll` replaced by a job-status-poll equivalent.

**Verify:** `pixi run pytest tests/_smoke_harness/test_matrix.py -q` → all pass

**Steps:**

- [ ] **Step 1: Write/adjust the failing tests** in `tests/_smoke_harness/test_matrix.py`:

```python
def test_run_matrix_polls_swap_job_to_done(monkeypatch):
    # Bug caught: harness never learns the swap finished because it polls the
    # wrong signal.
    posts = {"count": 0}
    def fake_post(url, body, *, timeout):
        posts["count"] += 1
        return {"job_id": "s-1"}
    swap_status = [{"state": "running"}, {"state": "done"}]
    inv = {"inventory": [{"ref": "a"}]}
    def fake_get(url, *, timeout):
        if url.endswith("/lora/set_stack/status/s-1"):
            return swap_status.pop(0)
        return inv  # /lora/inventory
    monkeypatch.setattr(matrix.http, "post_json", fake_post)
    monkeypatch.setattr(matrix.http, "get_json", fake_get)
    report = matrix.run_matrix(
        cfg_path=Path("x"), pod_proxy_url="http://pod:8000",
        steps=[matrix.MatrixStep(name="s", target_stack=["a"], expected_inventory=["a"])],
        download_specs={"a": {"url": "u", "headers": {}, "filename": "a", "size_hint": 1}},
        generate_per_step=False,
    )
    assert report.steps[0].inventory_after == ["a"]


def test_run_matrix_surfaces_real_error_payload(monkeypatch):
    # Bug caught: the 3-week misdiagnosis — a real failure logged as
    # `last observed []` instead of the server's error string.
    monkeypatch.setattr(matrix.http, "post_json", lambda u, b, *, timeout: {"job_id": "s-9"})
    def fake_get(url, *, timeout):
        return {"state": "error", "error": {"error": "lora_download_failed", "underlying": "connection reset"}}
    monkeypatch.setattr(matrix.http, "get_json", fake_get)
    with pytest.raises(AssertionError, match="connection reset"):
        matrix.run_matrix(
            cfg_path=Path("x"), pod_proxy_url="http://pod:8000",
            steps=[matrix.MatrixStep(name="s", target_stack=["a"], expected_inventory=["a"])],
            download_specs={"a": {"url": "u", "headers": {}, "filename": "a", "size_hint": 1}},
            generate_per_step=False,
        )
```

- [ ] **Step 2: Run to verify fail**

Run: `pixi run pytest tests/_smoke_harness/test_matrix.py -k "polls_swap_job or surfaces_real_error" -q`
Expected: FAIL — `run_matrix` still posts synchronously and calls `_wait_for_inventory_convergence`.

- [ ] **Step 3: Replace the set_stack call in `run_matrix`.** Swap the `try: resp = http.post_json(... set_stack ...) except HTTPError → _wait_for_inventory_convergence` block for submit+poll, and delete `_wait_for_inventory_convergence`:

```python
        submit = http.post_json(
            f"{pod_proxy_url.rstrip('/')}/lora/set_stack",
            {"target_refs": step.target_stack, "download_specs": sliced},
            timeout=120,
        )
        job_id = submit["job_id"]
        _poll_swap_job(pod_proxy_url, job_id, step_name=step.name, deadline_s=1800.0)
        inv_resp = http.get_json(f"{pod_proxy_url.rstrip('/')}/lora/inventory", timeout=30)
        observed = sorted(e["ref"] for e in inv_resp.get("inventory", []))
```

  Add the poll helper (replaces `_wait_for_inventory_convergence`; note the URLError/HTTPError split — the logging fix):

```python
def _poll_swap_job(pod_proxy_url: str, job_id: str, *, step_name: str, deadline_s: float) -> None:
    """Poll /lora/set_stack/status/{job_id} until terminal.

    HTTPError and URLError are caught in SEPARATE branches: a real HTTP error
    (e.g. 500) must surface, not be absorbed as a transient transport blip the
    way the retired _wait_for_inventory_convergence swallowed it (the defect
    that made every failure read as `last observed []`).
    """
    url = f"{pod_proxy_url.rstrip('/')}/lora/set_stack/status/{job_id}"
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            data = http.get_json(url, timeout=30)
        except urllib.error.HTTPError:
            raise  # real HTTP error — never swallow
        except urllib.error.URLError:
            time.sleep(5.0)  # genuine transient transport blip
            continue
        state = data.get("state")
        if state == "done":
            return
        if state == "error":
            raise AssertionError(f"{step_name}: swap job failed — {data.get('error')}")
        time.sleep(5.0)
    raise AssertionError(f"{step_name}: swap job {job_id} did not finish within {deadline_s}s")
```

- [ ] **Step 4: Update/remove obsolete tests.** Delete `test_run_matrix_recovers_from_proxy_502_via_inventory_poll` (the recovery hack is gone); keep `test_run_matrix_propagates_non_502_http_errors` but retarget it at `_poll_swap_job` raising on HTTPError.

- [ ] **Step 5: Run to verify pass**

Run: `pixi run pytest tests/_smoke_harness/test_matrix.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/_smoke_harness/matrix.py tests/_smoke_harness/test_matrix.py
git commit -m "test(harness): submit+poll swap driver + split URLError/HTTPError (kills last-observed-[] blindness)"
```

---

## Task 4: Live re-validation (RED scaffold before spend, then gated smoke)

**Goal:** Update the live smoke files to submit+poll semantics, commit them as a RED scaffold BEFORE any spend (durability rule), then run `smoke-21b-live` on a real A5000 and prove the 350 MB swap completes via poll with no 502.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `tests/smoke/live_wan21/test_lora_swap_matrix.py` (drives the updated `run_matrix` — likely no change if the harness change is transparent; verify)
- Modify: `tests/smoke/live_wan21/test_branch_routing.py` (`test_auto_branch_succeeds_on_wan21` → submit+poll then assert inventory; `test_explicit_high_noise…` keeps the synchronous `pytest.raises(HTTPError, 400)`, now no download)

**Acceptance Criteria:**
- [ ] RED scaffold (both smoke files at their new submit+poll shape) committed BEFORE the live run.
- [ ] `pixi run preflight` exits 0 immediately before spend.
- [ ] `test_lora_swap_matrix_wan21`: 4 steps pass, no 502, distinct mp4 shas per step.
- [ ] `test_auto_branch_succeeds_on_wan21` passes via submit+poll; `test_explicit_high_noise_branch_rejected_on_wan21` returns synchronous 400 with no download.
- [ ] Pod utilisation polled during the run (GPU% surfaced, not just est_spend).
- [ ] Every output video frame-QA'd; verdict recorded.
- [ ] After the run, `pixi run kinoforge list` shows no running instances AND empty ledger.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run smoke-21b-live` → 4 passed (matrix) + 2 passed (branch) with captured GPU-util + frame-QA verdict; then `pixi run kinoforge list` → "No running instances" + "No instances recorded in ledger."

**Steps:**

- [ ] **Step 1: Update `test_branch_routing.py::test_auto_branch_succeeds_on_wan21`** to submit+poll. Replace the single `http.post_json(... set_stack ...)` + inline inventory read with: POST set_stack → read `job_id` → poll `/lora/set_stack/status/{job_id}` to `done` (reuse a local poll helper or `matrix._poll_swap_job`) → GET `/lora/inventory` → assert the `auto` entry. Leave `test_explicit_high_noise_branch_rejected_on_wan21` asserting the synchronous 400 (unchanged; it now returns before any download).

- [ ] **Step 2: Confirm `test_lora_swap_matrix.py` needs no change** beyond the transparent harness swap (it calls `matrix.run_matrix`). If it references `_wait_for_inventory_convergence` directly, retarget. Run offline collection to confirm import health:

Run: `pixi run pytest tests/smoke/live_wan21/ --collect-only -q`
Expected: collects with no import errors (tests skip without `KINOFORGE_LIVE_TESTS=1`).

- [ ] **Step 3: Commit the RED scaffold BEFORE spend** (durability rule — committed even though live-gated tests can't be green offline):

```bash
git add tests/smoke/live_wan21/test_branch_routing.py tests/smoke/live_wan21/test_lora_swap_matrix.py
git commit -m "test(live): submit+poll set_stack in wan21 smoke (RED scaffold, pre-spend)"
```

- [ ] **Step 4: Preflight, then run the gated live smoke.** Poll pod GPU-util every 60-90 s during the run (live-smoke monitoring rule) using the `RunPodGraphQLUtilEndpoint` probe:

```bash
pixi run preflight   # must exit 0
KINOFORGE_LIVE_TESTS=1 pixi run smoke-21b-live 2>&1 | tee /tmp/smoke-21b.log
```

- [ ] **Step 5: Frame-QA every output video** (mandatory visual-QA rule) with `kinoforge.core.frames.ffmpeg_frames_by_count` (~5 frames), read the contact sheet, record a verdict (artifacts / temporal coherence / prompt adherence). Flag anything not clearly high quality with ⚠️.

- [ ] **Step 6: Verify teardown** (never trust a mid-run log line):

```bash
pixi run kinoforge list
```
Expected: "No running instances." AND "No instances recorded in ledger." If either shows a pod: `pixi run kinoforge destroy --id <pod-id>`.

- [ ] **Step 7: Record evidence + commit.** Add a PROGRESS note (weekly-smoke now green via job-based swap) and, since this is a repro-fix not a new capability axis, NO `successful-generations.md` entry. Commit docs.

```bash
git add PROGRESS.md
git commit -m "docs(progress): smoke-wan21-weekly green — job-based set_stack live-verified (no 502)"
```

---

## Self-review notes

- **Spec coverage:** §1 submit → Task 1; §2 job runner → Task 1; §3 status → Task 1; §4 client → Task 2; §5 harness+logging → Task 3; §6 testing offline → Tasks 0-3, live → Task 4; branch-gate hoist risk → Task 0 (shared helper). All covered.
- **Golden regen:** folded into Tasks 0 and 1 (both edit the embedded `wan_t2v_server.py`) so every task stays green — no orphan golden failure.
- **Type consistency:** status payload shape `{state, inventory, free_bytes, swap_rejected, error}` is identical across server write (Task 1), client read (Task 2), and harness read (Task 3); error payload always carries `status`.
