# LoRA-flexible warm-reuse (v1, Diffusers) — design spec

**Date:** 2026-06-20
**Author:** brainstorm session (Dr. Twinklebrane + Claude)
**Status:** validated, awaiting user spec review before plan phase
**Issue:** none yet (recommend opening one before plan phase)

## 1. Motivation

Kinoforge's existing warm-reuse (PROGRESS B3 / B5) attaches a fresh
`kinoforge generate` invocation to an already-alive RunPod pod when the
new job's `CapabilityKey` byte-equals an existing pod's recorded key.
The match is all-or-nothing — same base model, same engine, same
precision, same ordered LoRA stack.

For Wan 2.2 T2V the cold-boot cost is dominated by the ~28 GB base-model
download (`hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers`); LoRAs are typically
50 MB to a few GB. Today, a generate that differs from the warm pod's
LoRA stack by even one ref forces a fresh cold-boot — re-downloading
the 28 GB base model when only the (much smaller) LoRA delta has
actually changed.

This spec adds the ability to warm-attach when the base + engine +
precision agree but the LoRA stack differs. The warm pod's resident
LoRAs get swapped in place: download what's new, evict (LRU) only what
must go to make room, hot-swap the pipeline's adapter set. The
expensive base-model bytes stay put across the swap.

## 2. Scope

### In scope

- **`CapabilityKey` factoring.** Split the existing key into
  `WarmAttachKey(base_model, engine, precision)` +
  `LoraStack(refs: tuple[str, ...])`, preserving the existing byte-equal
  hash for backward compatibility (full key serializes identically).
- **Pod-side declarative swap endpoint.** New
  `POST /lora/set_stack {target_refs, download_specs}` on the Diffusers
  Wan T2V server. Pod computes the diff against its current inventory,
  evicts (LRU) only what must go, downloads what's missing, hot-swaps
  the pipeline adapter set, returns the new inventory + free disk.
- **Pod-side inventory query.** New `GET /lora/inventory` returning the
  current inventory + free disk; used by matcher re-probe + operator UX
  surfaces.
- **Basic LoRA loading at cold-boot.** Diffusers engine today loads no
  LoRAs; cold-boot path extended to provision with N LoRAs at startup.
  Strict prerequisite for the swap path.
- **Matcher generalization.** New `core/warm_reuse/matcher.py` module
  factored out of `core/orchestrator.py`. Two-tier lookup:
  `WarmAttachKey` indexed lookup → per-candidate `LoraStack`
  delta-evaluation against pod inventory + free disk + LRU policy.
  Preserves the existing exact-byte fast path bit-for-bit.
