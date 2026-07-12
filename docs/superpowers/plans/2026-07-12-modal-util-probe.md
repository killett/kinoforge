# Modal util probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Modal a GPU/CPU/mem util probe by implementing the existing `core.util_endpoints.UtilSnapshotEndpoint` contract — an in-container `/util` endpoint on the diffusers server plus a `ModalUtilEndpoint` the orchestrator polls — so `provider_util_supported("modal")` is true and the live-smoke "0% GPU = dead pod" rule works on Modal.

**Architecture:** Server-side `GpuStatsReader` seam (pynvml primary → nvidia-smi fallback → psutil) behind a sync `/util` route returns a plain dict (server has no `kinoforge.core`); controller-side `ModalUtilEndpoint.read_util` resolves `instance_id → .modal.run` URL via the ledger (the §26 endpoints fix), GETs `/util`, and maps the dict to a `UtilSnapshot`. Register `"modal"` in `_UTIL_SUPPORTED` and wire the endpoint into `build_util_endpoint_for` with a ledger-backed resolver threaded from the orchestrator.

**Tech Stack:** Python 3.13, FastAPI (`wan_t2v_server`), pynvml (`nvidia-ml-py`) + `psutil` (optional, graceful-degrade) + `nvidia-smi` fallback, Modal SDK (`live-modal` env), the `core/util_endpoints.py` contract, pytest.

**User decisions (already made):**
- **GPU-util source: reader seam** — pynvml primary + nvidia-smi fallback, both → the `UtilSnapshot` fields. Selected 2026-07-12.
- **Scope: snapshot-only**, implementing the EXISTING shared `core/util_endpoints.py` contract (no contract build/hoist — it already exists); boot-liveness/STALL-reap deferred. Selected 2026-07-12.
- **id→URL resolution: ledger-resolution** (reuse the §26 persisted endpoints) via an injected resolver seam, so `read_util` works cross-process. Selected 2026-07-12.

Spec: `docs/superpowers/specs/2026-07-12-modal-util-probe-design.md`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/kinoforge/engines/diffusers/servers/_util_stats.py` (create) | `read_gpu_stats() -> dict` — pynvml→nvidia-smi→psutil, self-contained (NO kinoforge.core import), never raises | 0 |
| `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (modify) | sync `def util()` route returning `read_gpu_stats()` | 0 |
| `src/kinoforge/providers/modal/util.py` (create) | `ModalUtilEndpoint(resolve_endpoint, http_get)` `.read_util(id) -> UtilSnapshot|None` | 1 |
| `src/kinoforge/core/util_endpoints.py` (modify) | add `"modal"` to `_UTIL_SUPPORTED` | 2 |
| `src/kinoforge/_adapters.py` (modify) + `src/kinoforge/core/orchestrator.py` (modify) | `build_util_endpoint_for` modal branch + ledger-backed resolver threaded from the orchestrator call-site | 2 |
| `tests/live/test_modal_util_probe.py` (create) | RED live scaffold, committed pre-spend | 3 |
| `PROGRESS.md` + `reference_modal_provider_gotchas` memory (modify) | record the live proof (NO `successful-generations.md` entry — infra, not a gen axis) | 4 |

Ordering: **0 → 1 → 2 → 3 → 4 (live)**. Tasks 0 and 1 touch disjoint files (server vs provider) and may run in parallel; 2 needs 1; 3 needs 0+1+2; 4 needs all.

---

### Task 0: Server `/util` endpoint + `GpuStatsReader` seam

**Goal:** A self-contained `read_gpu_stats()` (pynvml primary, nvidia-smi fallback, psutil for cpu/mem/disk, uptime) that never raises, exposed via a sync `/util` route on `wan_t2v_server`.

**Files:**
- Create: `src/kinoforge/engines/diffusers/servers/_util_stats.py`
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (add the route near `def health()` ~line 1365)
- Test: `tests/engines/diffusers/servers/test_util_stats.py`

**Acceptance Criteria:**
- [ ] `read_gpu_stats()` returns a dict with keys `gpu_util_percent, cpu_percent, memory_percent, disk_percent, uptime_seconds` (values float/int or None).
- [ ] pynvml path used when it imports+inits; on pynvml ImportError/exception → nvidia-smi subprocess fallback; on BOTH failing → `gpu_util_percent=None` (no raise).
- [ ] cpu/mem/disk from psutil when present, else None. `uptime_seconds` from a module-level start time.
- [ ] The reader NEVER raises — any internal failure degrades a field to None.
- [ ] `wan_t2v_server` exposes `GET /util` (sync `def`, matching `def health()`) returning the dict.
- [ ] `_util_stats.py` imports NOTHING from `kinoforge.core` (server runs in a slim pod env without it).

