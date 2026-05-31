# Layer H — Cross-process discovery lock (design)

**Status:** Validated, ready for implementation plan.
**GitHub issue:** #7 Cross-process discovery lock.
**Date:** 2026-05-30.
**Prior layers:** A (uri_for) · B (continuity) · C (S3/GCS stores) · D (.env loader) · E (per-engine extract_last_frame) · F (engine asset wiring) · G (concurrent backend scheduler).
**Closes:** GitHub issue #7 + post-Layer-C residual at `PROGRESS.md` L126 (multi-node cloud-backed ledger).

## 1. Problem

Two coordination surfaces in `kinoforge` assume a single Python process:

1. **`JsonProfileCache.resolve_or_discover`** (`src/kinoforge/core/profiles.py:285`) — `threading.Lock` + per-key `threading.Event` map provide single-flight discovery within one process. Two processes pointed at the same store both race past the cache miss, both probe (expensive — `inspect_capabilities()` provisions a smallest-viable backend), both persist; last writer wins.
2. **`Ledger.record` / `Ledger.forget`** (`src/kinoforge/core/lifecycle.py:434`, `:465`) — read-modify-write per call with an explicit docstring "Single-process assumption: no cross-process concurrency". Two processes mutating the ledger concurrently lose entries; a lost record means an orphaned billing instance.

Both surfaces became real gaps after Layer C (cloud-backed `ArtifactStore`) and Layer G (concurrent backends) shipped, because realistic multi-node setups now point multiple `kinoforge` workers at one shared store.

## 2. Goals & non-goals

**Goals.**
- Best-effort cross-process serialization for profile discovery and ledger mutation.
- Single primitive (lease-based mutex) reused across both surfaces.
- Mirror existing project patterns: registry-mediated, store-backed, vendor-agnostic core, dependency-injected I/O seams, offline-only tests.
- Ship three real adapters: same-host (`FileLock` over `fcntl.flock`), AWS (`S3CloudLock`), GCP (`GCSCloudLock`).

**Non-goals.**
- Strict distributed-lock semantics (fencing tokens, Paxos/Raft consensus, Byzantine fault tolerance).
- Cross-cloud lock (e.g. S3 store + GCS lock); lock backend follows store backend.
- New YAML config block; tuning is per-call kwargs only.
- Auto-renewing leases (heartbeat threads). Callers pass a TTL generous enough for their worst-case operation.

## 3. Decisions (locked)

| Topic | Decision |
|---|---|
| **Scope** | Profile cache + ledger (one primitive covers both). |
| **Primitives** | `FileLock` (fcntl) + `S3CloudLock` (conditional PUT) + `GCSCloudLock` (`if_generation_match=0`). |
| **Semantics** | Lease-based mutex with per-call `ttl_s`. No fencing tokens, no auto-renewal. |
| **TTL configuration** | Per-call `ttl_s` parameter to `acquire_lock`; no YAML surface. |
| **Adapter wiring** | `ArtifactStore.acquire_lock(key, *, ttl_s) -> Lock` factory. Lock backend implicit in store choice. |
| **Integration** | Approach A — outer cross-process lock wraps existing in-process synchronization (`threading.Lock` + per-key `Event` map for profiles; per-call RMW for ledger). |
| **Failure model** | Best-effort. TTL expiry → another holder may proceed. Profile JSON is idempotent (last writer wins, semantically equal). Ledger RMW races bounded by TTL window. |

## 4. Architecture

### 4.1 New ABC — `src/kinoforge/core/locks.py`

```python
class LockToken(Protocol):
    """Opaque handle returned by acquire_lock; passed to release_lock."""
    key: str

class Lock(Protocol):
    """Distributed lease-based mutex; obtained via ArtifactStore.acquire_lock.

    Always usable as a context manager. ``acquire`` may block; ``release`` is
    idempotent and silently no-ops when the lock was already stolen after TTL.
    """
    def acquire(
        self,
        *,
        blocking: bool = True,
        timeout_s: float | None = None,
    ) -> LockToken | None: ...
    def release(self, token: LockToken) -> None: ...
    def __enter__(self) -> LockToken: ...
    def __exit__(self, *exc) -> None: ...
```

### 4.2 New factory on existing `ArtifactStore` ABC

`src/kinoforge/stores/base.py` gains an abstract method:

```python
@abstractmethod
def acquire_lock(self, key: str, *, ttl_s: float) -> Lock: ...
```

Every concrete store ships its own implementation. `LocalArtifactStore` → `FileLock`; `S3ArtifactStore` → `S3CloudLock`; `GCSArtifactStore` → `GCSCloudLock`. Lock layout is store-local: `<store_root>/_locks/<sanitized_key>.lock` where `sanitized_key = key.replace("/", "__")`.

### 4.3 Concrete adapters

