# Layer G — Concurrent backend scheduler

**Date:** 2026-05-30
**Scope:** Ship `ConcurrentPool` as a drop-in sibling of `SequentialPool` behind
the existing `BackendPool` ABC. Dispatch one or more jobs across one or more
registered `GenerationBackend` replicas with bounded per-backend concurrency
and explicit shutdown. Tweak `GenerateClipStage` so the t2v non-chained
fallback (the one shape with real intra-request parallelism) actually exploits
the pool. Swap the CLI's `SequentialPool` for `ConcurrentPool` with
`max_in_flight` sourced from `LifecycleConfig` — defaults preserve byte-equal
behaviour.
**GitHub issue:** #3

## 1. Problem

`SequentialPool` runs every job inline through `_backends[0]` and pre-resolves
the Future before returning. `add(backend)` accepts multiple backends but only
the first is ever used. `GenerateClipStage` calls `pool.submit(job).result()`
in a Python for-loop, so even a hypothetical concurrent pool would serialise
behind the loop. The result: kinoforge has no way to overlap multiple
concurrent `generate()` calls (multi-tenant / batch render), no way to exploit
a backend with `max_in_flight > 1` (one Comfy server handling parallel
prompts), and no way to fan out the t2v non-chained fallback across N
segments.

Layer G fixes all three: a true ConcurrentPool that load-balances across
backend replicas under bounded caps, plus the one stage branch needed to make
the t2v fan-out in-tree.

## 2. Non-goals

- Multi-replica provisioning (orchestrator-level multi-deploy). Pool is
  forward-compatible — when a caller adds N backends from N instances, the
  pool dispatches across them. Wiring N instances is a future layer.
- Async/await rewrite. `BackendPool`, `GenerationBackend.submit`,
  `GenerationBackend.result` stay synchronous.
- Cross-process / distributed dispatch. Single-process only (consistent with
  the existing single-process Ledger assumption).
- Stitching, audio sync, keyframe stage (GitHub issues #2, #4 — separate
  layers).
- Statistics / observability hooks (utilization gauges, per-backend latency
  histograms). Not load-bearing; YAGNI.

## 3. Resolved design questions

| Question | Decision |
|---|---|
| Scope | Full scheduler: segments (fan-out across one backend's cap) + replicas (fan-out across multiple registered backends). |
| Per-backend cap source | Per-add keyword param: `pool.add(backend, max_in_flight=N)`. Default `1`. No ABC change. |
| Dispatch policy | Least-loaded by utilization (`in_flight / cap`). Ties broken by registration order via Python `min`'s left-bias. |
| Backpressure | Queue internally, return pending `Future`. `submit()` never blocks the caller; queueing is delegated to `ThreadPoolExecutor`'s internal work queue. |
| Map failure | Fail-fast: capture first exception, cancel queued (not-yet-started) futures, drain in-flight to completion (results discarded), re-raise first. |
| Stage touch | Branch `GenerateClipStage.run` on `should_chain`. Chained branch + 1-job native branch unchanged. Non-chained N>1 calls `pool.map(jobs)`. |
| Shutdown | Explicit `close()` + context-manager protocol on the ABC. `executor.shutdown(wait=True)` per backend. SequentialPool gets a no-op `close()`. |
| Concurrency primitive | One `concurrent.futures.ThreadPoolExecutor` per backend, `max_workers = cap`. Selection at `submit()` time, not at worker pickup. |

## 4. Module surface

### Modified: `src/kinoforge/core/interfaces.py`

`BackendPool` ABC gains one abstract method + context-manager protocol:

```python
class BackendPool(ABC):
    """Dispatches jobs across one or more GenerationBackends.

    Implementations may call ``backend.submit`` / ``backend.result`` from
    multiple threads concurrently; backends MUST be thread-safe (no shared
    mutable state across calls).
    """

    @abstractmethod
    def add(self, backend: GenerationBackend) -> None: ...

    @abstractmethod
    def submit(self, job: GenerationJob) -> Future[Artifact]: ...

    @abstractmethod
    def map(self, jobs: list[GenerationJob]) -> list[Artifact]: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "BackendPool":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
```

The `add` ABC signature stays one-arg. Subclasses are free to accept an extra
kw-only `max_in_flight` param (Liskov-safe: callers using the ABC signature
get the default `1`).

### Modified: `src/kinoforge/core/pool.py`

`SequentialPool` gains:

```python
def add(self, backend: GenerationBackend, *, max_in_flight: int = 1) -> None:
    # cap kwarg accepted for parity; ignored — only _backends[0] runs.
    self._backends.append(backend)

def close(self) -> None:
    # No threads owned; nothing to release.
    return
```

New class in the same module:

```python
@dataclass
class _Slot:
    backend: GenerationBackend
    executor: concurrent.futures.ThreadPoolExecutor
    cap: int
    in_flight: int = 0  # mutated only under ConcurrentPool._lock


class ConcurrentPool(BackendPool):
    """Bounded-concurrency pool across one or more backend replicas.

    Each backend gets its own ThreadPoolExecutor sized to its
    ``max_in_flight`` cap. submit() picks the least-loaded backend by
    in_flight/cap utilization and forwards to that backend's executor.
    map() dispatches all jobs eagerly and returns results in input order;
    on first exception it cancels queued futures, drains in-flight, and
    re-raises.

    Use as a context manager for deterministic shutdown::

        with ConcurrentPool() as pool:
            pool.add(backend, max_in_flight=4)
            results = pool.map(jobs)
    """

    def __init__(self) -> None: ...
    def add(self, backend: GenerationBackend, *, max_in_flight: int = 1) -> None: ...
    def submit(self, job: GenerationJob) -> concurrent.futures.Future[Artifact]: ...
    def map(self, jobs: list[GenerationJob]) -> list[Artifact]: ...
    def close(self) -> None: ...

    # internals
    def _pick(self) -> _Slot: ...
    def _release(self, slot: _Slot) -> None: ...
    def _run_one(self, slot: _Slot, job: GenerationJob) -> Artifact: ...
```

### Modified: `src/kinoforge/pipeline/generate_clip.py`

Single new branch around the existing for-loop:

```python
should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())
if not should_chain and len(jobs) > 1:
    # Parallel dispatch for non-chained fallback (t2v with N segments).
    results = list(self.pool.map(jobs))
else:
    # Chained continuity (i2v) or 1-job native — sequential by data
    # dependency or by triviality. Existing loop unchanged.
    results = []
    for i, job in enumerate(jobs):
        if i > 0 and should_chain:
            ...  # existing tail-frame injection + engine.validate_spec
        art = self.pool.submit(job).result()
        results.append(art)
```

### Modified: `src/kinoforge/cli.py`

Single-line swap at the existing `SequentialPool(backend)` site, wrapped in
`with` for deterministic shutdown:

```python
with ConcurrentPool() as pool:
    pool.add(backend, max_in_flight=cfg.lifecycle.max_in_flight)
    # existing stage construction + .run call unchanged
```

Default `LifecycleConfig.max_in_flight = 1` → `ConcurrentPool` behaves
byte-equivalent to today's `SequentialPool` (one job at a time, sequential
ordering). Higher caps are opt-in via yaml.