- **Per-pod serialization.** New `PodLockRegistry` (in-process
  `threading.Lock` per `pod_id`). Held for the duration of
  (`/lora/set_stack` + `/generate` + result). v1 limitation: in-process
  only (same as today's warm-reuse).
- **Ledger schema additions.** New fields on `PodEntry`:
  `warm_attach_key`, `lora_inventory: list[LoraInventoryEntry]`,
  `loras_dir_free_bytes`, `loras_dir_free_bytes_observed_at_local`.
  Lazy-derived for pre-feature entries — no migration script.
- **Ephemeral integration.** Existing `ledger_record` gate covers the
  new fields for free — payload routes to `session.in_memory_ledger`
  under strict policy. New `_register_observed_lora_refs` helper
  registers pod-observed LoRA refs with `RedactionRegistry` so logs
  don't leak them. AST-scan invariant enforces the helper is called at
  every inventory read site.
- **Operator UX.** `kinoforge status --id` renders the inventory
  section; new `--dry-run-swap` flag on `generate`/`batch` previews
  matcher decisions without side-effects; new `kinoforge pod lora ls
  <pod_id>` direct inventory query.
- **Failure modes.** Five new error classes — download-failed,
  degraded-pod, unreachable, VRAM-OOM, disk-full — all routed via the
  existing reaper (degraded pods get `status="degraded"` ledger field,
  reaper recognizes alongside heartbeat-stale).

### Out of scope (this spec)

See §9 for the full deferral table with promotion triggers. High-level:

- ComfyUI engine LoRA-flexible warm-reuse (gated on existing C23
  follow-up).
- Cold-boot fallback on swap failure (Option B / §7); half-state
  recovery (Option C / §7) — both layered on top of v1's fail-loud
  baseline.
- Concurrent jobs on the same warm pod (lift the per-pod lock).
- Cross-process pod-lock coordination (folds into Layer H whenever
  that lands).
- Operator-controlled pinning (`pod lora pin/unpin/rm`).
- Smart eviction (size-weighted instead of pure LRU).
- Persistent matcher-decision log.
- Smart prefetch.
- Pod-side post-session LoRA scrub under `--ephemeral` (§5.4 documents
  the residual threat vector; mitigations partially in place).

### Public-by-design surfaces unchanged

- `prompt-field-realistic.txt` / `prompt-field-dreamlike.txt` and the
  in-`examples/configs/` LoRA refs (e.g. the Arcane Style Wan 2.2
  default pair at `civitai:2197303@2474081` +
  `civitai:2197303@2474073` documented in README) stay plaintext in
  the repo. Vault-supplied LoRAs continue to be redacted via the
  existing `lora:ref` token kind.

## 3. Threat model deltas

Existing ephemeral threat model (`2026-06-08-ephemeral-workspaces-design.md`
§3) holds. One new vector worth documenting:

| Adversary | New reach with this spec | Mitigation |
|---|---|---|
| Adversary with RunPod console access to a specific pod | Can read the pod's LoRA cache directory + observe inventory changes across sessions; can infer "ephemeral session N had LoRA X resident because X was swapped in then out" | Pod is inside the operator's trust boundary (same posture as today's hosted-provider internal logs). Pod naming under strict policy already drops the alias (`pod_name_includes_alias=False`), weakening linkability. Future hardening candidate: opt-in pod-side post-session LoRA scrub under a separate flag (deferred to §9). |
| Adversary reading orchestrator-side logs across multiple sessions | Could correlate observed-LoRA-ref token IDs to specific pod IDs across sessions | Observed refs registered with `RedactionRegistry` for the lifetime of the session (`_register_observed_lora_refs`); cross-session correlation requires the adversary to have run-time access during the sessions in question, which is already excluded by the existing threat model. |

## 4. Decisions locked during brainstorm

| # | Decision | Value | Why |
|---|---|---|---|
| D1 | Engine scope for v1 | **Diffusers only.** ComfyUI deferred to follow-up after C23 lands. | Diffusers exposes `pipe.unload_lora_weights()` + `pipe.load_lora_weights(adapter_name=...)` as first-class API; ComfyUI's LoRA stack lives in the graph JSON + has no hot-swap API. Wan 2.2 14B T2V via Diffusers is the only working path today (Kijai-ComfyUI marked DEAD per PROGRESS:3584); the Wan 2.2 T2V Diffusers green-smoke (2026-06-20, commit `248b39c`) is the foundation we extend. |
| D2 | Eviction policy | **Conservative LRU.** Keep everything that fits; evict only as needed; pick targets by `last_used_at` ascending. | Best fit for iterative workflows (operator cycles through a handful of LoRAs across many runs). Trades a single ledger timestamp field for re-download cost avoidance. Smart-eviction (size-weighted) deferred. |
| D3 | Failure-mode for swap errors | **Fail loud + existing reaper handles.** Orchestrator surfaces the failure to the operator; degraded pods marked `status="degraded"` in the ledger; existing reaper destroys them at the next sweeper pass. | Simplest. Reuses existing reaper. Foundation for future opt-in cold-boot-fallback (v2 candidate). Disk pressure is rare in the design's primary scenario (Wan 2.2 + 200 GB pod + 50 MB-to-GB LoRAs); rare failures are acceptable when surfaced cleanly. |
| D4 | Concurrency on a single pod | **Serialize via per-pod lock.** `PodLockRegistry` holds an in-process `threading.Lock` per `pod_id` for the duration of (swap + generate). | Avoids LoRA refcount machinery; existing serial behavior is well-tested. Future v2 can lift the lock if measured throughput justifies it. Cross-process gap is the same as existing warm-reuse — folds into Layer H. |
| D5 | Swap contract shape | **Pod-side declarative.** Orchestrator POSTs `/lora/set_stack {target_refs, download_specs}`; pod computes the diff + executes. | Single round-trip; pod authoritative for its own disk/memory state; recovers gracefully from out-of-band changes (e.g., operator SSH'd into the pod). Orchestrator can still log a predicted swap plan locally for diagnostics. |
| D6 | Identity model | **Approach 1: split `CapabilityKey` into `WarmAttachKey` + `LoraStack`.** Composite serializes to the same byte-equal hash as today. | Clean architectural seam; matches existing `compute_profile_alias` factoring of `(base, loras, engine, precision)`; ledger migration avoidable via lazy derivation; existing byte-stability tests stay green. |
| D7 | Ephemeral policy field | **No new field on `EphemeralPolicy`.** The new persistent-write fields ride on the existing `PodEntry` shape, which the `ledger_record` gate already protects. | Zero new mechanism. Pattern already proven by every other persistent-write site (lifecycle.py:502, batch.py:904, profiles.py:246). |
| D8 | Pod-side disk scrub at ephemeral exit | **Do NOT scrub.** Pod's LoRA cache directory survives `EphemeralSession.__exit__`. Disk goes away when the pod itself is destroyed. | Scrubbing would force every `--ephemeral` run to cold-boot LoRAs, defeating the feature for the most privacy-conscious user. Pod is inside the trust boundary. Residual threat documented in §3 + §5.4. |
| D9 | Observed-ref redaction | **Auto-register every observed LoRA ref with `RedactionRegistry`.** New helper `_register_observed_lora_refs` called after every `/lora/inventory` + `/lora/set_stack` response. AST-scan invariant enforces the call. | Prevents pod-observed refs (refs the current session didn't supply via vault) from bleeding into logs. Idempotent — safe to call repeatedly. |

## 5. Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR (local process)                               │
│                                                             │
│  ┌────────────────────────────────────────────────────┐    │
│  │ Matcher  (core/warm_reuse/matcher.py — new)        │    │
│  │  1. derive WarmAttachKey from cfg                  │    │
│  │  2. ledger.find_pods_by_warm_attach_key(k)         │    │
│  │  3. for each candidate: evaluate LoraStack delta   │    │
│  │     against pod inventory + free disk (LRU)        │    │
│  │  4. pick cheapest or fall through to cold-boot     │    │
│  └────────────────────────────────────────────────────┘    │
│           │ holds per-pod lock for swap+generate            │
│           ▼                                                 │
│  ┌────────────────────────────────────────────────────┐    │
│  │ DiffusersEngine                                    │    │
│  │  1. POST /lora/set_stack → declarative target      │    │
│  │  2. POST /generate (existing)                      │    │
│  └────────────────────────────────────────────────────┘    │
│                                                             │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTP over RunPod proxy
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  POD (wan_t2v_server.py — extended)                         │
│                                                             │
│  POST /lora/set_stack {refs, download_urls}                 │
│    → diff current vs target                                 │
│    → evict LRU losers if disk-tight                         │
│    → download missing                                       │
│    → pipe.unload_lora_weights() + load_lora_weights(...)    │
│    → return new inventory + sizes + last_used_at            │
│                                                             │
│  GET /lora/inventory  (for re-probe + dry-run)              │
└─────────────────────────────────────────────────────────────┘
```

### 5.1 New modules

```
src/kinoforge/core/warm_reuse/
├── __init__.py
├── matcher.py            # find_warm_attach_candidate + SwapPlan + SwapEvaluation
├── pod_lock.py           # PodLockRegistry
└── redaction.py          # _register_observed_lora_refs helper
```

### 5.2 Edited modules

```
src/kinoforge/core/profiles.py                          # CapabilityKey split → WarmAttachKey + LoraStack
src/kinoforge/core/orchestrator.py                      # extract matcher call sites; route to warm_reuse.matcher
src/kinoforge/core/lifecycle.py                         # PodEntry gains warm_attach_key + lora_inventory + loras_dir_free_bytes; Ledger.find_pods_by_warm_attach_key
src/kinoforge/core/errors.py                            # LoraSwapError base + 5 subclasses
src/kinoforge/engines/diffusers/__init__.py             # set_lora_stack(target_refs, download_specs) wraps /lora/set_stack POST
src/kinoforge/engines/diffusers/servers/wan_t2v_server.py # /lora/set_stack + /lora/inventory + initial_lora_stack startup arg
src/kinoforge/cli/_main.py                              # --dry-run-swap flag; new `pod lora ls` subcommand; status renderer extension
```

### 5.3 New tests

```
tests/core/test_capability_key_split.py
tests/core/test_warm_reuse_matcher_exact.py
tests/core/test_warm_reuse_matcher_delta_no_evict.py
tests/core/test_warm_reuse_matcher_lru.py
tests/core/test_warm_reuse_matcher_reprobe.py
tests/core/test_warm_reuse_matcher_lock.py
tests/core/test_pod_lock_registry.py
tests/core/test_ledger_lora_inventory.py
tests/core/test_ledger_lora_inventory_ephemeral.py
tests/core/test_warm_reuse_redaction.py
tests/core/test_lora_swap_errors.py
tests/core/test_reaper_degraded_pods.py
tests/engines/test_wan_t2v_server_set_stack.py
tests/engines/test_wan_t2v_server_set_stack_failures.py
tests/engines/test_wan_t2v_server_inventory.py
tests/integration/test_warm_reuse_lora_first_attach.py
tests/integration/test_warm_reuse_lora_overlap.py
tests/integration/test_warm_reuse_lora_lru_eviction.py
tests/integration/test_warm_reuse_lora_cold_boot_fallthrough.py
tests/integration/test_warm_reuse_lora_ephemeral.py
tests/live/test_wan22_lora_warm_reuse.py                # the green gate
```

### 5.4 AST-scan invariants (added to existing `tests/test_no_unredacted_writes.py`)

1. **`InventorySnapshot.inventory` reader → mandatory
   `_register_observed_lora_refs` call.** Any function body that reads
   `InventorySnapshot.inventory` (or iterates `LoraInventoryEntry`
   accessing `.ref`) must contain a call to
   `_register_observed_lora_refs(...)` or be annotated
   `# noqa: KF-LORA-REDACT-EXEMPT`. The exemption tag must be
   grep-able as a single canonical list.
2. **`PodEntry.lora_inventory` writes route through `Ledger.touch`.**
   No call site outside `core/lifecycle.py` may construct a
   `PodEntry` with non-default `lora_inventory` and persist via
   `store.put_json` directly. Enforces ephemeral-gate routing.

## 6. Identity model

### 6.1 New types

```python
# src/kinoforge/core/profiles.py — alongside existing CapabilityKey

@dataclass(frozen=True)
class WarmAttachKey:
    """The slow-to-rebuild part of a pod's identity."""
    base_model: str   # e.g. "hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers"
    engine: str       # e.g. "diffusers"
    precision: str    # e.g. "fp16"

    def derive(self) -> str:
        """sha256 hex digest of canonical-JSON of (base_model, engine, precision)."""

    @classmethod
    def from_cfg(cls, cfg: Config) -> WarmAttachKey: ...


@dataclass(frozen=True)
class LoraStack:
    """The cheap-to-swap part of a pod's identity. Ordered."""
    refs: tuple[str, ...]

    @classmethod
    def from_cfg(cls, cfg: Config) -> LoraStack: ...
```

### 6.2 `CapabilityKey` refactor

`CapabilityKey` becomes a composite over the two factors:

```python
@dataclass(frozen=True)
class CapabilityKey:
    warm_attach_key: WarmAttachKey
    lora_stack: LoraStack

    def derive(self) -> str:
        """Returns the SAME byte-equal hash as today's CapabilityKey.derive()
        for the same (base, engine, precision, loras) tuple. Implementation
        canonicalises the composite to the historical JSON shape."""

    @classmethod
    def from_cfg(cls, cfg: Config) -> CapabilityKey: ...
```

Backward-compat invariant: `CapabilityKey.derive()` produces the
**same hash bytes** as the pre-split implementation for the same
inputs. Pinned by `tests/core/test_capability_key_split.py` as a
regression test against a golden hash table.

### 6.3 Ledger migration strategy

Pre-feature pod entries lack `warm_attach_key`. Strategy:
self-healing lazy derivation. When the matcher reads an entry without
`warm_attach_key`, it derives one from the entry's existing
`capability_key` payload (which already carries the full
`(base_model, engine, precision, loras)` tuple in canonical-JSON form
per the existing `derive()`), then writes it back via `Ledger.touch`.
One self-healing pass per pre-feature pod. No migration script. No
data loss. Worst-case cost: one extra ledger write per pre-feature
pod on its first post-feature matcher call.

`lora_inventory` defaults to `[]` for pre-feature entries — matcher
treats the empty list as "unknown, re-probe via `GET /lora/inventory`
before deciding." Re-probe populates the field; future matcher calls
skip the round-trip.

## 7. Pod-side server changes

### 7.1 In-memory inventory

Module-level dict in `wan_t2v_server.py` guarded by `asyncio.Lock`:

```python
# {ref: {filename, size_bytes, loras_dir_path, downloaded_at_local,
#        last_used_at_local, adapter_name}}
_inventory: dict[str, dict[str, Any]] = {}
_swap_lock: asyncio.Lock = asyncio.Lock()
```

`last_used_at_local` updates on every `/generate` that uses the LoRA.
`size_bytes` is the **actual** downloaded byte count, not the
metadata-claimed size (guards against CivitAI/HF size mismatches).

### 7.2 `POST /lora/set_stack`

Request:

```python
class ArtifactDownloadSpec(BaseModel):
    url: str
    headers: dict[str, str]    # carries Authorization: Bearer ...
    filename: str
    size_hint: int | None      # for orchestrator-side disk arithmetic

class SetStackRequest(BaseModel):
    target_refs: list[str]
    download_specs: dict[str, ArtifactDownloadSpec]

class SetStackResponse(BaseModel):
    inventory: list[LoraInventoryEntry]
    free_bytes: int
    swap_rejected: SwapRejectedDetails | None = None   # for §7.4 VRAM-OOM rollback
```

Handler:

```python
async def set_stack(req: SetStackRequest) -> SetStackResponse:
    async with _swap_lock:
        target_set = set(req.target_refs)
        current_set = set(_inventory.keys())
        to_evict_candidates = current_set - target_set
        to_download = target_set - current_set
        free_bytes = _disk_free_bytes(LORAS_DIR)
        target_dl_bytes = sum((req.download_specs[r].size_hint or 0)
                              for r in to_download)
        if target_dl_bytes <= free_bytes:
            evict_plan = []
        else:
            evict_plan = _pick_lru_evict(
                to_evict_candidates,
                need=target_dl_bytes - free_bytes,
            )
        for ref in evict_plan:
            await _evict_one(ref)            # unload adapter + rm file + drop from _inventory
        for ref in to_download:
            await _download_one(ref, req.download_specs[ref])   # failure → fail loud
        await _reload_pipeline_loras(req.target_refs)
        return SetStackResponse(
            inventory=_inventory_snapshot(),
            free_bytes=_disk_free_bytes(LORAS_DIR),
        )
```

### 7.3 `GET /lora/inventory`

Read-only snapshot of `_inventory` + `_disk_free_bytes(LORAS_DIR)`.
Acquires `_swap_lock` in shared mode (FastAPI doesn't have shared
locks natively; v1 just takes the exclusive lock — reads are rare
relative to generates).

### 7.4 Pipeline LoRA mechanics

```python
async def _reload_pipeline_loras(refs: list[str]) -> None:
    pipe.unload_lora_weights()
    for i, ref in enumerate(refs):
        pipe.load_lora_weights(
            _inventory[ref]["loras_dir_path"],
            adapter_name=f"lora_{i}",
        )
    pipe.set_adapters([f"lora_{i}" for i in range(len(refs))])
```

Order matters — `set_adapters` applies in list order, mirroring
`LoraStack` tuple ordering used by `CapabilityKey.derive()`.

For Wan 2.2's MoE high+low pair, both tensors load as separate
adapters on the same pipeline; the pipeline routes each tensor to its
matching transformer internally. Assumption to verify in live smoke
(documented in §8 and §11).

VRAM-OOM at `set_adapters` triggers a rollback: handler reloads the
previous adapter set, returns 200 with `swap_rejected` populated
(§9.4 / failure mode 7.4).

### 7.5 Cold-boot path

`_load_pipeline()` accepts `initial_lora_stack:
list[ArtifactDownloadSpec] | None`. When set, startup downloads each
LoRA before the first `/generate`, populating `_inventory` + loading
adapters. Provisioner threads the cfg's `models[kind=lora]` entries
into this call.

This is the **basic LoRA-loading at cold-boot** capability that
Diffusers engine doesn't have today — prerequisite for the swap path
to mean anything.

## 8. Ledger schema additions

### 8.1 `PodEntry` extension

```python
class PodEntry(BaseModel):
    # ... existing fields unchanged ...
    capability_key: str
    warm_attach_key: str = ""                                  # NEW; "" = lazy-derive
    lora_inventory: list[LoraInventoryEntry] = []              # NEW
    loras_dir_free_bytes: int | None = None                    # NEW
    loras_dir_free_bytes_observed_at_local: str | None = None  # NEW; ISO local-tz
    status: Literal["alive", "degraded"] = "alive"             # NEW; reaper recognizes "degraded"


class LoraInventoryEntry(BaseModel):
    ref: str                          # "civitai:2197303@2474081"
    filename: str
    size_bytes: int
    downloaded_at_local: str          # ISO local-tz
    last_used_at_local: str           # ISO local-tz; drives LRU
    adapter_name: str                 # "lora_0" / "lora_1" / ...
```

### 8.2 Write sites

| Site | Operation | When |
|---|---|---|
| Provisioner (cold-boot path in `core/orchestrator.py`) | Set `warm_attach_key` + initial `lora_inventory` from cold-boot's `/lora/set_stack` response | Once per cold-boot |
| Matcher (after successful swap) | Replace `lora_inventory` + `loras_dir_free_bytes` with `/lora/set_stack` response payload | Every successful warm-attach swap |
| Generate path | Bump `last_used_at_local` for every LoRA in the job's stack | Every successful `/generate` |
| Failure-handler | Set `status="degraded"` when a swap fails in the eviction-required phase | Per failure |

All four go through `Ledger.touch(pod_id, **fields)` — the existing
single-write seam — so the redaction + ephemeral-gate pipelines
already in place handle them without new mechanism.

### 8.3 Read sites

| Site | Operation |
|---|---|
| Matcher | Lookup by `warm_attach_key`; read each candidate's `lora_inventory` + `loras_dir_free_bytes` to compute LRU + disk arithmetic |
| `kinoforge status --id` | Render inventory + free bytes + last-used timestamps |
| Reaper | No new behavior — reaps on existing heartbeat-stale criteria + the new `status="degraded"` value |

## 9. Matcher

### 9.1 Public surface

```python
# src/kinoforge/core/warm_reuse/matcher.py

def find_warm_attach_candidate(
    cfg: Config,
    ledger: Ledger,
    *,
    pod_lock_registry: PodLockRegistry,
    re_probe: Callable[[str], InventorySnapshot] | None = None,
) -> WarmAttachMatch | None:
    """Return a WarmAttachMatch or None.

    WarmAttachMatch carries:
      - pod_id, pod_entry
      - swap_plan: SwapPlan
      - estimated_cost_seconds
    """
```

### 9.2 Algorithm

```
1. new_warm_key  = WarmAttachKey.from_cfg(cfg)
2. new_lora_stack = LoraStack.from_cfg(cfg)
3. candidates = ledger.find_pods_by_warm_attach_key(new_warm_key)
4. Filter:
   - drop heartbeat-stale (existing reaper criteria)
   - drop status == "degraded"
   - drop pods locked by pod_lock_registry (non-blocking acquire fails)
5. For each remaining candidate:
   a. exact-byte fast path: pod.capability_key == cfg.capability_key.derive() → cost=0
   b. else compute SwapPlan:
        to_download = new_refs - pod_refs
        to_evict    = pod_refs - new_refs
        download_bytes_needed = sum(size_hint for r in to_download)
        free_bytes = pod.loras_dir_free_bytes
        if download_bytes_needed <= free_bytes:
            evict_plan = []
        else:
            evict_plan = pick_lru_until_room(
                pod.lora_inventory, to_evict,
                need=download_bytes_needed - free_bytes,
            )
            if evict_plan is None:
                continue            # not enough disk even with full eviction
        cost = estimate_download_seconds(to_download) + small_constant
6. Rank by cost ascending; pick lowest.
7. acquire_result = pod_lock_registry.acquire(pod_id, blocking=False)
   if not acquire_result: try next candidate (or return None → cold-boot)
8. Return WarmAttachMatch(pod_id, swap_plan, cost)
```

### 9.3 Re-probe policy

`pod.loras_dir_free_bytes` is a snapshot. Re-probe rules:

- If `now - loras_dir_free_bytes_observed_at_local > re_probe_threshold_s`
  (default 300 s, configurable per-cfg as
  `compute.lifecycle.lora_swap_re_probe_after_s`), matcher calls
  `re_probe(pod_id)` before committing the swap plan. Updates ledger;
  re-runs swap-plan math.
- Under `--ephemeral`, ledger has nothing persisted (in-memory only);
  matcher ALWAYS re-probes on first warm-attach attempt within a fresh
  ephemeral session. First probe populates the in-memory ledger; later
  calls in the same session skip the round-trip.

### 9.4 `PodLockRegistry`

```python
# src/kinoforge/core/warm_reuse/pod_lock.py

class PodLockRegistry:
    """In-process per-pod_id threading.Lock registry.

    v1 limitation: in-process only. Multiple kinoforge processes on
    the same machine attaching to the same pod will not see each
    other's locks. Deferred to v2 / Layer H.
    """

    def acquire(self, pod_id: str, *, blocking: bool = False, timeout: float | None = None) -> bool: ...
    def release(self, pod_id: str) -> None: ...
    def __contains__(self, pod_id: str) -> bool: ...
```

Lock held for the duration of (`/lora/set_stack` + `/generate` +
result()). Released in `finally`. Released automatically on thread
death.

### 9.5 Cost estimator

`estimate_download_seconds(to_download)` uses a fixed
`bytes_per_second` constant (v1 default: 100 MB/s, derived from one
live observation; documented as a module constant with a comment).
v2 candidate: per-source historical bandwidth from the ledger.

## 10. Ephemeral integration

### 10.1 No new policy field

The new persistent-write fields ride on the existing `PodEntry`
shape, which the `ledger_record` gate already protects. The pattern at
`src/kinoforge/core/lifecycle.py:502` short-circuits the payload to
`session.in_memory_ledger` under strict policy. New fields land in
the same payload — no new branch, no new field on `EphemeralPolicy`.

### 10.2 Observed-ref redaction

```python
# src/kinoforge/core/warm_reuse/redaction.py

def _register_observed_lora_refs(snapshot: InventorySnapshot) -> None:
    """Register every observed LoRA ref under the lora:ref token kind.

    Idempotent. Called after every /lora/inventory and /lora/set_stack
    response. Ensures observed refs are redacted from subsequent log
    output for the rest of the session.
    """
    r = RedactionRegistry.instance()
    pairs = [(entry.ref, "lora:ref") for entry in snapshot.inventory]
    r.add_many(pairs)
```

Called from `find_warm_attach_candidate` after every re-probe and
from the swap-execution path after every `/lora/set_stack` response.
AST-scan invariant (§5.4) enforces the call at every reader site.

### 10.3 Pod-side disk untouched at session exit

`EphemeralSession.__exit__` already walks `_registered_stores` and
calls `store.delete_run(run_id)` when
`policy.delete_on_completion=True`. The pod's LoRA cache directory is
NOT a registered store and is NOT scrubbed at exit. Pod-side files
survive across ephemeral sessions so warm-reuse-with-different-LoRAs
works under `--ephemeral`. Pod disk goes away when the pod itself is
destroyed (existing reaper or operator `kinoforge destroy`).

### 10.4 Residual threat vector

Documented in §3. Mitigation already partially in place via
`pod_name_includes_alias=False` under strict policy. Opt-in pod-side
post-session scrub deferred to §11.

## 11. Failure modes

See §7 of the brainstorm transcript for the discussion narrative.
This section captures the locked-in contract.

### 11.1 Error classes

```python
# src/kinoforge/core/errors.py

class LoraSwapError(KinoforgeError):
    """Base for all LoRA-swap failures."""
    pod_id: str
    def manual_cleanup_command(self) -> str: ...

class LoraSwapDownloadError(LoraSwapError):
    """Download failed; no eviction had happened yet. Pod healthy."""
    ref: str
    underlying: str

class LoraSwapDegradedPodError(LoraSwapError):
    """Download failed AFTER eviction started. Pod in half-state.
    Marked status=degraded; existing reaper handles."""
    evict_completed: list[str]
    download_failed: str
    underlying: str

class LoraSwapPodUnreachableError(LoraSwapError):
    """Pod proxy returned past retry budget. Marked degraded."""
    underlying: str

class LoraSwapVramOomError(LoraSwapError):
    """set_adapters OOM; rollback to previous adapter set succeeded.
    Pod healthy at previous LoRA stack."""
    dropped_refs: list[str]

class LoraSwapDiskFullError(LoraSwapError):
    """Mid-download disk full. Marked degraded."""
    evict_completed: list[str]
    download_failed: str
```

### 11.2 Behavior matrix

| Class | Pod state after | Marked `degraded`? | Pod-lock released? |
|---|---|---|---|
| `LoraSwapDownloadError` | Healthy, partial inventory | No | Yes |
| `LoraSwapDegradedPodError` | Half-state | Yes | Yes |
| `LoraSwapPodUnreachableError` | Unknown | Yes | Yes |
| `LoraSwapVramOomError` | Healthy, previous stack | No | Yes |
| `LoraSwapDiskFullError` | Half-state | Yes | Yes |

### 11.3 Lock-held-during-crash recovery

In-process `threading.Lock` releases on thread death. Pod-side
`asyncio.Lock` releases when the request handler returns. Pod boot
reads its on-disk LoRA cache and rebuilds the in-memory inventory from
scratch. Orchestrator's next attach attempt sees the empty/rebuilt
inventory and reconciles via re-probe → matcher trusts the re-probe,
updates the ledger, proceeds. No special recovery code.

## 12. Operator UX

### 12.1 `kinoforge status --id <pod_id>`

Extended to render a LoRA section when `pod_entry.lora_inventory` is
non-empty:

```
pod-7b2 (warm)
  warm_attach_key: wak-a3f7e1b2c4d5
  capability_key: cap-9988aabbccdd
  heartbeat: 18s ago
  cost: $0.41 / $3.50 budget
  loras (3 resident, 412 MB used, 198.4 GB free):
    civitai:2197303@2474081  314 MB  last_used 12m ago  adapter lora_0
    civitai:2197303@2474073  287 MB  last_used 12m ago  adapter lora_1
    hf:org/repo:foo.safetensors  47 MB  last_used 4h ago  adapter lora_2
```

Refs go through the existing log redaction filter. JSON mode emits
the inventory as a list of dicts.

### 12.2 `kinoforge generate --dry-run-swap` / `kinoforge batch --dry-run-swap`

New flag. Runs the matcher, prints `WarmAttachMatch` (or "no warm
candidate, would cold-boot"), exits before any HTTP to the pod. Does
NOT acquire the lock; documented as informational only.

### 12.3 `kinoforge pod lora ls <pod_id>`

New thin subcommand. Hits `GET /lora/inventory` directly. Distinct
from `status --id` because it bypasses the ledger — useful when the
ledger view is stale or when running under `--ephemeral` (where the
ledger has no inventory persisted but the pod still does).

### 12.4 Config surface

One new optional cfg field:

```yaml
compute:
  lifecycle:
    lora_swap_re_probe_after_s: 300   # NEW; default 300; 0 disables stale-check
```

That's it. The LoRA stack itself stays in `models[kind=lora]` or vault
`loras:`.

## 13. Testing

Mirrors kinoforge's existing testing posture: dense unit tests at every
seam, one offline integration test per behavior, one live smoke at the
very end. AST-scan invariants for cross-cutting safety properties.

### 13.1 Unit tests (offline)

See the table in §5.3 for the full file list. Each pins a single
behavior with adversarial assertions (per the user-scope `test-design`
skill): no weak `is not None`, no over-mocking, no happy-path-only
coverage.

### 13.2 Integration tests (offline, FakeEngine + LocalProvider)

Five scenarios in `tests/integration/test_warm_reuse_lora_*.py`,
exhaustively covering: empty→non-empty stack, overlap, strict
eviction, cold-boot fallthrough, ephemeral cross-session preservation.

### 13.3 Live smoke (the green gate)

Single end-to-end smoke at `tests/live/test_wan22_lora_warm_reuse.py`.
Four generations on the same RunPod A100 80GB pod, ~$2 spend cap:

1. Cold-boot with 0 LoRAs.
2. Warm-attach with 2 LoRAs (Arcane high + low pair per README default).
3. Warm-attach with different 2 LoRAs (TBD — pick one additional public Wan 2.2 LoRA + a tensor from Arcane).
4. Warm-attach back to 0 LoRAs (full evict).

Asserts each generation produces a valid mp4; ledger inventory matches
pod's `/lora/inventory` at every step; cost stays under cap; `kinoforge
status` + `kinoforge pod lora ls` outputs agree.

Pod destruction at end requires explicit operator authorization per
the user-scope memory rule about destroying pods — the smoke gates
destruction behind an interactive prompt or fails open (documented in
the smoke's docstring).

### 13.4 Reaper unit test

`tests/core/test_reaper_degraded_pods.py` pins that the reaper
recognizes `status="degraded"` as reap-eligible alongside the existing
heartbeat-stale criteria.

## 14. Acceptance criteria (v1 GREEN gate)

1. All unit tests pass: `pixi run pytest tests/core/ tests/engines/`.
2. All integration tests pass: `pixi run pytest tests/integration/`.
3. AST-scan invariant: `pixi run pytest tests/test_no_unredacted_writes.py` passes with the new rules.
4. `CapabilityKey.derive()` byte-equality regression: `pixi run pytest tests/core/test_capability_key_split.py` confirms unchanged hash for all golden inputs.
5. Live smoke at `tests/live/test_wan22_lora_warm_reuse.py` produces 4 valid mp4s and reports total spend under $2.
6. `kinoforge status --id <pod>` and `kinoforge pod lora ls <pod>` render the inventory cleanly.
7. `--dry-run-swap` prints the matcher's decision for both same-stack and different-stack cases on a real warm pod.
8. README updated with a "LoRA-flexible warm-reuse" section explaining the user-facing behavior + the four supported scenarios from the live smoke.

## 15. Out of scope — full deferral table

| Item | Why deferred | Promotion trigger |
|---|---|---|
| ComfyUI engine LoRA-flexible warm-reuse | Gated on existing C23 follow-up (LoRA wiring in Wan ComfyUI graphs) | C23 ships + first Wan 2.2 ComfyUI live smoke with a LoRA stack |
| Cold-boot fallback on swap failure (Option B / §7) | Adds orchestrator-side automatic destroy + reprovision after eviction-required failure | Live data shows eviction-required failures recurring |
| Half-state recovery (Option C / §7) | Adds pod-side state machine for resumable swaps + ledger schema for partial-swap entries | Option B's cost-per-recovery becomes painful |
| Concurrent jobs on same pod | Lift the per-pod lock with pod-side LoRA refcount tracking | Measured throughput on a single warm pod becomes a bottleneck |
| Cross-process pod lock | In-process lock doesn't see other kinoforge processes | Folded into existing Layer H whenever that lands |
| Operator-controlled pinning (`pod lora pin/unpin`) | Adds CLI surface + ledger field for pinned-set tracking | Operator reports unwanted eviction of a LoRA they were about to re-use |
| Manual single-LoRA eviction (`pod lora rm`) | `kinoforge destroy` covers the "reset this pod's disk" use case in v1 | Operator workflow needs surface eviction without losing the pod |
| Smart eviction (size-weighted) | LRU evicts oldest first regardless of size | Eviction pattern shows pure LRU evicting many small old LoRAs unnecessarily |
| Persistent matcher-decision log | Separate from INFO log — operator-visible record | Operators ask for it |
| Per-source historical bandwidth in cost estimator | Fixed constant suffices for v1 | Estimates drift enough that operators stop trusting them |
| Opt-in pod-side post-session LoRA scrub under `--ephemeral` | Stronger ephemeral threat-model guarantee at the cost of warm-reuse benefit | Operator requests stronger ephemeral guarantees |
| Diffusers pipeline reload OOM live-test coverage | Unit coverage exists with mocked OOM; live coverage expensive | Real OOM regression slips past unit tests |
| Non-Wan pipeline LoRA support | Pod-side server module is Wan-specific | A second Diffusers pipeline ships in kinoforge |

### 15.1 Explicit non-goals

- A pluggable `MatchStrategy` abstraction (Approach 3 from the
  identity-factoring question). Over-engineering for one new use case.
- Cross-pod LoRA cache sharing. Different architecture.
- First-class LoRA versioning beyond the ref's `@versionId` component.
- Smart prefetch.

### 15.2 Known v1 limitations

- In-process pod-lock only; multi-`kinoforge`-process on one machine
  can race (same as existing warm-reuse).
- Cost estimator uses a fixed bandwidth constant; will be wrong for
  new sources.
- `--dry-run-swap` doesn't hold a lock; previewed pod may be claimed
  by another job between preview and commit.
- Ephemeral threat: pod-side inventory observable across sessions
  (§3, §10.4).

## 16. References

- `docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md` — vault, `--ephemeral`, `RedactionRegistry`, `EphemeralPolicy`, `EphemeralSession`.
- `docs/superpowers/plans/2026-06-13-b3-warm-reuse-retrofit-plan.md` — existing warm-reuse machinery (`warm_reuse_auto_attach`, `--no-reuse`).
- `docs/superpowers/plans/2026-06-19-wan22-native-t2v-a14b.md` — the Wan 2.2 native T2V Diffusers green path this builds on.
- `PROGRESS.md` C23 — ComfyUI LoRA wiring gap (deferred for v2).
- `PROGRESS.md` C24 — Default test LoRA pair (Arcane Style Wan 2.2) recorded in README.
- `src/kinoforge/core/profiles.py` — existing `CapabilityKey`.
- `src/kinoforge/core/lifecycle.py:502` — the `ledger_record` gate this spec rides.
- `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — the pod-side server we extend.
- `tests/test_no_unredacted_writes.py` — the AST-scan invariant we extend with §5.4 rules.