**`FileLock` (Local store).**
- `fcntl.flock(fd, LOCK_EX)` with optional `LOCK_NB` for non-blocking.
- On-disk JSON sidecar `{token: uuid4(), holder_pid, expires_at}` is informational; OS owns lifecycle (fcntl auto-releases on process death).
- Blocking + `timeout_s`: poll `LOCK_NB` with injected `sleep`.
- Seams: `flock_fn`, `clock`, `sleep` constructor kwargs.

**`S3CloudLock`.**
- Acquire: `s3.put_object(IfNoneMatch="*")` (supported by S3 since 2024-11). Body = `{token, holder, expires_at}` JSON.
- Contention → GET existing lock → if `now > expires_at`, steal via `delete_object(IfMatch=existing_etag)` then retry CAS PUT.
- Release: `delete_object(IfMatch=stored_etag)`; silent on `PreconditionFailed` (stolen after TTL).
- Seam: reuses `S3ArtifactStore`'s injected `s3_client`.

**`GCSCloudLock`.**
- Acquire: `blob.upload_from_string(if_generation_match=0)` (native CAS, no eventual-consistency caveats).
- Contention → GET existing → expiry check → `blob.delete(if_generation_match=existing_gen)` then retry CAS upload.
- Release: `blob.delete(if_generation_match=stored_generation)`; silent on `PreconditionFailed`.
- Seam: reuses `GCSArtifactStore`'s injected `gcs_client`.

**Stealing semantics (cloud adapters).** Read existing lock → parse `expires_at` → if expired, conditional-delete on captured ETag/generation → retry full CAS PUT. If delete fails (someone else stole already), retry loop from the top.

### 4.4 Test fakes

- `InMemoryLock` lives in `tests/conftest.py` (not in registry; mirrors existing fake-source/fake-engine pattern). Dict-backed `{key: (token, expires_at)}` with monotonic clock.
- `FakeS3Client` extended to honor `IfNoneMatch="*"` and `IfMatch=<etag>` headers and return `PreconditionFailed` semantics.
- `FakeGCSClient` extended to honor `if_generation_match=0` and `if_generation_match=<n>` and return `google.api_core.exceptions.PreconditionFailed`.

## 5. Integration sites

### 5.1 `JsonProfileCache.resolve_or_discover` (`profiles.py:285`)

```python
def resolve_or_discover(self, key, engine, backend):
    try:
        return self.resolve(key)                       # fast path: cache hit, no lock
    except ProfileNotCached:
        pass

    hash_key = key.derive()
    with self._store.acquire_lock(f"profiles/{hash_key}", ttl_s=self._discover_ttl_s):
        # Re-check under outer lock — another process may have populated it
        try:
            return self.resolve(key)
        except ProfileNotCached:
            pass
        # In-process single-flight stays for multi-thread safety within this process
        return self._discover_single_flight(key, engine, backend)
```

`_discover_single_flight` is the existing `threading.Lock` + `Event` map body, factored out of `resolve_or_discover`. New constructor kwarg `discover_ttl_s: float = 300.0`.

### 5.2 `Ledger.record` / `Ledger.forget` (`lifecycle.py:434`, `:465`)

Each mutating RMW wrapped:

```python
def record(self, instance):
    with self._store.acquire_lock(f"ledger/{self._run_id}", ttl_s=self._mutate_ttl_s):
        entries = self._read_entries()
        entries.append({...})
        self._write_entries(entries)
```

Same wrap on `forget`. `entries()` stays lock-free (snapshot read; eventual consistency acceptable for reporting). New constructor kwarg `mutate_ttl_s: float = 30.0`.

### 5.3 Callers

- `orchestrator.generate` and `cli._cmd_*` pass through defaults; no behavior change for single-process users.
- No YAML surface, no `LockConfig` block. Power users construct `JsonProfileCache(store, discover_ttl_s=...)` directly.

### 5.4 What does NOT change

- `ArtifactStore.uri_for`, `put_*`, `get_*`, `list`, `delete` signatures untouched.
- `LifecycleManager` (`_states` dict) — already process-local by design.
- `BudgetTracker.enforce` — reads `_entry_for` via lock-free `entries()`, mutates via already-locked `Ledger.forget`. No change.
- `Ledger._read_entries` / `_write_entries` body unchanged; outer lock added at the public surface.

## 6. Errors

Extend `core/errors.py`:

```python
class LockError(KinoforgeError):
    """Base for lock-acquisition failures."""

class LockTimeout(LockError):
    """acquire(blocking=True, timeout_s=X) elapsed without obtaining lock."""
```

`release` is silent when the lock was already stolen after TTL: log at INFO and return. No `LockStolen` exception class — best-effort semantics deliberately hide the stolen-after-TTL case from callers (the work they did under the expired lease is already irrevocable). Adding strict detection is a future-layer concern if a caller ever needs to distinguish.

## 7. Failure model & threat scope