**Verify:** `pixi run pytest tests/engines/diffusers/servers/test_util_stats.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing test** `tests/engines/diffusers/servers/test_util_stats.py`:

```python
"""Offline: the server-side GPU/CPU/mem stats reader (Modal util probe).

Bugs caught: a reader that raises when pynvml/nvidia-smi/psutil are absent
would crash the /util route (and, if that route were async, stall /health).
"""

from __future__ import annotations

import kinoforge.engines.diffusers.servers._util_stats as us


def test_read_gpu_stats_has_all_five_keys() -> None:
    d = us.read_gpu_stats()
    assert set(d) == {
        "gpu_util_percent",
        "cpu_percent",
        "memory_percent",
        "disk_percent",
        "uptime_seconds",
    }


def test_reader_never_raises_when_everything_missing(monkeypatch) -> None:
    # Force every source to fail; the reader must degrade to None, not raise.
    monkeypatch.setattr(us, "_read_gpu_via_pynvml", lambda: None)
    monkeypatch.setattr(us, "_read_gpu_via_smi", lambda: None)
    monkeypatch.setattr(us, "_read_host_via_psutil", lambda: (None, None, None))
    d = us.read_gpu_stats()
    assert d["gpu_util_percent"] is None
    assert d["cpu_percent"] is None
    assert d["memory_percent"] is None
    assert d["disk_percent"] is None
    assert isinstance(d["uptime_seconds"], int)  # uptime always computable


def test_pynvml_preferred_over_smi(monkeypatch) -> None:
    monkeypatch.setattr(us, "_read_gpu_via_pynvml", lambda: (42.0, 55.0))
    monkeypatch.setattr(
        us, "_read_gpu_via_smi", lambda: (99.0, 99.0)
    )  # must be ignored
    d = us.read_gpu_stats()
    assert d["gpu_util_percent"] == 42.0


def test_smi_fallback_when_pynvml_none(monkeypatch) -> None:
    monkeypatch.setattr(us, "_read_gpu_via_pynvml", lambda: None)
    monkeypatch.setattr(us, "_read_gpu_via_smi", lambda: (17.0, 33.0))
    d = us.read_gpu_stats()
    assert d["gpu_util_percent"] == 17.0
```

- [ ] **Step 2: Run — confirm it fails** (module does not exist).

Run: `pixi run pytest tests/engines/diffusers/servers/test_util_stats.py -v` → FAIL (ModuleNotFoundError).

- [ ] **Step 3: Write** `src/kinoforge/engines/diffusers/servers/_util_stats.py`:

```python
"""Self-contained GPU/CPU/mem stats reader for the diffusers server /util route.

Runs INSIDE the pod/container, which has NO ``kinoforge.core`` — so this module
imports only stdlib + optional pynvml/psutil (both guarded). Returns a plain
dict; the controller-side ModalUtilEndpoint maps it to core's UtilSnapshot.

Source order for GPU util: pynvml (typed NVML) → nvidia-smi subprocess → None.
Every read is defensive: any failure degrades a field to None, never raises.
"""

from __future__ import annotations

import subprocess
import time

_START = time.monotonic()


def _read_gpu_via_pynvml() -> tuple[float, float] | None:
    """Return (gpu_util_percent, gpu_mem_percent) via NVML, or None on any failure."""
    try:
        import pynvml  # nvidia-ml-py

        pynvml.nvmlInit()
        try:
            n = pynvml.nvmlDeviceGetCount()
            best_util = 0.0
            best_mem = 0.0
            for i in range(n):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                rates = pynvml.nvmlDeviceGetUtilizationRates(h)
                best_util = max(best_util, float(rates.gpu))
                best_mem = max(best_mem, float(rates.memory))
            return (best_util, best_mem)
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None