## 5. Dispatch flow

### `submit(job) -> Future[Artifact]`

1. Raise `RuntimeError("pool closed")` if `self._closed`.
2. Raise `RuntimeError("ConcurrentPool has no registered backend")` if no slots.
3. `slot = self._pick()` — acquires `_lock`, finds min-utilization slot,
   bumps `slot.in_flight`, releases lock, returns slot.
4. `return slot.executor.submit(self._run_one, slot, job)`.

The returned Future is unresolved until the executor worker picks it up. If
all `max_workers` workers are busy, the executor's internal queue holds the
call — the caller does not block.

### `_pick() -> _Slot`

```python
with self._lock:
    best = min(self._slots, key=lambda s: s.in_flight / s.cap)
    best.in_flight += 1
    return best
```

Asymmetric caps `[1, 4]`, all idle: utilization 0/1 == 0/4 == 0.0 → `min`
left-bias picks slot[0]. After it runs one job, utilizations are 1/1 = 1.0
vs 0/4 = 0.0 → slot[1] picked for next 4 jobs. Distribution converges to the
cap weights.

### `_run_one(slot, job) -> Artifact`

```python
try:
    job_id = slot.backend.submit(job)
    return slot.backend.result(job_id)
finally:
    self._release(slot)
```

`finally` ensures the slot frees even if `backend.submit` or `backend.result`
raises. The exception propagates through the executor's future.

### `map(jobs) -> list[Artifact]`

```python
futures = [self.submit(j) for j in jobs]   # eager dispatch
results: list[Artifact | None] = [None] * len(jobs)
first_exc: BaseException | None = None
for i, fut in enumerate(futures):
    if first_exc is not None:
        fut.cancel()    # only succeeds for QUEUED futures
        continue
    try:
        results[i] = fut.result()
    except BaseException as e:
        first_exc = e
if first_exc is not None:
    raise first_exc
return cast(list[Artifact], results)
```

Iterating `futures` in input order (not `as_completed`) preserves the caller
contract that results match job order. While blocked on `futures[i]`, every
`futures[j > i]` is already running on workers — no throughput penalty.

### `close()`

```python
with self._lock:
    if self._closed:
        return
    self._closed = True
    slots = list(self._slots)
for slot in slots:
    slot.executor.shutdown(wait=True)  # outside the lock — blocks on workers
```