- **Holder dies mid-hold.** TTL expires → next acquirer succeeds via the steal-after-TTL path. Profile JSON: subsequent persist semantically identical for the same `CapabilityKey`. Ledger: at-most-one mutation in flight when TTL is sized > worst-case RMW (default 30s).
- **Clock skew between hosts.** Generous TTLs absorb skew up to the configured TTL. No NTP enforcement. Failure mode = unnecessary stealing of a still-live lock; treated as benign.
- **Network partition during cloud-lock release.** Object remains until TTL expires; subsequent acquirer steals after expiry. No orphan.
- **Concurrent stealing race.** Two acquirers both observe `now > expires_at`, both attempt conditional-delete on the same ETag/generation — exactly one succeeds (CAS guarantee). Loser retries.
- **Out of scope.** Fencing tokens, Paxos/Raft consensus, Byzantine fault tolerance, intentional adversarial actors.

## 8. Testing — offline hard constraint preserved

Per CLAUDE.md + `test-design` skill: red-first, every test states behavior-under-test + a concrete failing bug.

| Surface | Test path |
|---|---|
| `Lock` Protocol | `InMemoryLock` conformance suite (acquire/release/blocking/timeout/expire). |
| `FileLock` | `tmp_path` + spy `flock_fn` + `FakeClock`; one multi-process subprocess integration test. |
| `S3CloudLock` | Extended `FakeS3Client` (IfNoneMatch / IfMatch) + `FakeClock`. |
| `GCSCloudLock` | Extended `FakeGCSClient` (if_generation_match) + `FakeClock`. |
| `ArtifactStore.acquire_lock` ABC | Conformance test parametrized over all three stores. |
| `JsonProfileCache` outer-lock wiring | `InMemoryLock` + `FakeStore`; assert lock acquired AND cache rechecked inside lock. |
| `Ledger` record/forget under lock | `InMemoryLock` + `FakeStore`; assert two interleaved `record` calls produce both entries (no lost update). |

Key red-first tests:

1. `acquire_lock` returns a fresh `Lock` per call (no singleton requirement).
2. `FileLock` blocking acquire on held lock + injected sleep eventually succeeds after first releases.
3. `S3CloudLock` steals after TTL: `FakeClock` jumps past `expires_at`; second acquirer succeeds.
4. `GCSCloudLock` precondition failure on contention with `blocking=False` returns `None`.
5. `JsonProfileCache.resolve_or_discover` calls `inspect_capabilities` exactly once across 4 concurrent threads in one process (existing test) AND exactly once across 2 process simulations (new test: two `JsonProfileCache` instances over the same `FakeStore` + shared `InMemoryLock`).
6. `Ledger.record` interleaved between two `Ledger` instances over the same `InMemoryLock` produces both entries.
7. Lock release after process death simulated by clock jump → next acquirer succeeds without explicit release.
8. `LockTimeout` raised when `blocking=True, timeout_s=X` elapses and the lock is still held.

Expected test-count delta: +35–45 (8 conformance + 5 file + 6 S3 + 6 GCS + 6 cache + 5 ledger + 3 cli/integration). Floor ~590 post-merge from current 555.

## 9. Build order (phases → tasks)

1. **Task 1** — `core/locks.py`: `Lock` Protocol + `LockToken` + `LockError`/`LockTimeout` in `core/errors.py` + `InMemoryLock` test primitive; conformance test suite.
2. **Task 2** — `ArtifactStore.acquire_lock` abstract method (no default body) + conformance test scaffolding parametrized over concrete stores.
3. **Task 3** — `FileLock` + `LocalArtifactStore.acquire_lock`; `tmp_path` + multi-process subprocess integration test.
4. **Task 4** — `S3CloudLock` + `S3ArtifactStore.acquire_lock`; extend `FakeS3Client`.
5. **Task 5** — `GCSCloudLock` + `GCSArtifactStore.acquire_lock`; extend `FakeGCSClient`.
6. **Task 6** — `JsonProfileCache` outer-lock wiring + `discover_ttl_s` kwarg + cross-process discovery test.
7. **Task 7** — `Ledger.record`/`forget` outer-lock wiring + `mutate_ttl_s` kwarg + interleaved-mutation test.
8. **Task 8** — README "Multi-node coordination" section + PROGRESS Phase 18 + close #7 + `--no-ff` merge.

## 10. Dependencies

- **stdlib** `fcntl` — `FileLock`. Already POSIX-only; matches CI matrix (Linux + macOS, Windows declined per `windows-migration-cancelled.md`).
- **boto3** (lazy-imported, already a Layer C dep) — `S3CloudLock` reuses `S3ArtifactStore`'s `s3_client`.
- **google-cloud-storage** (lazy-imported, already a Layer C dep) — `GCSCloudLock` reuses `GCSArtifactStore`'s `gcs_client`.
- No new conda-forge or PyPI packages required.

## 11. Deferred (not built in Layer H)

- Auto-renewing leases (heartbeat thread). Callers pass generous TTLs.
- Fencing tokens for strict distributed-lock guarantees.
- Cross-store lock kind (e.g. S3 store + file lock); lock backend follows store backend.
- `LockConfig` YAML block; tuning is per-call kwargs only.
- Azure / B2 / R2 lock adapters (no corresponding stores yet).