def _read_gpu_via_smi() -> tuple[float, float] | None:
    """Return (gpu_util_percent, gpu_mem_util_percent) via nvidia-smi, or None."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,utilization.memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        # One line per GPU: "util, mem" — take MAX util across devices.
        best_util = 0.0
        best_mem = 0.0
        for line in out.splitlines():
            u, m = (p.strip() for p in line.split(","))
            best_util = max(best_util, float(u))
            best_mem = max(best_mem, float(m))
        return (best_util, best_mem)
    except Exception:
        return None


def _read_host_via_psutil() -> tuple[float | None, float | None, float | None]:
    """Return (cpu_percent, memory_percent, disk_percent) via psutil, or Nones."""
    try:
        import psutil

        cpu = float(psutil.cpu_percent(interval=None))
        mem = float(psutil.virtual_memory().percent)
        disk = float(psutil.disk_usage("/").percent)
        return (cpu, mem, disk)
    except Exception:
        return (None, None, None)


def read_gpu_stats() -> dict:
    """Return the five UtilSnapshot fields as a plain dict; never raises."""
    gpu = _read_gpu_via_pynvml() or _read_gpu_via_smi()
    gpu_util = gpu[0] if gpu is not None else None
    cpu, mem, disk = _read_host_via_psutil()
    return {
        "gpu_util_percent": gpu_util,
        "cpu_percent": cpu,
        "memory_percent": mem,
        "disk_percent": disk,
        "uptime_seconds": int(time.monotonic() - _START),
    }
```

- [ ] **Step 4: Add the route** to `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — next to `def health()` (~line 1365). Use a **sync `def`** (matches `def health()`; FastAPI threadpools sync handlers so the blocking reads never stall the event loop / 502 `/health`):

```python
@app.get("/util")
def util() -> dict[str, Any]:
    """Per-tick GPU/CPU/mem stats for the provider util probe (Modal).

    Sync def on purpose: FastAPI runs sync handlers in a threadpool, so the
    blocking pynvml/nvidia-smi/psutil reads cannot stall the event loop.
    """
    from kinoforge.engines.diffusers.servers._util_stats import read_gpu_stats

    return read_gpu_stats()
```

- [ ] **Step 5: Run — confirm PASS** + no server-module regression:

Run: `pixi run pytest tests/engines/diffusers/servers/test_util_stats.py -q` → PASS. Then `pixi run pytest tests/engines/diffusers/ -q` → PASS (no import/route regression).

- [ ] **Step 6: Commit:**

```bash
pixi run pre-commit run --files src/kinoforge/engines/diffusers/servers/_util_stats.py src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/diffusers/servers/test_util_stats.py
git add src/kinoforge/engines/diffusers/servers/_util_stats.py src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/diffusers/servers/test_util_stats.py
git commit -m "feat(server): /util endpoint + GpuStatsReader seam (pynvml->nvidia-smi->psutil)"
```

---

### Task 1: `ModalUtilEndpoint.read_util`

**Goal:** A controller-side `UtilSnapshotEndpoint` satisfier that resolves `instance_id → .modal.run` URL (injected resolver), GETs `/util`, and maps the JSON to a `UtilSnapshot` — honoring the contract invariants (None when unresolved/404; `TransportError` on non-2xx).

**Files:**
- Create: `src/kinoforge/providers/modal/util.py`
- Test: `tests/providers/modal/test_modal_util_endpoint.py`

**Acceptance Criteria:**
- [ ] `ModalUtilEndpoint(resolve_endpoint, http_get=None)` implements `read_util(instance_id) -> UtilSnapshot | None` and passes `isinstance(ep, UtilSnapshotEndpoint)` (runtime-checkable Protocol).
- [ ] Resolver returns None (instance gone / no endpoint) → `read_util` returns None (no HTTP call).
- [ ] HTTP 200 with the five-field body → a populated `UtilSnapshot`; missing/null fields → None on that field.
- [ ] HTTP 404 → None; HTTP 500 (or transport error) → raises `kinoforge.core.errors.TransportError`.
- [ ] `read_util` is side-effect-free (no ledger writes).

**Verify:** `pixi run pytest tests/providers/modal/test_modal_util_endpoint.py -v` → PASS

**Steps:**

- [ ] **Step 1: Confirm the core contract shape** (already exists — do not modify here): `sed -n '22,63p' src/kinoforge/core/util_endpoints.py` — `UtilSnapshot(gpu_util_percent, cpu_percent, memory_percent, disk_percent, uptime_seconds)` + `UtilSnapshotEndpoint.read_util`. Also confirm `TransportError` exists: `rg -n "class TransportError" src/kinoforge/core/errors.py`.

- [ ] **Step 2: Write the failing test** `tests/providers/modal/test_modal_util_endpoint.py`:

```python
"""Offline: ModalUtilEndpoint maps /util JSON to a UtilSnapshot.

