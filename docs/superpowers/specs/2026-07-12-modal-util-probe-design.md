# Modal util probe — GPU/CPU/mem monitoring for Modal

**Status:** validated 2026-07-12 (brainstorm).
**Depends on:** the existing `core/util_endpoints.py` contract (`UtilSnapshot`, `UtilSnapshotEndpoint`,
`provider_util_supported`), the `_adapters.build_util_endpoint_for` factory, the diffusers
`wan_t2v_server`, and the §26 ledger-endpoints fix (`1cb4299`) that persists a Modal instance's
`.modal.run` URL for cross-process resolution.

## Goal

Close the one hard Modal↔RunPod provider-parity gap: make `provider_util_supported("modal")` true by
implementing the **existing** `UtilSnapshotEndpoint` contract for Modal, so `HeartbeatLoop`'s per-tick
util read and the live-smoke "poll GPU%, 0% for ≥3 probes = dead pod" rule work on Modal instead of the
current app-state-and-log-only blindness.

## Background — the shared contract already exists (do not rebuild it)

`src/kinoforge/core/util_endpoints.py` (shipped for RunPod under "C26") already defines:

- `UtilSnapshot` (frozen dataclass): `gpu_util_percent`, `cpu_percent`, `memory_percent`,
  `disk_percent`, `uptime_seconds` — all `float|int|None` (None = "unavailable", excluded from the
  STALL AND-clause).
- `UtilSnapshotEndpoint` Protocol: `read_util(instance_id) -> UtilSnapshot | None`. Invariants: `None`
  when the instance is gone / slot never written / all fields unavailable; `TransportError` on HTTP
  non-2xx / transport failure; idempotent + side-effect-free.
- `provider_util_supported(kind)` gated on `_UTIL_SUPPORTED = frozenset({"local", "runpod"})`.

Consumers already wired to this contract: `HeartbeatLoop` (per-tick), `boot_liveness`, Layer-V classify,
and `_adapters.build_util_endpoint_for(cfg, creds) -> UtilSnapshotEndpoint | None` (the factory —
returns `None` when `compute is None`, both `stall_reap_enabled` + `restart_loop_reap_enabled` are off,
or the provider is unsupported). **This milestone implements the contract for Modal; it adds nothing to
the contract itself.**

Modal exposes no per-container GPU-util API, so the util must be sourced from **inside the container**
and served over the existing `.modal.run` transport — there is no Modal-SDK path.

## Approach — three small units + wiring

### Unit 1 — server-side `GpuStatsReader` seam + `/util` route

- New reader seam (new module, e.g. `src/kinoforge/engines/diffusers/servers/_util_stats.py`):
  `read_gpu_stats() -> dict` returning the five `UtilSnapshot` fields.
  - **GPU util + VRAM:** pynvml primary (`nvmlInit` + `nvmlDeviceGetUtilizationRates(h).gpu` /
    `.memory`), **nvidia-smi subprocess fallback**
    (`nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits`)
    when pynvml import/init fails. `gpu_util_percent` = MAX across devices (mirrors RunPod's semantics,
    `util_endpoints.py:31`).
  - **CPU / memory / disk %:** `psutil` (`cpu_percent`, `virtual_memory().percent`,
    `disk_usage("/").percent`).
  - **uptime_seconds:** process/container start delta.
  - Every field is read under its own `try/except` → `None` on any failure. The reader NEVER raises;
    the probe must not be able to crash the server.
- New route on `wan_t2v_server`: a **sync `def util()`** (matching the existing `def health()` /
  `def status()` — FastAPI runs sync handlers in a threadpool, so the blocking pynvml/subprocess/psutil
  reads do NOT need `asyncio.to_thread` and cannot stall the event loop / 502 `/health`; keeping `/util`
  sync is the load-bearing choice here, per [[feedback_wan_server_async_blocking]]). Returns the reader's
  dict as JSON.

### Unit 2 — `ModalUtilEndpoint` (the Modal satisfier)

- New `src/kinoforge/providers/modal/util.py::ModalUtilEndpoint`, implements
  `UtilSnapshotEndpoint.read_util`.