Two-phase: flip `_closed` flag under lock so new `submit()` calls reject
immediately; release lock before calling executor `shutdown(wait=True)` which
blocks until each in-flight worker finishes. `wait=True` is the cost-safety
choice — no torn HTTP connections, no orphaned remote jobs.

## 6. Error handling & edge cases

| Case | Behaviour |
|---|---|
| Backend raises in `submit` or `result` | Exception propagates through Future; slot freed by `finally`. |
| `map` mid-failure | First exception captured; queued futures cancelled; in-flight drained silently; first exception re-raised. |
| `close()` mid-flight | Blocks on `executor.shutdown(wait=True)`. New `submit()` raises `RuntimeError("pool closed")`. |
| `close()` called twice | Idempotent — early returns under lock. |
| `submit` on empty pool | `RuntimeError("ConcurrentPool has no registered backend")`. |
| `submit` on closed pool | `RuntimeError("pool closed")`. |
| `map([])` | Returns `[]`; no futures created; no `backend.submit` calls. |
| Asymmetric caps `[1, 4]` | Distribution converges to cap weights via utilization metric. |
| All slots saturated | New `submit()` queues inside the chosen executor; never blocks caller. |
| `Future.cancel()` on in-flight job | Returns False; job runs to completion; result/exception silently discarded by `map`. |
| Single-backend cap=1 | Byte-equivalent to SequentialPool. All existing tests pass unmodified. |

### What we explicitly do NOT do

- **Kill in-flight HTTP calls.** No portable safe way to interrupt a running
  `urllib` request from another thread without risking leaked sockets or
  orphaned remote jobs. Brief drain window is accepted.
- **Connection pooling.** `urllib` opens per-request; Layer G doesn't change
  that.
- **`add` while `submit` is in-flight.** Documented as caller responsibility
  (same as today's pool). CLI/orchestrator call `add` once during setup.

## 7. Thread-safety contract

- `_slots` list mutated only by `add()` and `close()`. Caller responsibility
  to call these from a single setup/teardown thread.
- `slot.in_flight` always mutated under `_lock`.
- `slot.executor` is thread-safe by stdlib contract (`ThreadPoolExecutor.submit`).
- `backend.submit` / `backend.result` invoked from worker threads → backends
  MUST be thread-safe. Today's HTTP-only backends (Fake, ComfyUI, Diffusers,
  Hosted) satisfy this: each call opens its own `urllib` request, no shared
  mutable state. Documented in the `BackendPool` ABC docstring.
- `_lock` held only across the `O(len(slots))` `min` walk and counter mutation
  — never across `backend.*` calls. No deadlock risk.

## 8. Test strategy

### Determinism primitive: `BlockingFakeBackend`

New test helper in `tests/core/conftest.py` — a backend whose `submit()`
returns immediately but whose `result()` blocks on a per-job
`threading.Event`. Tests control completion order by calling
`backend.release(job_id)`. 5-second safety timeout in `result()` prevents
test hangs from masking bugs. No `time.sleep` anywhere.

```python
class BlockingFakeBackend(GenerationBackend):
    def __init__(self) -> None:
        self._gates: dict[str, threading.Event] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def submit(self, job: GenerationJob) -> str:
        with self._lock:
            self._counter += 1
            jid = f"job-{self._counter}"
            self._gates[jid] = threading.Event()
        return jid

    def result(self, job_id: str) -> Artifact:
        if not self._gates[job_id].wait(timeout=5.0):
            raise TimeoutError(f"{job_id} never released")
        return Artifact(filename=f"{job_id}.mp4", meta={"jid": job_id})

    def release(self, job_id: str) -> None:
        self._gates[job_id].set()
```

### Acceptance criteria

**`tests/core/test_concurrent_pool.py` (new, ~20 tests)**