Bugs caught: warm pod whose endpoint is unresolved must yield None (not crash);
a 500 must surface as TransportError so consumers can tolerate; a 404 (pod gone)
is None per the contract.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.core.util_endpoints import UtilSnapshot, UtilSnapshotEndpoint
from kinoforge.providers.modal.util import ModalUtilEndpoint


class _Resp:
    def __init__(self, status: int, body: dict | None) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body or {}


_FULL = {
    "gpu_util_percent": 73.0,
    "cpu_percent": 12.0,
    "memory_percent": 40.0,
    "disk_percent": 5.0,
    "uptime_seconds": 88,
}


def _ep(status: int = 200, body: dict | None = None, url: str | None = "https://x.modal.run"):
    return ModalUtilEndpoint(
        resolve_endpoint=lambda _id: url,
        http_get=lambda _u: _Resp(status, body if body is not None else _FULL),
    )


def test_satisfies_protocol() -> None:
    assert isinstance(_ep(), UtilSnapshotEndpoint)


def test_maps_full_body_to_snapshot() -> None:
    snap = _ep().read_util("run-x")
    assert isinstance(snap, UtilSnapshot)
    assert snap.gpu_util_percent == 73.0
    assert snap.cpu_percent == 12.0
    assert snap.memory_percent == 40.0
    assert snap.disk_percent == 5.0
    assert snap.uptime_seconds == 88


def test_unresolved_endpoint_is_none_no_http() -> None:
    called = {"n": 0}

    def _boom(_u):  # must not be called
        called["n"] += 1
        raise AssertionError("HTTP called despite unresolved endpoint")

    ep = ModalUtilEndpoint(resolve_endpoint=lambda _id: None, http_get=_boom)
    assert ep.read_util("gone") is None
    assert called["n"] == 0


def test_404_is_none() -> None:
    assert _ep(status=404, body={}).read_util("run-x") is None


def test_500_raises_transport_error() -> None:
    with pytest.raises(TransportError):
        _ep(status=500, body={}).read_util("run-x")


def test_missing_fields_become_none() -> None:
    snap = _ep(body={"gpu_util_percent": 50.0}).read_util("run-x")
    assert snap is not None
    assert snap.gpu_util_percent == 50.0
    assert snap.cpu_percent is None
    assert snap.uptime_seconds is None
```

- [ ] **Step 3: Run — confirm it fails** (module missing).

Run: `pixi run pytest tests/providers/modal/test_modal_util_endpoint.py -v` → FAIL.

- [ ] **Step 4: Write** `src/kinoforge/providers/modal/util.py`:

```python
"""Modal satisfier of the core UtilSnapshotEndpoint contract.

Resolves instance_id -> .modal.run URL (injected resolver, ledger-backed at
wire time), GETs the server's /util route, and maps the JSON to a UtilSnapshot.
Mirrors RunPodGraphQLUtilEndpoint's injected-http-seam shape.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kinoforge.core.errors import TransportError
from kinoforge.core.util_endpoints import UtilSnapshot


def _default_http_get(url: str) -> Any:
    """Default GET seam — urllib-based, returns an object with status_code + json()."""
    import json
    import urllib.request

    class _R:
        def __init__(self, status: int, body: dict) -> None:
            self.status_code = status
            self._body = body

        def json(self) -> dict:
            return self._body

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 — https only
            return _R(resp.status, json.loads(resp.read().decode()))
    except urllib.error.HTTPError as exc:  # 4xx/5xx
        return _R(exc.code, {})


class ModalUtilEndpoint:
    """Read GPU/CPU/mem util from a Modal pod's /util route."""

    def __init__(
        self,
        resolve_endpoint: Callable[[str], str | None],
        http_get: Callable[[str], Any] | None = None,
    ) -> None:
        """Wire the id->URL resolver + the HTTP GET seam (test-injectable)."""
        self._resolve = resolve_endpoint
        self._http_get = http_get if http_get is not None else _default_http_get

    def read_util(self, instance_id: str) -> UtilSnapshot | None:
        """Return a snapshot, or None (unresolved / 404); TransportError on 5xx."""
        base = self._resolve(instance_id)
        if not base:
            return None
        resp = self._http_get(base.rstrip("/") + "/util")
        status = getattr(resp, "status_code", 0)
        if status == 404:
            return None
        if status < 200 or status >= 300:
            raise TransportError(
                f"modal /util for {instance_id} returned HTTP {status}"
            )
        body = resp.json() or {}

        def _f(key: str) -> float | None:
            v = body.get(key)
            return float(v) if v is not None else None

        up = body.get("uptime_seconds")
        return UtilSnapshot(
            gpu_util_percent=_f("gpu_util_percent"),
            cpu_percent=_f("cpu_percent"),
            memory_percent=_f("memory_percent"),
            disk_percent=_f("disk_percent"),
            uptime_seconds=int(up) if up is not None else None,
        )