- Constructed with two injected seams (both fake-able in tests):
  - `resolve_endpoint: Callable[[str], str | None]` — maps `instance_id → .modal.run` base URL. Wired
    from the **ledger** (`Ledger.read(id)["endpoints"]["8000"]`, persisted by the §26 fix) so it works
    from any process, matching how the util-aware `HeartbeatLoop` resolves RunPod by id.
  - `http_get: Callable[[str], <resp>]` — HTTP GET seam (default: the project's http helper).
- `read_util(instance_id)`:
  - resolve URL → `None` if unresolved (instance gone / no endpoint recorded).
  - GET `<url>/util`; non-2xx → `TransportError` (per contract; consumers tolerate).
  - map JSON → `UtilSnapshot`; missing/`null` fields → `None`.

### Unit 3 — registration + factory wiring

- Add `"modal"` to `_UTIL_SUPPORTED` in `core/util_endpoints.py`.
- Add a `modal` branch to `_adapters.build_util_endpoint_for`: build a `ModalUtilEndpoint` with the
  ledger-backed `resolve_endpoint`. (The Modal branch needs the session ledger/store, not creds — thread
  it in; keep the RunPod branch untouched.)
- Result: `HeartbeatLoop` + the live-smoke monitor get real Modal util automatically once a reap flag is
  on; the live proof can also call `read_util` directly (factory gating bypassed).

## Testing

- **Offline unit:**
  - `GpuStatsReader` seam: fake pynvml → snapshot; pynvml-fails → nvidia-smi fallback → snapshot;
    both fail → all-`None` (never raises).
  - `/util` route returns the five-field shape with a mock reader.
  - `ModalUtilEndpoint.read_util`: fake resolver + fake HTTP body → `UtilSnapshot`; unresolved id →
    `None`; 404 → `None`; 500 → `TransportError`.
  - `provider_util_supported("modal") is True`; `build_util_endpoint_for` returns a `ModalUtilEndpoint`
    for a Modal cfg with a reap flag on, `None` when both reap flags off.
- **RED live scaffold** committed BEFORE spend (durability rule), `pytest.mark.live`, deselected under
  `-m 'not live'`.
- **Live proof (controller-driven):** a cheap Wan 2.1 1.3B Modal gen; poll `read_util(instance_id)`
  during generation and assert `gpu_util_percent > 0` under load, and ≈0 (or a clear drop) when idle —
  proving the probe sees real GPU work end-to-end over the `.modal.run` transport. Teardown verified
  (`kinoforge list` + `modal app list`).

## Acceptance criteria

- `provider_util_supported("modal")` is `True`; `build_util_endpoint_for` yields a `ModalUtilEndpoint`
  for a reap-enabled Modal cfg.
- `/util` on the Modal server returns `{gpu_util_percent, cpu_percent, memory_percent, disk_percent,
  uptime_seconds}` sourced pynvml-first, nvidia-smi-fallback; the reader never raises.
- `ModalUtilEndpoint.read_util(id)` returns a populated `UtilSnapshot` for a live Modal instance,
  resolving id→URL via the ledger; honors the None / `TransportError` invariants.
- Live: `gpu_util_percent > 0` observed mid-generation on a real Modal pod; teardown clean.
- Offline suite green; RunPod util path untouched.

## Known risks

1. **pynvml availability in the Modal image.** `nvidia-ml-py` must be baked into the server image; if
   absent/mismatched the reader falls back to nvidia-smi (present in CUDA images). Both failing →
   `gpu_util_percent=None` (degraded, not broken) — the fallback chain is the mitigation.
2. **`/util` must stay sync `def`.** An `async def` handler with blocking reads would stall the event
   loop and 502 `/health` (the [[feedback_wan_server_async_blocking]] failure). Spec mandates sync.
3. **id→URL resolution depends on the ledger carrying endpoints** (§26 fix). For an ephemeral/index-only
   instance the resolver reads the ephemeral-index endpoints instead; both are covered by the injected
   `resolve_endpoint` seam.
4. **No util during cold boot** (server not up yet) — `read_util` returns `None`/`TransportError` until
   `/util` binds; consumers already treat that as "data unavailable", matching RunPod's pre-ready window.

## Non-goals

- Boot-liveness / STALL-reap classifier for Modal (mirror of `RunPodBootLivenessProbe` →
  `BootVerdict` → `STALL_REAP`) — a clean follow-up milestone built ON this probe.
- Any Modal-SDK-native stats path (Modal exposes no per-container GPU util).
- Changes to the `core/util_endpoints.py` contract or the RunPod satisfier.

## Logging

A util probe is infrastructure, not a new generation axis (no new mode / provider / engine / model /
YAML shape), so it gets **no `successful-generations.md` entry** — record completion in `PROGRESS.md`
and the Modal-gotchas memory instead.

## User decisions (already made, 2026-07-12)

- **GPU-util source: reader seam** — pynvml primary + nvidia-smi fallback, both → `UtilSnapshot`.
- **Scope: snapshot-only**, implementing the existing shared `core` contract (the "hoist to core" is
  already done); boot-liveness/STALL-reap deferred.
- **id→URL resolution: ledger-resolution** (reuse the §26 persisted endpoints) via an injected resolver
  seam, so `read_util` works cross-process — not URL-passed-at-construction.