| AC | Behaviour |
|---|---|
| 1 | `submit()` returns a Future that resolves to the backend's Artifact after release. |
| 2 | Empty pool `submit` raises `RuntimeError("ConcurrentPool has no registered backend")`. |
| 3 | Closed pool `submit` raises `RuntimeError("pool closed")`. |
| 4 | `add(backend, max_in_flight=N)` honoured — submit N+1 jobs without release; exactly N reach `backend.submit`. |
| 5 | Single backend cap=1 — 3 jobs run strictly serially. |
| 6 | Single backend cap=4 — 4 jobs all reach `backend.submit` before any release. |
| 7 | Two backends caps `[1, 1]` idle — 2 submits distribute one each, registration order. |
| 8 | Two backends caps `[1, 4]` idle — 5 submits: backend[1] gets 4, backend[0] gets 1. |
| 9 | Two backends caps `[2, 2]`, backend[0] pre-occupied with 1 — 2 new submits both go to backend[1]. |
| 10 | After backend[0] releases, next submit returns to it (lowest utilization). |
| 11 | `map([])` returns `[]`; no `backend.submit` calls. |
| 12 | `map(jobs)` returns results in input order even when releases reverse-ordered. |
| 13 | `map` fail-fast — middle job raises; `map` re-raises that specific exception. |
| 14 | `map` fail-fast cancels queued — cap=1, 4 jobs, job[0] raises; futures[2:] are `cancelled()`. |
| 15 | `map` fail-fast drains in-flight — 2 backends cap=1, both running; backend[0] raises; backend[1] reaches `release`; `map` raises backend[0]'s exception. |
| 16 | `close()` waits for in-flight — start job, call `close()` on a thread, assert it blocks; release, assert close returns. |
| 17 | `close()` is idempotent. |
| 18 | Context manager calls `close()` even when the `with` block raises. |
| 19 | Fuzz: 8 parallel submit/release cycles; final `in_flight` is 0; counter never negative or above cap. |
| 20 | Slot finally-release on backend exception — backend raises in `result()`; next submit to same backend succeeds. |

**`tests/core/test_pool.py` (existing, +3 tests)**

- SequentialPool `close()` is a no-op, idempotent, works as context manager.
- `_ListPool` test fixture gains `close()` for ABC parity.
- Pool-swap test extended: same stage produces byte-equal output with
  `SequentialPool(backend)` and `ConcurrentPool() + add(backend, max_in_flight=1)`.

**`tests/pipeline/test_generate_clip.py` (existing, +3 tests)**

- Unchained branch (t2v, N=3) with `ConcurrentPool(cap=3)`: assert all 3 jobs
  reach `backend.submit` before any `result` returns (parallel dispatch
  verified via Event-gated backend).
- Chained branch (i2v, N=3) with `ConcurrentPool(cap=3)`: assert each
  `backend.submit` is preceded by prior `release` (serial enforced by
  continuity loop).
- 1-job native branch: `len(jobs) == 1` skips `map`, uses
  `pool.submit(j).result()`.

**`tests/test_cli.py` (existing, +1 test)**

- CLI deploy → generate flow exits the `with ConcurrentPool` block;
  `executor.shutdown(wait=True)` invoked (asserted via spy).

### Backwards compatibility gate

All existing `SequentialPool` test sites stay green untouched. `pixi run test`
must show zero regressions across Linux + macOS CI matrix.

### Quality gate

`pixi run test && pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files` clean before merge.

## 9. File inventory

**Modified (4 files):**
- `src/kinoforge/core/interfaces.py` — add `close` abstractmethod +
  `__enter__`/`__exit__` to `BackendPool`.
- `src/kinoforge/core/pool.py` — add `SequentialPool.close` (no-op),
  `SequentialPool.add` `max_in_flight` kwarg, new `ConcurrentPool` class.
- `src/kinoforge/pipeline/generate_clip.py` — branch on `should_chain` and
  `len(jobs) > 1` → `pool.map(jobs)`.
- `src/kinoforge/cli.py` — swap `SequentialPool(backend)` for
  `with ConcurrentPool() as pool: pool.add(...)`.

**New (1 file):**
- `tests/core/test_concurrent_pool.py` — ~20 ACs above.

**Modified tests (3 files):**
- `tests/core/conftest.py` — add `BlockingFakeBackend` helper.
- `tests/core/test_pool.py` — SequentialPool `close` parity tests; `_ListPool`
  gains `close`.
- `tests/pipeline/test_generate_clip.py` — branch coverage for
  chained/unchained/1-job under `ConcurrentPool`.
- `tests/test_cli.py` — shutdown spy.

**Docs (2 files):**
- `README.md` — new "Concurrency" section documenting `max_in_flight` cfg.
- `PROGRESS.md` — Phase 17 entry mirroring Phase 14/15/16 style; update
  Single next action.

## 10. Out of scope / follow-ups

- **Multi-replica provisioning** (orchestrator-level multi-deploy). Required
  for pool to dispatch across multiple instances of one engine. Forward-
  compatible with Layer G; itself a future layer.
- **Cross-process / distributed pool.** Single-process only.
- **Late-binding dispatch** (Approach B in brainstorm). Selection happens at
  `submit()` time; under skewed job durations a queued job might miss a
  freeing backend. Revisit only if measured.
- **Observability** — `pool.stats()` returning per-backend `in_flight`/`cap`,
  queue depth, throughput. Out of scope; would be a follow-up if/when CLI
  status surfaces it.
- **`as_completed` map variant** (`map_unordered`). Would let callers process
  results as they arrive. No current consumer needs it.