```

Note: if `sed` at Step 1 shows `TransportError` is NOT in `core/errors.py`, use the actual error type the RunPod endpoint raises on transport failure (grep `rg -n "raise .*Error" src/kinoforge/providers/runpod/util.py`) and update the import + test to match — keep the invariant (5xx → a tolerated transport-class error).

- [ ] **Step 5: Run — confirm PASS:** `pixi run pytest tests/providers/modal/test_modal_util_endpoint.py -q` → PASS.

- [ ] **Step 6: Commit:**

```bash
pixi run pre-commit run --files src/kinoforge/providers/modal/util.py tests/providers/modal/test_modal_util_endpoint.py
git add src/kinoforge/providers/modal/util.py tests/providers/modal/test_modal_util_endpoint.py
git commit -m "feat(modal): ModalUtilEndpoint.read_util (ledger-resolved /util -> UtilSnapshot)"
```

---

### Task 2: Register `modal` + wire the factory with a ledger-backed resolver

**Goal:** `provider_util_supported("modal")` true; `build_util_endpoint_for` builds a `ModalUtilEndpoint` for a reap-enabled Modal cfg, with the id→URL resolver threaded from the orchestrator's ledger.

**Files:**
- Modify: `src/kinoforge/core/util_endpoints.py` (`_UTIL_SUPPORTED`)
- Modify: `src/kinoforge/_adapters.py` (`build_util_endpoint_for` — modal branch + new `resolve_modal_endpoint` kwarg)
- Modify: `src/kinoforge/core/orchestrator.py` (~line 1172 — pass a ledger-backed resolver)
- Test: `tests/core/test_build_util_endpoint_modal.py`

**Acceptance Criteria:**
- [ ] `provider_util_supported("modal")` is `True`.
- [ ] `build_util_endpoint_for(cfg, creds, resolve_modal_endpoint=<fn>)` returns a `ModalUtilEndpoint` for a Modal cfg with a reap flag on; returns None when both reap flags off (unchanged gate) and when `resolve_modal_endpoint` is None (can't resolve → inert).
- [ ] The orchestrator passes a resolver that reads `ledger.read(id)["endpoints"]["8000"]`.
- [ ] The RunPod + local branches are byte-unchanged.

**Verify:** `pixi run pytest tests/core/test_build_util_endpoint_modal.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing test** `tests/core/test_build_util_endpoint_modal.py`:

```python
"""Offline: modal is util-supported and the factory builds a ModalUtilEndpoint."""

from __future__ import annotations

from types import SimpleNamespace

from kinoforge._adapters import build_util_endpoint_for
from kinoforge.core.util_endpoints import provider_util_supported
from kinoforge.providers.modal.util import ModalUtilEndpoint


def test_modal_is_util_supported() -> None:
    assert provider_util_supported("modal") is True


def _modal_cfg(*, stall: bool = True):
    lifecycle = SimpleNamespace(
        stall_reap_enabled=stall, restart_loop_reap_enabled=False
    )
    compute = SimpleNamespace(provider="modal", lifecycle=lifecycle)
    return SimpleNamespace(compute=compute)


def test_factory_builds_modal_endpoint_with_resolver() -> None:
    creds = SimpleNamespace(get=lambda _k: None)
    ep = build_util_endpoint_for(
        _modal_cfg(), creds, resolve_modal_endpoint=lambda _id: "https://x.modal.run"
    )
    assert isinstance(ep, ModalUtilEndpoint)


def test_factory_none_when_reap_disabled() -> None:
    creds = SimpleNamespace(get=lambda _k: None)
    ep = build_util_endpoint_for(
        _modal_cfg(stall=False), creds, resolve_modal_endpoint=lambda _id: "u"
    )
    assert ep is None


def test_factory_none_when_no_resolver() -> None:
    creds = SimpleNamespace(get=lambda _k: None)
    ep = build_util_endpoint_for(_modal_cfg(), creds)  # no resolver
    assert ep is None
```

- [ ] **Step 2: Run — confirm it fails** (`provider_util_supported("modal")` False; factory has no modal branch/kwarg).

Run: `pixi run pytest tests/core/test_build_util_endpoint_modal.py -v` → FAIL.

- [ ] **Step 3: Add `"modal"`** to `_UTIL_SUPPORTED` in `src/kinoforge/core/util_endpoints.py`:

```python
_UTIL_SUPPORTED: frozenset[str] = frozenset({"local", "modal", "runpod"})
```

- [ ] **Step 4: Extend the factory** `build_util_endpoint_for` in `src/kinoforge/_adapters.py`. Add the kwarg to the signature and a modal branch after the `local` branch (keep runpod/local branches byte-identical):

```python
def build_util_endpoint_for(
    cfg: "Config",
    creds: "CredentialProvider",
    *,
    resolve_modal_endpoint: "Callable[[str], str | None] | None" = None,
) -> "UtilSnapshotEndpoint | None":
```

Then, inside, after the existing `if provider == "local":` block:

```python
    if provider == "modal":
        if resolve_modal_endpoint is None:
            return None  # no ledger resolver wired -> cannot reach /util
        from kinoforge.providers.modal.util import ModalUtilEndpoint

        return ModalUtilEndpoint(resolve_endpoint=resolve_modal_endpoint)
    return None
```

Add the `Callable` import to the TYPE_CHECKING block if not already imported (`from collections.abc import Callable`).

- [ ] **Step 5: Thread the resolver from the orchestrator.** In `src/kinoforge/core/orchestrator.py` at the `build_util_endpoint_for(cfg, creds)` call (~line 1172), build a ledger-backed resolver and pass it. The orchestrator has the ledger in scope as `self._ledger` (confirm the exact attribute: `rg -n "self\._ledger|ledger =|\.ledger\(\)" src/kinoforge/core/orchestrator.py | head`). Replace the call with:

```python
        from kinoforge._adapters import build_util_endpoint_for

        def _resolve_modal_endpoint(iid: str) -> str | None:
            entry = self._ledger.read(iid)
            if not entry:
                return None
            return (entry.get("endpoints") or {}).get("8000")

        _util_endpoint = (
            build_util_endpoint_for(
                cfg, creds, resolve_modal_endpoint=_resolve_modal_endpoint
            )
            if creds is not None
            else None
        )
```

If the ledger attribute is named differently (Step 5 grep), use the real name. If the orchestrator holds a `store`/`ctx` instead, build a `Ledger` from it the same way the rest of the method does.

- [ ] **Step 6: Run — confirm PASS + no regression:** `pixi run pytest tests/core/test_build_util_endpoint_modal.py tests/core/ -q -k "util or adapter or endpoint"` → PASS, then `pixi run test 2>&1 | tail -3` → `0 failed`.

- [ ] **Step 7: Commit:**

```bash
pixi run pre-commit run --files src/kinoforge/core/util_endpoints.py src/kinoforge/_adapters.py src/kinoforge/core/orchestrator.py tests/core/test_build_util_endpoint_modal.py
git add src/kinoforge/core/util_endpoints.py src/kinoforge/_adapters.py src/kinoforge/core/orchestrator.py tests/core/test_build_util_endpoint_modal.py
git commit -m "feat(modal): register modal util support + factory wiring with ledger-backed resolver"
```

---

### Task 3: RED live scaffold (committed before any spend)

**Goal:** A `pytest.mark.live` scaffold documenting the util-probe live proof, committed BEFORE the live spend (durability rule).

**Files:**
- Create: `tests/live/test_modal_util_probe.py`

**Acceptance Criteria:**
- [ ] Marked `pytest.mark.live` so `pixi run test` (`-m 'not live'`) DESELECTS it (offline suite green).
- [ ] Documents the proof runbook (start a warm Modal gen → resolve id from ledger → poll `read_util` mid-generation, assert `gpu_util_percent > 0`) as constants + an `xfail` contract. Mirrors the M4/M5 live scaffolds.

**Verify:** `pixi run pytest tests/live/test_modal_util_probe.py -q` → `1 xfailed` (or deselected under `-m 'not live'`); must NOT run live.

**Steps:**

- [ ] **Step 1: Read the M5 scaffold** for house style: `sed -n '1,40p' tests/live/test_modal_warm_reuse_hf_cache.py` (`pytestmark = pytest.mark.live`, string constants, `@pytest.mark.xfail` contract raising `AssertionError`).

- [ ] **Step 2: Write** `tests/live/test_modal_util_probe.py`:

```python
"""LIVE: Modal util probe returns real GPU% mid-generation (Wan 2.1 1.3B / A10).

Driven manually via the controller; this file records the contract. Marked
`live` so the default suite (`-m 'not live'`) skips it. Mirrors the M5 scaffold.

Runbook:
  1. Start a warm gen (default reuse): pod stays up, ledger carries endpoints.
     pixi run -e live-modal kinoforge generate \
       --config examples/configs/modal-wan-t2v-1_3b.yaml --mode t2v \
       --prompt "$(cat examples/configs/prompts/field-realistic.txt)"
  2. Resolve the instance id from `pixi run kinoforge list`, then poll read_util
     via ModalUtilEndpoint (ledger-resolved) DURING a second gen; assert
     gpu_util_percent > 0 under load, ~0 when idle.
  3. Teardown: kinoforge destroy + verify `kinoforge list` and `modal app list`.
"""

import pytest

pytestmark = pytest.mark.live


@pytest.mark.xfail(reason="live proof driven via controller; see PROGRESS")
def test_modal_util_probe_reports_gpu_load():
    raise AssertionError(
        "run a warm Modal gen; poll ModalUtilEndpoint.read_util(id) mid-gen; "
        "assert gpu_util_percent > 0 under load and ~0 idle; then teardown"
    )
```

- [ ] **Step 3: Confirm offline suite skips it:** `pixi run test 2>&1 | tail -3` → `0 failed`, file deselected; `pixi run pytest tests/live/test_modal_util_probe.py -q 2>&1 | tail -3` → `1 xfailed`.

- [ ] **Step 4: Commit BEFORE any spend:**

```bash
pixi run pre-commit run --files tests/live/test_modal_util_probe.py
git add tests/live/test_modal_util_probe.py
git commit -m "test(live): RED scaffold for Modal util probe (pre-spend)"
```

---

### Task 4: Live proof — util>0 mid-generation + PROGRESS/memory

**Goal:** Live-prove the Modal util probe returns real non-zero GPU% during a generation and ≈0 when idle; verify teardown; record in `PROGRESS.md` + the gotchas memory (NO `successful-generations.md` entry — infra, not a gen axis).

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation (Modal util probe). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `PROGRESS.md`, `reference_modal_provider_gotchas` memory
- (Possibly) Modify: `examples/configs/modal-wan-t2v-1_3b.yaml` — add `psutil` + `nvidia-ml-py` to `pip:` so cpu/mem + pynvml populate (nvidia-smi fallback already covers GPU with zero deps; commit any cfg change before re-spend)

**Acceptance Criteria:**
- [ ] `pixi run preflight` → PASS before spend.
- [ ] A live Modal pod is up (warm gen). Polling `ModalUtilEndpoint.read_util(<instance_id>)` (ledger-resolved) **during** a generation returns `gpu_util_percent > 0` (captured value); a poll while idle returns `gpu_util_percent` at/near 0 (or a clear drop) — the two states are both observed and recorded.
- [ ] `read_util` also returns non-None `cpu_percent` + `uptime_seconds` (proves the full body round-trips, not just GPU).
- [ ] After orchestrator exit: `pixi run kinoforge list` → no instances AND `modal app list` → no running kinoforge app.
- [ ] `PROGRESS.md` + memory updated; NO new `successful-generations.md` section.

**Verify:** `pixi run kinoforge list` → No running instances + empty ledger; the captured `read_util` output showing gpu_util_percent>0 under load recorded in PROGRESS.

**Steps:**

- [ ] **Step 1: Preflight** — `pixi run preflight` → PASS. (`kinoforge` self-loads `.env`; for the poll snippet + `modal` CLI, `set -a; . /workspace/.env; set +a` first.)

- [ ] **Step 2 (optional cfg): add probe deps** to `examples/configs/modal-wan-t2v-1_3b.yaml` `pip:` (`psutil`, `nvidia-ml-py`) so cpu/mem + pynvml populate. If added, commit before spend: `git commit -m "chore(config): add psutil+nvidia-ml-py to Modal 1.3B cfg for util probe"`. (GPU-util works via nvidia-smi with zero deps, so this is enhancement, not required for the gpu_util>0 assertion.)

- [ ] **Step 3: Start a warm gen** (default reuse — pod survives), background it, and capture the instance id:

```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-wan-t2v-1_3b.yaml --mode t2v \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" &
# once it prints `generated:`, the pod is warm; get the id:
pixi run kinoforge list   # -> instance id, e.g. run-YYYYMMDD-HHMMSS
```

- [ ] **Step 4: Poll `read_util` under load + idle.** Fire a SECOND gen (background) and, while it runs, poll the probe; then poll again once idle. Copy-paste snippet (loads `.env`, ledger-resolves the id):

```python
from kinoforge.core.dotenv_loader import load_env_file; load_env_file()
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.local import LocalArtifactStore
from kinoforge.providers.modal.util import ModalUtilEndpoint
# NOTE: construct Ledger/store exactly as the CLI does — confirm the run_id/store
# wiring via `rg -n "Ledger(" src/kinoforge/cli/context.py` and mirror it.
IID = "<instance-id-from-step-3>"
led = Ledger(...)  # per the CLI wiring
ep = ModalUtilEndpoint(
    resolve_endpoint=lambda i: (led.read(i) or {}).get("endpoints", {}).get("8000")
)
print(ep.read_util(IID))  # under load -> UtilSnapshot(gpu_util_percent>0, ...)
```

Capture one snapshot with `gpu_util_percent > 0` (during inference) and one at/near 0 (idle). If the ledger construction is fiddly, resolve the `.modal.run` URL directly from `kinoforge list`/the ledger file and pass a constant-returning resolver — the endpoint contract is the same.

- [ ] **Step 5: Teardown** — destroy the pod (under `-e live-modal` — the default env lacks the `modal` binary) + verify:

```bash
pixi run -e live-modal kinoforge destroy --id <instance-id>
pixi run kinoforge list
set -a; . /workspace/.env; set +a
pixi run -e live-modal modal app list | grep -i kinoforge | grep -iv stopped || echo "no running kinoforge apps"
```

Expected: `No running instances.` + `No instances recorded in ledger.` AND no running Modal app.

- [ ] **Step 6: Record + commit** (NO `successful-generations.md` entry — infra). Add a PROGRESS note (new milestone line + SINGLE NEXT ACTION update) with the captured under-load vs idle `read_util` output, and update the `reference_modal_provider_gotchas` memory (Modal now has a util probe; gotcha #6/#9 neighbourhood — the monitoring-blindness caveat is now resolved via the in-container `/util` endpoint):

```bash
git add PROGRESS.md
git commit -m "docs(progress): Modal util probe live-green (gpu_util>0 mid-gen); parity monitoring gap closed"
```

---

## Self-Review

**Spec coverage:** Unit 1 server `/util` + `GpuStatsReader` seam (spec §"Unit 1") → Task 0. Unit 2 `ModalUtilEndpoint` (spec §"Unit 2") → Task 1. Unit 3 registration + factory wiring (spec §"Unit 3") → Task 2. Testing "RED live scaffold" (spec §Testing) → Task 3. Testing "Live proof" + logging "no §entry" (spec §Testing + §Logging) → Task 4. Ledger-resolution decision → Task 1 (`resolve_endpoint`) + Task 2 Step 5 (orchestrator resolver). Sync-`/util` risk (spec §Known-risks #2) → Task 0 Step 4. pynvml-fallback risk (#1) → Task 0 reader chain. All spec sections covered.

**Placeholder scan:** No TBD/TODO. Every code step has complete code. Task 2 Step 5 + Task 4 Step 4 contain explicit "confirm the exact attribute/wiring via `rg`" instructions (real reconcile steps against live code, not placeholders) because the orchestrator ledger attribute + the CLI Ledger construction are the source of truth. `<instance-id>` is a runtime value, not a placeholder.

**Type consistency:** `read_gpu_stats() -> dict` (Task 0) ↔ `ModalUtilEndpoint` maps that dict's five keys → `UtilSnapshot` (Task 1) ↔ `build_util_endpoint_for(..., resolve_modal_endpoint=)` returns a `ModalUtilEndpoint` (Task 2). The five field names (`gpu_util_percent, cpu_percent, memory_percent, disk_percent, uptime_seconds`) are identical across the reader, the endpoint, and the core `UtilSnapshot`. `resolve_endpoint`/`resolve_modal_endpoint` callable shape `(str) -> str | None` is consistent Task 1↔2.
