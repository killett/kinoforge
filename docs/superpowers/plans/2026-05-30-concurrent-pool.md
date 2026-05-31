# Layer G — Concurrent Backend Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `ConcurrentPool` as a drop-in sibling of `SequentialPool` behind the `BackendPool` ABC; branch `GenerateClipStage` so t2v non-chained fallback uses `pool.map`; swap `orchestrator.generate()`'s `SequentialPool` for `ConcurrentPool` with `max_in_flight` from `LifecycleConfig`. Defaults preserve today's behaviour; higher `max_in_flight` unlocks intra-request fan-out and multi-replica dispatch.

**Architecture:** One `concurrent.futures.ThreadPoolExecutor` per backend, sized to that backend's `max_in_flight` cap. `submit()` picks least-loaded backend by `in_flight / cap` under a single pool lock, returns a (possibly pending) `Future`. `map(jobs)` dispatches all jobs eagerly, resolves futures in input order, on first exception cancels queued and drains in-flight. `close()` calls `executor.shutdown(wait=True)` per backend; context-manager protocol on the ABC. `GenerateClipStage` keeps its serial loop for chained continuity and 1-job native; new branch routes non-chained N>1 through `pool.map`. Orchestrator wraps the stage in `with ConcurrentPool() as pool:` for deterministic shutdown.

**Tech Stack:** Python 3.x stdlib only — `concurrent.futures.ThreadPoolExecutor`, `threading.Lock`, `threading.Event` (test helper), `dataclasses`. No new runtime deps. pixi + pytest + mypy + ruff as configured.

**Spec:** `docs/superpowers/specs/2026-05-30-concurrent-pool-design.md`

---

## File Structure

**Modified (4 source files):**
- `src/kinoforge/core/interfaces.py` — `BackendPool` ABC gains `close()` abstractmethod and `__enter__`/`__exit__` concrete methods.
- `src/kinoforge/core/pool.py` — `SequentialPool` gains no-op `close()` and `max_in_flight` kwarg on `add()`. New `_Slot` dataclass + `ConcurrentPool` class added in the same module (sibling-by-name).
- `src/kinoforge/pipeline/generate_clip.py` — one new branch in `GenerateClipStage.run`: non-chained N>1 jobs use `pool.map(jobs)`; existing serial loop preserved for chained continuity and trivial 1-job native.
- `src/kinoforge/core/orchestrator.py` — `generate()` wraps stage construction + `.run()` + log + `return` inside `with ConcurrentPool() as pool: pool.add(backend, max_in_flight=cfg.lifecycle.max_in_flight)`.

**New (2 test files):**
- `tests/core/conftest.py` — `BlockingFakeBackend` test helper (Event-gated; deterministic completion ordering).
- `tests/core/test_concurrent_pool.py` — 20 ACs covering dispatch, distribution, cancellation, shutdown, and stress invariants.

**Modified tests (3 files):**
- `tests/core/test_pool.py` — `SequentialPool.close()` parity + `_ListPool` fixture gains `close()` for ABC parity.
- `tests/pipeline/test_generate_clip.py` — 3 tests covering the new branch (unchained map, chained serial, 1-job native).
- `tests/core/test_orchestrator.py` — 1 test asserting `ConcurrentPool.close()` runs at the end of `generate()`.

**Docs (2 files, in Task 6 only):**
- `README.md` — new "Concurrency" section documenting `max_in_flight` configuration.
- `PROGRESS.md` — Phase 17 entry mirroring Phase 14/15/16 style; `Single next action` updated to point at next layer.

---

## Quality Gate (run before every commit)

```bash
pixi run pre-commit run --files <changed-files>
```

## Full Gate (run at task completion + before merge)

```bash
pixi run test && pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files
```

---

## Task 1: BackendPool ABC `close()` + SequentialPool no-op + parity tests

**Goal:** Extend the `BackendPool` ABC with `close()` + context-manager protocol; give `SequentialPool` a no-op `close()` and a `max_in_flight` kwarg on `add()`; verify with parity tests that today's `SequentialPool` is unchanged in behaviour.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (BackendPool ABC, around line 354)
- Modify: `src/kinoforge/core/pool.py` (SequentialPool)
- Modify: `tests/core/test_pool.py` (add parity tests; update `_ListPool` fixture)

**Acceptance Criteria:**
- [ ] `BackendPool` declares `close(self) -> None` as `@abstractmethod`.
- [ ] `BackendPool` provides concrete `__enter__` returning `self` and `__exit__` calling `self.close()`.
- [ ] `SequentialPool.close()` returns `None`; calling twice raises nothing.
- [ ] `SequentialPool.add(backend)` AND `SequentialPool.add(backend, max_in_flight=4)` both work; the `max_in_flight` value is accepted but ignored.
- [ ] `_ListPool` test fixture in `tests/core/test_pool.py` declares `close(self) -> None` (no-op).
- [ ] `with SequentialPool(backend) as pool: ...` works; `pool.close()` is called on exit.
- [ ] All pre-existing tests in `tests/core/test_pool.py` pass unmodified.

**Verify:** `pixi run test -k test_pool -v` → all green, including new tests.

**Steps:**

- [ ] **Step 1: Write the failing tests in `tests/core/test_pool.py`**

Add at the bottom of `tests/core/test_pool.py`:

```python
# ---------------------------------------------------------------------------
# Layer G: close() + context-manager parity
# ---------------------------------------------------------------------------


def test_sequential_pool_close_is_noop():
    """SequentialPool.close() returns None and is safe to call on an empty pool."""
    pool = SequentialPool()
    result = pool.close()
    assert result is None


def test_sequential_pool_close_is_idempotent():
    """Calling close() twice is a no-op (no exception)."""
    probe = _profile()
    backend = FakeBackend(probe=probe)
    pool = SequentialPool(backend)
    pool.close()
    pool.close()  # must not raise


def test_sequential_pool_as_context_manager_calls_close():
    """`with SequentialPool() as pool:` exits cleanly and pool is closed."""
    probe = _profile()
    backend = FakeBackend(probe=probe)
    closed_called: list[bool] = []

    class _SpyPool(SequentialPool):
        def close(self) -> None:
            closed_called.append(True)
            super().close()

    with _SpyPool(backend) as pool:
        assert isinstance(pool, _SpyPool)
        assert pool.submit(_simple_job("hi")).result() is not None
    assert closed_called == [True]


def test_sequential_pool_add_accepts_max_in_flight_kwarg():
    """add(backend, max_in_flight=N) is accepted; SequentialPool ignores N."""
    probe = _profile()
    backend = FakeBackend(probe=probe)
    pool = SequentialPool()
    pool.add(backend, max_in_flight=4)  # must not raise
    assert len(pool._backends) == 1
    # Still uses _backends[0] regardless of cap; verify by submitting.
    result = pool.submit(_simple_job("after-add")).result()
    assert result is not None
```

Also update the existing `_ListPool` test fixture in the same file. Find:

```python
class _ListPool(BackendPool):
    """Minimal alternative BackendPool for pool-swap AC test."""
```

Add a `close` method to it (search for the class body and append):

```python
    def close(self) -> None:  # noqa: D102
        return None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pixi run test -k "test_sequential_pool_close or test_sequential_pool_as_context_manager or test_sequential_pool_add_accepts" -v
```

Expected: each new test fails with `AttributeError: 'SequentialPool' object has no attribute 'close'` (the first three) or `TypeError: add() got an unexpected keyword argument 'max_in_flight'` (the fourth).

The `_ListPool` change will surface as `TypeError: Can't instantiate abstract class _ListPool with abstract method close` once Step 3 lands the ABC change. Both gates fire; both prove the wiring.

- [ ] **Step 3: Add `close()` to `BackendPool` ABC**

In `src/kinoforge/core/interfaces.py`, replace the existing `BackendPool` block (around line 354) with:

```python
class BackendPool(ABC):
    """Dispatches jobs across one or more GenerationBackends.

    Implementations may call ``backend.submit`` / ``backend.result`` from
    multiple threads concurrently; backends MUST be thread-safe (no shared
    mutable state across calls).
    """

    @abstractmethod
    def add(self, backend: GenerationBackend) -> None: ...  # noqa: D102

    @abstractmethod
    def submit(self, job: GenerationJob) -> Future[Artifact]: ...  # noqa: D102

    @abstractmethod
    def map(self, jobs: list[GenerationJob]) -> list[Artifact]: ...  # noqa: D102

    @abstractmethod
    def close(self) -> None: ...  # noqa: D102

    def __enter__(self) -> "BackendPool":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
```

The `add` ABC signature stays one-arg. Subclasses are free to accept a kw-only `max_in_flight` (Liskov-safe: ABC callers get the default).

- [ ] **Step 4: Add `close()` + `max_in_flight` kwarg to `SequentialPool`**

In `src/kinoforge/core/pool.py`, update the `SequentialPool` class. Replace the existing `add` method:

```python
    def add(
        self,
        backend: GenerationBackend,
        *,
        max_in_flight: int = 1,
    ) -> None:
        """Append *backend* to the internal backend list.

        Args:
            backend: The :class:`~kinoforge.core.interfaces.GenerationBackend`
                to register.
            max_in_flight: Accepted for :class:`BackendPool` ABC parity with
                :class:`ConcurrentPool`; ignored by ``SequentialPool`` because
                only ``_backends[0]`` is ever used.
        """
        self._backends.append(backend)
```

And add `close()` at the end of the class (after `map`):

```python
    def close(self) -> None:
        """Release any resources held by this pool.

        ``SequentialPool`` owns no threads or open handles; this is a no-op
        provided for :class:`BackendPool` ABC parity with concurrent pools
        that must drain worker threads.
        """
        return None
```

- [ ] **Step 5: Run tests to verify all pass**

```bash
pixi run test -k test_pool -v
```

Expected: all tests in `test_pool.py` green, including the 4 new ones and the existing 11.

- [ ] **Step 6: Full gate on changed files**

```bash
pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/core/pool.py tests/core/test_pool.py
pixi run typecheck
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/pool.py tests/core/test_pool.py
git commit -m "feat(pool): add close() + context-manager to BackendPool ABC

SequentialPool gains no-op close() and accepts max_in_flight kwarg on add()
for ConcurrentPool parity. _ListPool test fixture also gains no-op close().
All existing tests pass unmodified.

Layer G Task 1 of 6."
```

---

## Task 2: `BlockingFakeBackend` helper + `ConcurrentPool` core (no `map` yet)

**Goal:** Add a deterministic Event-gated test backend, then build `ConcurrentPool` with init/add/_pick/_release/_run_one/submit/close. Defer `map` to Task 3. Cover ACs 1–10 and 16–20 from the spec (15 tests).

**Files:**
- Create: `tests/core/conftest.py`
- Modify: `src/kinoforge/core/pool.py` (add `_Slot` + `ConcurrentPool`)
- Create: `tests/core/test_concurrent_pool.py`

**Acceptance Criteria:**
- [ ] `tests/core/conftest.py` exports `BlockingFakeBackend` with `submit() -> str`, `result() -> Artifact` (blocks on Event with 5s safety timeout), `release(job_id)` (sets event).
- [ ] `ConcurrentPool()` constructible with no args.
- [ ] `pool.add(backend, max_in_flight=N)` registers backend with cap N (default 1).
- [ ] `pool.submit(job)` returns a `concurrent.futures.Future[Artifact]`.
- [ ] Empty pool `submit` raises `RuntimeError("ConcurrentPool has no registered backend")`.
- [ ] Closed pool `submit` raises `RuntimeError("pool closed")`.
- [ ] Cap N=1 serializes; cap N=4 allows 4 concurrent in-flight.
- [ ] Two backends with caps `[1,1]` idle → 2 submits go one-each, registration order.
- [ ] Two backends with caps `[1,4]` idle → 5 submits: backend[1] gets 4, backend[0] gets 1.
- [ ] Two backends caps `[2,2]`, backend[0] pre-occupied with 1 → next 2 submits both go to backend[1].
- [ ] `close()` blocks until in-flight jobs complete; idempotent; works as context manager.
- [ ] Slot counter never goes negative or above cap under 8 parallel submit/release cycles.

**Verify:** `pixi run test tests/core/test_concurrent_pool.py -v` → 15 tests pass; `pixi run test -k test_pool -v` → existing tests still green.

**Steps:**

- [ ] **Step 1: Create `tests/core/conftest.py` with `BlockingFakeBackend`**

```python
"""Shared fixtures for kinoforge.core tests.

Provides :class:`BlockingFakeBackend`: an Event-gated GenerationBackend whose
``result()`` blocks until the test explicitly calls ``release(job_id)``.
Use it to assert deterministic dispatch ordering in concurrent-pool tests
without sleeps.
"""

from __future__ import annotations

import threading

from kinoforge.core.interfaces import Artifact, GenerationBackend, GenerationJob


class BlockingFakeBackend(GenerationBackend):
    """GenerationBackend whose result() blocks on a per-job threading.Event.

    Tests control completion order by calling :meth:`release` with the job id
    returned by :meth:`submit`. A 5-second safety timeout in :meth:`result`
    prevents test hangs from masking ordering bugs.

    Attributes:
        submit_log: Job IDs in the order ``submit()`` was called. Read by tests
            to assert which backend received which job.
    """

    def __init__(self, name: str = "blk") -> None:
        """Initialise with empty state.

        Args:
            name: Optional name prefix used in generated job IDs; useful for
                disambiguating jobs across multiple backends in a test.
        """
        self._name = name
        self._gates: dict[str, threading.Event] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self._raise_for: set[str] = set()
        self.submit_log: list[str] = []

    def submit(self, job: GenerationJob) -> str:
        """Register a new job id, return it. Does not block.

        Args:
            job: The :class:`GenerationJob`; only used to advance the counter.

        Returns:
            A unique job id of the form ``"<name>-<counter>"``.
        """
        with self._lock:
            self._counter += 1
            jid = f"{self._name}-{self._counter}"
            self._gates[jid] = threading.Event()
            self.submit_log.append(jid)
        return jid

    def result(self, job_id: str) -> Artifact:
        """Block until ``release(job_id)`` is called; then return an Artifact.

        Args:
            job_id: The id returned by :meth:`submit`.

        Returns:
            An :class:`Artifact` whose filename and meta encode the job id.

        Raises:
            TimeoutError: The Event was not set within 5 seconds — typically
                indicates a test forgot to call :meth:`release`.
            RuntimeError: ``job_id`` was added to ``_raise_for`` via
                :meth:`fail_for` — used to test failure paths.
        """
        if not self._gates[job_id].wait(timeout=5.0):
            raise TimeoutError(f"{job_id} never released")
        if job_id in self._raise_for:
            raise RuntimeError(f"backend deliberately failed {job_id}")
        return Artifact(filename=f"{job_id}.mp4", meta={"jid": job_id})

    def release(self, job_id: str) -> None:
        """Unblock a pending :meth:`result` call.

        Args:
            job_id: The id returned by :meth:`submit`.
        """
        self._gates[job_id].set()

    def fail_for(self, job_id: str) -> None:
        """Mark *job_id* so that :meth:`result` raises ``RuntimeError`` on release.

        Args:
            job_id: The id returned by :meth:`submit`.
        """
        self._raise_for.add(job_id)
```

- [ ] **Step 2: Add the `_Slot` dataclass + `ConcurrentPool` skeleton in `src/kinoforge/core/pool.py`**

Append to the end of `src/kinoforge/core/pool.py`:

```python
import threading
from dataclasses import dataclass


@dataclass
class _Slot:
    """Per-backend bookkeeping inside :class:`ConcurrentPool`.

    Attributes:
        backend: The registered :class:`GenerationBackend`.
        executor: The dedicated :class:`concurrent.futures.ThreadPoolExecutor`,
            sized to ``cap``.
        cap: The per-backend ``max_in_flight`` cap.
        in_flight: Live count of jobs in this slot's executor (queued or
            running). Mutated only under :attr:`ConcurrentPool._lock`.
    """

    backend: GenerationBackend
    executor: concurrent.futures.ThreadPoolExecutor
    cap: int
    in_flight: int = 0


class ConcurrentPool(BackendPool):
    """Bounded-concurrency pool across one or more backend replicas.

    Each registered backend owns one
    :class:`concurrent.futures.ThreadPoolExecutor` sized to its
    ``max_in_flight`` cap.  :meth:`submit` picks the least-loaded backend by
    ``in_flight / cap`` utilization (ties broken by registration order) and
    forwards the call to that backend's executor.  Returned ``Future`` may
    be pending if all workers are busy; the executor's internal queue holds
    overflow — the caller never blocks.

    :meth:`map` dispatches every job eagerly, then resolves futures in input
    order.  On the first exception it cancels still-queued futures, drains
    in-flight ones (results discarded), and re-raises the captured exception.

    Use as a context manager for deterministic shutdown::

        with ConcurrentPool() as pool:
            pool.add(backend, max_in_flight=4)
            results = pool.map(jobs)

    Attributes:
        _slots: Per-backend state; appended to by :meth:`add`.
        _lock: Guards :attr:`_Slot.in_flight` and the ``_closed`` flag.
        _closed: Set by :meth:`close`; subsequent :meth:`submit` calls raise.
    """

    def __init__(self) -> None:
        """Construct an empty pool. Use :meth:`add` to register backends."""
        self._slots: list[_Slot] = []
        self._lock = threading.Lock()
        self._closed = False

    def add(
        self,
        backend: GenerationBackend,
        *,
        max_in_flight: int = 1,
    ) -> None:
        """Register *backend* with its per-replica concurrency cap.

        Args:
            backend: The :class:`GenerationBackend` to dispatch through.
            max_in_flight: Maximum concurrent in-flight calls to this
                backend.  Defaults to 1 (equivalent to sequential).  Must be
                a positive integer.

        Raises:
            ValueError: If ``max_in_flight`` is not a positive integer.
        """
        if max_in_flight < 1:
            raise ValueError(
                f"max_in_flight must be >= 1, got {max_in_flight}"
            )
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_in_flight,
            thread_name_prefix=f"kinoforge-pool-{len(self._slots)}",
        )
        self._slots.append(
            _Slot(backend=backend, executor=executor, cap=max_in_flight)
        )

    def submit(
        self, job: GenerationJob
    ) -> concurrent.futures.Future[Artifact]:
        """Dispatch *job* to the least-loaded backend.

        Args:
            job: The :class:`GenerationJob` to execute.

        Returns:
            A :class:`concurrent.futures.Future` resolving to the Artifact.
            May be pending when all workers are busy; the chosen executor's
            internal queue holds overflow.

        Raises:
            RuntimeError: The pool has been closed, OR no backend has been
                added yet.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("pool closed")
            if not self._slots:
                raise RuntimeError(
                    "ConcurrentPool has no registered backend"
                )
        slot = self._pick()
        return slot.executor.submit(self._run_one, slot, job)

    def close(self) -> None:
        """Shut down every per-backend executor, waiting for in-flight jobs.

        Two-phase: flip the ``_closed`` flag under the lock so new
        :meth:`submit` calls reject immediately; then call
        ``executor.shutdown(wait=True)`` on each slot outside the lock so
        long-running shutdowns do not serialise.

        Idempotent — second call is a no-op.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            slots = list(self._slots)
        for slot in slots:
            slot.executor.shutdown(wait=True)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _pick(self) -> _Slot:
        """Return the least-loaded slot, bumping its in_flight counter.

        Selection is by ``in_flight / cap`` ratio.  Ties broken by
        registration order via CPython ``min(iter, key=...)`` returning the
        first occurrence of the minimum key (documented behaviour).

        Returns:
            The chosen :class:`_Slot` with ``in_flight`` already incremented.
        """
        with self._lock:
            best = min(self._slots, key=lambda s: s.in_flight / s.cap)
            best.in_flight += 1
            return best

    def _release(self, slot: _Slot) -> None:
        """Decrement *slot*'s in_flight counter under the lock.

        Args:
            slot: The :class:`_Slot` whose worker has finished a job.
        """
        with self._lock:
            slot.in_flight -= 1

    def _run_one(self, slot: _Slot, job: GenerationJob) -> Artifact:
        """Run *job* through *slot*'s backend, ensuring counter release.

        Args:
            slot: The :class:`_Slot` chosen by :meth:`_pick`.
            job: The :class:`GenerationJob`.

        Returns:
            The :class:`Artifact` produced by the backend.

        Raises:
            Any exception raised by ``backend.submit`` or ``backend.result``
            is re-raised after the slot counter is released.
        """
        try:
            job_id = slot.backend.submit(job)
            return slot.backend.result(job_id)
        finally:
            self._release(slot)

    def map(self, jobs: list[GenerationJob]) -> list[Artifact]:
        """DEFERRED to Task 3. Raises NotImplementedError for now."""
        raise NotImplementedError("map() implemented in Layer G Task 3")
```

Notes for the implementer:
- `concurrent.futures` is already imported at the top of `pool.py` (used by SequentialPool). The new `threading` and `dataclass` imports go at the top of the file alongside existing imports (do not duplicate `from __future__ import annotations` or `concurrent.futures`).
- Mypy will need to see `Artifact`, `BackendPool`, `GenerationBackend`, `GenerationJob` — these are already imported at the top from `kinoforge.core.interfaces`.

- [ ] **Step 3: Create `tests/core/test_concurrent_pool.py` with the 15 ACs for this task**

```python
"""Tests for ConcurrentPool — Layer G core dispatch (no map; map covered in Task 3)."""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from kinoforge.core.interfaces import GenerationJob
from kinoforge.core.pool import ConcurrentPool

from .conftest import BlockingFakeBackend


def _job(prompt: str = "test") -> GenerationJob:
    """Build a minimal GenerationJob for tests that don't inspect content."""
    from kinoforge.core.interfaces import Segment

    return GenerationJob(
        capability_key="cap",
        engine_name="fake",
        spec={},
        params={},
        segments=[Segment(prompt=prompt, assets=[])],
    )


# --- AC 1: submit returns a Future that resolves to the Artifact ----------


def test_submit_returns_future_that_resolves_after_release():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=1)
        fut = pool.submit(_job())
        assert isinstance(fut, concurrent.futures.Future)
        # Must NOT be resolved until release.
        assert not fut.done()
        backend.release("blk-1")
        art = fut.result(timeout=2.0)
        assert art.meta["jid"] == "blk-1"


# --- AC 2: empty pool submit raises ---------------------------------------


def test_empty_pool_submit_raises_runtimeerror():
    pool = ConcurrentPool()
    try:
        with pytest.raises(
            RuntimeError, match="ConcurrentPool has no registered backend"
        ):
            pool.submit(_job())
    finally:
        pool.close()


# --- AC 3: closed pool submit raises --------------------------------------


def test_closed_pool_submit_raises_runtimeerror():
    backend = BlockingFakeBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)
    pool.close()
    with pytest.raises(RuntimeError, match="pool closed"):
        pool.submit(_job())


# --- AC 4: add(max_in_flight=N) honoured ----------------------------------


def test_add_max_in_flight_honoured_n_plus_one_jobs():
    """Submit N+1 jobs without release; exactly N reach backend.submit."""
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=2)
        futures = [pool.submit(_job(f"j{i}")) for i in range(3)]
        # Give workers a brief moment to pick up; assert exactly 2 in flight.
        for _ in range(50):
            if len(backend.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert len(backend.submit_log) == 2
        # Now release the first 2; the third should pick up.
        backend.release("blk-1")
        backend.release("blk-2")
        for _ in range(50):
            if len(backend.submit_log) >= 3:
                break
            time.sleep(0.01)
        assert len(backend.submit_log) == 3
        backend.release("blk-3")
        for f in futures:
            f.result(timeout=2.0)


# --- AC 5: cap=1 serialises -----------------------------------------------


def test_cap_one_serialises_jobs():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=1)
        futures = [pool.submit(_job(f"j{i}")) for i in range(3)]
        for _ in range(50):
            if len(backend.submit_log) >= 1:
                break
            time.sleep(0.01)
        assert backend.submit_log == ["blk-1"]
        backend.release("blk-1")
        for _ in range(50):
            if len(backend.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert backend.submit_log == ["blk-1", "blk-2"]
        backend.release("blk-2")
        backend.release("blk-3")
        for f in futures:
            f.result(timeout=2.0)


# --- AC 6: cap=4 allows 4 in-flight ---------------------------------------


def test_cap_four_allows_four_in_flight():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=4)
        futures = [pool.submit(_job(f"j{i}")) for i in range(4)]
        for _ in range(50):
            if len(backend.submit_log) >= 4:
                break
            time.sleep(0.01)
        assert len(backend.submit_log) == 4
        for jid in backend.submit_log:
            backend.release(jid)
        for f in futures:
            f.result(timeout=2.0)


# --- AC 7: two backends caps [1,1] distribute -----------------------------


def test_two_backends_cap_one_each_distribute_one_each():
    b1 = BlockingFakeBackend(name="A")
    b2 = BlockingFakeBackend(name="B")
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=1)
        pool.add(b2, max_in_flight=1)
        futures = [pool.submit(_job(f"j{i}")) for i in range(2)]
        for _ in range(50):
            if len(b1.submit_log) >= 1 and len(b2.submit_log) >= 1:
                break
            time.sleep(0.01)
        # Registration order tie-break: first job → b1, second → b2.
        assert b1.submit_log == ["A-1"]
        assert b2.submit_log == ["B-1"]
        b1.release("A-1")
        b2.release("B-1")
        for f in futures:
            f.result(timeout=2.0)


# --- AC 8: caps [1,4] weight-distribute -----------------------------------


def test_caps_one_and_four_distribute_by_utilization():
    b1 = BlockingFakeBackend(name="A")  # cap 1
    b4 = BlockingFakeBackend(name="B")  # cap 4
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=1)
        pool.add(b4, max_in_flight=4)
        # First submit: utilization 0/1 == 0/4 → tie → b1 (registration order).
        # Subsequent submits: b1 is at 1/1=1.0 vs b4 at 0/4=0.0 → b4 every time.
        futures = [pool.submit(_job(f"j{i}")) for i in range(5)]
        for _ in range(50):
            if len(b1.submit_log) + len(b4.submit_log) >= 5:
                break
            time.sleep(0.01)
        assert len(b1.submit_log) == 1, b1.submit_log
        assert len(b4.submit_log) == 4, b4.submit_log
        for jid in b1.submit_log:
            b1.release(jid)
        for jid in b4.submit_log:
            b4.release(jid)
        for f in futures:
            f.result(timeout=2.0)


# --- AC 9: pre-occupied backend skipped -----------------------------------


def test_pre_occupied_backend_skipped_in_favour_of_lower_utilization():
    """caps [2,2]; pre-occupy b1 with 1 in-flight; next 2 submits go to b2."""
    b1 = BlockingFakeBackend(name="A")
    b2 = BlockingFakeBackend(name="B")
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=2)
        pool.add(b2, max_in_flight=2)
        # Pre-occupy b1 with one in-flight job.
        f0 = pool.submit(_job("seed"))
        for _ in range(50):
            if len(b1.submit_log) >= 1:
                break
            time.sleep(0.01)
        assert b1.submit_log == ["A-1"]
        # Now b1 utilization is 1/2 = 0.5; b2 is 0/2 = 0.0 → next pick b2.
        f1 = pool.submit(_job("j1"))
        f2 = pool.submit(_job("j2"))
        for _ in range(50):
            if len(b2.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert len(b1.submit_log) == 1  # unchanged
        assert len(b2.submit_log) == 2
        b1.release("A-1")
        b2.release("B-1")
        b2.release("B-2")
        for f in [f0, f1, f2]:
            f.result(timeout=2.0)


# --- AC 10: after release, lowest-utilization backend wins again ---------


def test_after_release_lowest_utilization_wins():
    b1 = BlockingFakeBackend(name="A")
    b2 = BlockingFakeBackend(name="B")
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=1)
        pool.add(b2, max_in_flight=1)
        f0 = pool.submit(_job("j0"))  # → b1
        f1 = pool.submit(_job("j1"))  # → b2
        for _ in range(50):
            if b1.submit_log and b2.submit_log:
                break
            time.sleep(0.01)
        # Release b1's job; now b1 = 0/1, b2 = 1/1 → next submit goes to b1.
        b1.release("A-1")
        f0.result(timeout=2.0)
        f2 = pool.submit(_job("j2"))
        for _ in range(50):
            if len(b1.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert b1.submit_log == ["A-1", "A-2"]
        b1.release("A-2")
        b2.release("B-1")
        f1.result(timeout=2.0)
        f2.result(timeout=2.0)


# --- AC 16: close() waits for in-flight ----------------------------------


def test_close_blocks_until_inflight_completes():
    backend = BlockingFakeBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)
    fut = pool.submit(_job("j"))
    for _ in range(50):
        if backend.submit_log:
            break
        time.sleep(0.01)

    closed_event = threading.Event()

    def _close() -> None:
        pool.close()
        closed_event.set()

    closer = threading.Thread(target=_close, daemon=True)
    closer.start()
    # close() should NOT have returned yet — the job is still in-flight.
    time.sleep(0.05)
    assert not closed_event.is_set()
    backend.release("blk-1")
    # Now close() can complete.
    assert closed_event.wait(timeout=2.0)
    fut.result(timeout=2.0)


# --- AC 17: close() is idempotent -----------------------------------------


def test_close_is_idempotent():
    backend = BlockingFakeBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)
    pool.close()
    pool.close()  # must not raise


# --- AC 18: context-manager calls close even on exception ----------------


def test_context_manager_calls_close_on_exception():
    backend = BlockingFakeBackend()
    pool_ref: list[ConcurrentPool] = []

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with ConcurrentPool() as pool:
            pool_ref.append(pool)
            pool.add(backend, max_in_flight=1)
            raise _Boom("test")

    # Pool should be closed — submitting now must reject.
    with pytest.raises(RuntimeError, match="pool closed"):
        pool_ref[0].submit(_job())


# --- AC 19: stress test — invariants under parallel submit/release -------


def test_stress_in_flight_invariant_under_parallel_load():
    """8 threads each submit+release one job; in_flight must stay in [0, cap]."""
    backend = BlockingFakeBackend()
    observed_max = [0]
    observed_lock = threading.Lock()

    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=4)
        # Monkey-observe the slot.
        slot = pool._slots[0]

        def _check_invariant() -> None:
            with pool._lock:
                with observed_lock:
                    if slot.in_flight > observed_max[0]:
                        observed_max[0] = slot.in_flight
                assert 0 <= slot.in_flight <= slot.cap

        def _worker(i: int) -> None:
            fut = pool.submit(_job(f"j{i}"))
            _check_invariant()
            # Release after a tiny delay so contention happens.
            time.sleep(0.005)
            # Find the matching jid for this submission.
            with backend._lock:
                jid = backend.submit_log[-1] if backend.submit_log else None
            # We can't reliably know which jid was ours; release all on first
            # worker that observes 8 submits.
            for known_jid in list(backend._gates.keys()):
                backend.release(known_jid)
            fut.result(timeout=5.0)
            _check_invariant()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()

        # Final invariant: counter back to 0.
        assert slot.in_flight == 0
        # We exercised concurrency: observed at least 2 in-flight at peak.
        assert observed_max[0] >= 2, (
            f"stress test never observed concurrency; observed_max={observed_max[0]}"
        )


# --- AC 20: backend exception frees the slot -----------------------------


def test_backend_exception_releases_slot_in_finally():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=1)
        # Submit a job and mark it to fail on release.
        fut1 = pool.submit(_job("fail"))
        for _ in range(50):
            if backend.submit_log:
                break
            time.sleep(0.01)
        backend.fail_for("blk-1")
        backend.release("blk-1")
        with pytest.raises(RuntimeError, match="deliberately failed"):
            fut1.result(timeout=2.0)
        # Slot must be free again — next submit should succeed.
        fut2 = pool.submit(_job("ok"))
        for _ in range(50):
            if len(backend.submit_log) >= 2:
                break
            time.sleep(0.01)
        assert backend.submit_log == ["blk-1", "blk-2"]
        backend.release("blk-2")
        fut2.result(timeout=2.0)
```

A note on the polling pattern (`for _ in range(50): if cond: break; time.sleep(0.01)`): we cannot use Events alone here because the test asserts on the side-effect of a different thread (the executor worker calling `backend.submit`). A short bounded poll (max 500 ms) is the standard cpython test pattern for this case. Tighter coordination would require instrumenting `_run_one` with an Event seam — not worth the production-code complexity.

- [ ] **Step 4: Run the new tests; verify they fail (initially they pass-or-fail mixed because Step 2 already landed the class — re-run after Step 5)**

```bash
pixi run test tests/core/test_concurrent_pool.py -v
```

If Step 2 was committed before Step 3, these tests will run against a real (working) ConcurrentPool. The red-green discipline applies *within* the task: write a test, watch it fail (e.g. by commenting out the relevant production code temporarily), then implement. For a class-creation task like this, you may write the tests in batches matching method groups (submit/close first, then _pick, then exception path) and verify each batch fails before its implementation lands.

Pragmatic discipline: at minimum, run the full new test file once before Step 2's code lands, confirm `ImportError: cannot import name 'ConcurrentPool'`, then proceed with Steps 2+3.

- [ ] **Step 5: Run full test suite — confirm no regressions**

```bash
pixi run test -v 2>&1 | tail -30
```

Expected: 524 (existing) + 15 (new) = 539 tests, all green.

- [ ] **Step 6: Quality gate**

```bash
pixi run pre-commit run --files tests/core/conftest.py src/kinoforge/core/pool.py tests/core/test_concurrent_pool.py
pixi run typecheck
```

Expected: clean. `mypy` may flag `pool._slots` and `pool._lock` access in the stress test as accessing private members — keep them; this is a test of internal invariants and the pattern is precedented in existing pool tests.

- [ ] **Step 7: Commit**

```bash
git add tests/core/conftest.py src/kinoforge/core/pool.py tests/core/test_concurrent_pool.py
git commit -m "feat(pool): ConcurrentPool core dispatch (submit/close, no map yet)

- BlockingFakeBackend test helper (Event-gated, deterministic ordering)
- _Slot dataclass + ConcurrentPool with init/add/_pick/_release/_run_one/submit/close
- ThreadPoolExecutor per backend sized to max_in_flight
- Least-loaded-by-utilization dispatch; ties by registration order
- Context-manager protocol; idempotent close with executor.shutdown(wait=True)
- 15 ACs covering empty/closed pool, cap honour, distribution, shutdown, stress

map() raises NotImplementedError; arrives in Task 3.

Layer G Task 2 of 6."
```

---

## Task 3: `ConcurrentPool.map` with fail-fast cancellation

**Goal:** Implement `map(jobs) -> list[Artifact]` that dispatches all jobs eagerly, returns results in input order, and on first exception cancels queued futures, drains in-flight ones, and re-raises the captured exception. 5 ACs (11–15).

**Files:**
- Modify: `src/kinoforge/core/pool.py` (replace the `NotImplementedError` stub with the real `map`)
- Modify: `tests/core/test_concurrent_pool.py` (append 5 tests)

**Acceptance Criteria:**
- [ ] `map([])` returns `[]`; no `backend.submit` calls observed.
- [ ] `map(jobs)` returns results in input order even when backend releases are in reverse order.
- [ ] `map` raises the first exception encountered when a middle job fails.
- [ ] After a fail-fast, futures for queued (not-yet-started) jobs return `True` from `cancelled()`.
- [ ] After a fail-fast, in-flight jobs on other backends are allowed to drain (release reachable); their results are discarded; `map` re-raises the first exception.

**Verify:** `pixi run test tests/core/test_concurrent_pool.py -v` → 20 tests pass (15 from Task 2 + 5 new).

**Steps:**

- [ ] **Step 1: Write the failing tests in `tests/core/test_concurrent_pool.py`**

Append:

```python
# --- AC 11: map([]) returns [] ; no submits -------------------------------


def test_map_empty_list_returns_empty_and_makes_no_calls():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=2)
        results = pool.map([])
        assert results == []
        assert backend.submit_log == []


# --- AC 12: map preserves input order despite reverse release order ------


def test_map_preserves_input_order_with_reverse_release():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=3)
        jobs = [_job(f"j{i}") for i in range(3)]

        # Release in reverse on a separate thread once all 3 are picked up.
        def _releaser() -> None:
            for _ in range(50):
                if len(backend.submit_log) >= 3:
                    break
                time.sleep(0.01)
            for jid in reversed(list(backend._gates.keys())):
                backend.release(jid)

        threading.Thread(target=_releaser, daemon=True).start()
        results = pool.map(jobs)
        # Results must be in INPUT order (j0, j1, j2) despite reverse release.
        assert [r.meta["jid"] for r in results] == ["blk-1", "blk-2", "blk-3"]


# --- AC 13: map fail-fast raises first exception ------------------------


def test_map_failfast_raises_first_exception_from_middle_job():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=3)
        jobs = [_job(f"j{i}") for i in range(3)]

        def _releaser() -> None:
            for _ in range(50):
                if len(backend.submit_log) >= 3:
                    break
                time.sleep(0.01)
            # Fail the middle one; release in order so j0 returns first.
            backend.release("blk-1")
            backend.fail_for("blk-2")
            backend.release("blk-2")
            backend.release("blk-3")

        threading.Thread(target=_releaser, daemon=True).start()
        with pytest.raises(RuntimeError, match="deliberately failed blk-2"):
            pool.map(jobs)


# --- AC 14: cap=1, 4 jobs, job 0 raises → queued jobs cancelled ---------


def test_map_failfast_cancels_queued_futures():
    backend = BlockingFakeBackend()
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=1)
        jobs = [_job(f"j{i}") for i in range(4)]

        def _releaser() -> None:
            for _ in range(50):
                if backend.submit_log:
                    break
                time.sleep(0.01)
            backend.fail_for("blk-1")
            backend.release("blk-1")

        threading.Thread(target=_releaser, daemon=True).start()
        with pytest.raises(RuntimeError, match="deliberately failed blk-1"):
            pool.map(jobs)
        # Job 1 must have been picked up before cancel reached it; jobs 2 and
        # 3 should still be queued and thus cancellable.  However, since map
        # raised, we only need to verify that at most 1 additional job
        # reached backend.submit (job 1 is racy; jobs 2 and 3 must NOT).
        assert len(backend.submit_log) <= 2, (
            f"too many jobs reached backend after fail-fast: {backend.submit_log}"
        )


# --- AC 15: map fail-fast drains in-flight on other backend ---------------


def test_map_failfast_drains_inflight_on_other_backend():
    """2 backends cap=1, both running; backend[0]'s job fails; backend[1]'s
    job is released after and completes; map re-raises backend[0]'s exception."""
    b1 = BlockingFakeBackend(name="A")
    b2 = BlockingFakeBackend(name="B")
    with ConcurrentPool() as pool:
        pool.add(b1, max_in_flight=1)
        pool.add(b2, max_in_flight=1)
        jobs = [_job(f"j{i}") for i in range(2)]
        # Spawn the releaser on a thread so map() can be called synchronously.
        b2_released = threading.Event()

        def _releaser() -> None:
            for _ in range(50):
                if b1.submit_log and b2.submit_log:
                    break
                time.sleep(0.01)
            b1.fail_for("A-1")
            b1.release("A-1")
            # Wait a moment so map captures the exception before b2 completes.
            time.sleep(0.05)
            b2.release("B-1")
            b2_released.set()

        threading.Thread(target=_releaser, daemon=True).start()
        with pytest.raises(RuntimeError, match="deliberately failed A-1"):
            pool.map(jobs)
        # Verify b2 was indeed allowed to drain (releaser fired).
        assert b2_released.wait(timeout=2.0)
```

- [ ] **Step 2: Run new tests; verify they fail with `NotImplementedError`**

```bash
pixi run test tests/core/test_concurrent_pool.py::test_map_empty_list_returns_empty_and_makes_no_calls -v
```

Expected: `NotImplementedError: map() implemented in Layer G Task 3`.

- [ ] **Step 3: Implement `map` in `src/kinoforge/core/pool.py`**

Replace the stub `map` method on `ConcurrentPool`:

```python
    def map(self, jobs: list[GenerationJob]) -> list[Artifact]:
        """Dispatch every job and return results in input order; fail-fast.

        All jobs are submitted eagerly; results are awaited in input order.
        On first exception, every still-queued future is cancelled (in-flight
        jobs continue and their outcomes are discarded), and the first
        exception is re-raised.

        Args:
            jobs: Ordered list of :class:`GenerationJob`.  Empty list returns
                ``[]`` with no backend calls.

        Returns:
            List of :class:`Artifact` in the same order as *jobs*.

        Raises:
            BaseException: The first exception raised by any backend; queued
                futures are cancelled before the re-raise.  In-flight futures
                are allowed to complete; their results (success or further
                exceptions) are silently discarded.
        """
        if not jobs:
            return []
        futures = [self.submit(j) for j in jobs]
        results: list[Artifact | None] = [None] * len(jobs)
        first_exc: BaseException | None = None
        for i, fut in enumerate(futures):
            if first_exc is not None:
                fut.cancel()
                continue
            try:
                results[i] = fut.result()
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                first_exc = exc
        if first_exc is not None:
            raise first_exc
        # All slots filled; cast is safe because no None remains.
        return [r for r in results if r is not None]
```

Mypy note: the final return uses a comprehension instead of `cast(list[Artifact], results)` to avoid the typing-extensions `cast` import — the filter cannot drop any element because we only reach this branch when `first_exc is None`, meaning every index was assigned.

- [ ] **Step 4: Run new tests; verify they pass**

```bash
pixi run test tests/core/test_concurrent_pool.py -v
```

Expected: 20 tests pass.

- [ ] **Step 5: Full suite check**

```bash
pixi run test -v 2>&1 | tail -10
```

Expected: 539 + 5 = 544 tests, all green.

- [ ] **Step 6: Quality gate + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/pool.py tests/core/test_concurrent_pool.py
pixi run typecheck
git add src/kinoforge/core/pool.py tests/core/test_concurrent_pool.py
git commit -m "feat(pool): ConcurrentPool.map with fail-fast cancellation

- Eager dispatch of all jobs; results resolved in input order
- On first exception: cancel queued futures, drain in-flight (results
  discarded), re-raise first exception
- map([]) returns [] with no backend calls
- 5 new ACs cover empty list, order preservation under reverse release,
  middle-job failure, queued cancellation, cross-backend in-flight drain

Layer G Task 3 of 6."
```

---

## Task 4: `GenerateClipStage` branch for non-chained `pool.map`

**Goal:** Add one branch to `GenerateClipStage.run`: when `not should_chain and len(jobs) > 1`, use `pool.map(jobs)`. Chained continuity and 1-job native paths preserved verbatim.

**Files:**
- Modify: `src/kinoforge/pipeline/generate_clip.py` (around the existing for-loop)
- Modify: `tests/pipeline/test_generate_clip.py` (3 new tests)

**Acceptance Criteria:**
- [ ] Non-chained branch (e.g. t2v, N=3): with a `ConcurrentPool(cap=3)`, all 3 jobs reach `backend.submit` before any `result` returns (parallel verified via `BlockingFakeBackend.submit_log` size).
- [ ] Chained branch (i2v, N=3): with a `ConcurrentPool(cap=3)`, each `backend.submit` is preceded by the prior `release` (serial enforced).
- [ ] 1-job native: `len(jobs) == 1` skips `map`, uses `pool.submit(j).result()` directly (verified by ensuring map is NOT called via a spy pool or by observing only one submit_log entry).
- [ ] All existing `test_generate_clip.py` tests pass unmodified.

**Verify:** `pixi run test tests/pipeline/test_generate_clip.py -v` → all green.

**Steps:**

- [ ] **Step 1: Write the failing tests in `tests/pipeline/test_generate_clip.py`**

Append to the file (after the existing tests). The test imports `BlockingFakeBackend` from `tests.core.conftest` — this works because pytest discovers conftest fixtures at the package root and helper classes are importable directly:

```python
# ---------------------------------------------------------------------------
# Layer G: ConcurrentPool branch coverage
# ---------------------------------------------------------------------------

import threading
import time

from kinoforge.core.pool import ConcurrentPool
from tests.core.conftest import BlockingFakeBackend


def _stage_with_concurrent_pool(
    backend: BlockingFakeBackend,
    *,
    cap: int,
    mode: str,
    profile: ModelProfile,
    store: ArtifactStore,
) -> tuple[GenerateClipStage, ConcurrentPool]:
    """Build a stage backed by ConcurrentPool(cap) with a non-chaining engine.

    The engine is FakeEngine so extract_last_frame is concrete; the backend is
    the supplied BlockingFakeBackend, registered into a fresh ConcurrentPool.
    """
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=cap)
    fake_engine = FakeEngine(probe=profile)
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="r-concurrent",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=fake_engine,
    )
    return stage, pool


def test_unchained_branch_uses_pool_map_parallel_dispatch(tmp_path):
    """t2v 3-segment fallback: all 3 jobs reach backend.submit before any release."""
    probe = _profile()
    store = LocalArtifactStore(root=tmp_path)
    backend = BlockingFakeBackend()
    stage, pool = _stage_with_concurrent_pool(
        backend, cap=3, mode="t2v", profile=probe, store=store
    )

    segments = [Segment(prompt=f"p{i}", assets=[]) for i in range(3)]
    request = GenerationRequest(
        prompt="x", mode="t2v", kind="video", assets=[]
    )

    # Spawn a releaser thread that waits until all 3 reach backend.submit,
    # then releases them.  If the stage were serial, only 1 would arrive
    # before any release — the releaser's wait would time out.
    releaser_saw_three = threading.Event()

    def _releaser() -> None:
        for _ in range(100):
            if len(backend.submit_log) >= 3:
                releaser_saw_three.set()
                break
            time.sleep(0.01)
        for jid in list(backend._gates.keys()):
            backend.release(jid)

    threading.Thread(target=_releaser, daemon=True).start()
    try:
        result = stage.run(request, segments_override=segments)
        assert result is not None
    finally:
        pool.close()
    assert releaser_saw_three.is_set(), (
        "stage did not dispatch all 3 jobs in parallel — branch missed"
    )


def test_chained_branch_remains_serial_under_concurrent_pool(tmp_path):
    """i2v 3-segment chain: each backend.submit preceded by prior release.

    We assert serialness by observing that at any moment, submit_log has
    at most one MORE entry than the number of completed releases.
    """
    probe = _profile()  # standard probe; i2v chaining triggers via
    # MODE_ROLE_REQUIREMENTS["i2v"] = {"init_image"} in interfaces.py,
    # not via profile config. Mirror the existing chained-3-segment
    # test at tests/pipeline/test_generate_clip.py:190.
    store = LocalArtifactStore(root=tmp_path)
    backend = BlockingFakeBackend()
    stage, pool = _stage_with_concurrent_pool(
        backend, cap=3, mode="i2v", profile=probe, store=store
    )

    # Seed asset for seg 0.
    seed_uri = store.put_bytes("r-concurrent", "seed.png", b"seed").uri
    seed_asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="seed.png", uri=seed_uri),
    )
    segments = [
        Segment(prompt="p0", assets=[seed_asset]),
        Segment(prompt="p1", assets=[]),
        Segment(prompt="p2", assets=[]),
    ]
    request = GenerationRequest(
        prompt="x", mode="i2v", kind="video", assets=[seed_asset]
    )

    released_count = [0]

    def _releaser() -> None:
        # Watchdog: every 10ms, release one more job if there's a pending one.
        for _ in range(200):
            if len(backend.submit_log) > released_count[0]:
                # Assert serial: only 1 ahead of released.
                assert len(backend.submit_log) - released_count[0] == 1, (
                    f"chained branch ran in parallel: "
                    f"submitted={backend.submit_log}, released={released_count[0]}"
                )
                # Release the most recent.
                with backend._lock:
                    last_jid = backend.submit_log[-1]
                backend.release(last_jid)
                released_count[0] += 1
                if released_count[0] >= 3:
                    break
            time.sleep(0.01)

    threading.Thread(target=_releaser, daemon=True).start()
    try:
        result = stage.run(request, segments_override=segments)
        assert result is not None
    finally:
        pool.close()
    assert released_count[0] == 3


def test_one_job_native_skips_map_uses_submit(tmp_path):
    """Native single-job path uses pool.submit().result(), not pool.map()."""
    probe = _profile()
    store = LocalArtifactStore(root=tmp_path)
    backend = BlockingFakeBackend()
    stage, pool = _stage_with_concurrent_pool(
        backend, cap=3, mode="t2v", profile=probe, store=store
    )

    # Single segment — strategy.decide produces 1 job for native or
    # 1-segment fallback; either way len(jobs) == 1.
    segments = [Segment(prompt="solo", assets=[])]
    request = GenerationRequest(
        prompt="x", mode="t2v", kind="video", assets=[]
    )

    def _releaser() -> None:
        for _ in range(50):
            if backend.submit_log:
                break
            time.sleep(0.01)
        for jid in list(backend._gates.keys()):
            backend.release(jid)

    threading.Thread(target=_releaser, daemon=True).start()
    try:
        result = stage.run(request, segments_override=segments)
        assert result is not None
    finally:
        pool.close()
    # Only one job should have been submitted.
    assert len(backend.submit_log) == 1
```

Implementer note: `_profile()` is already defined at `tests/pipeline/test_generate_clip.py:36` and accepts a `supports_native_extension` kwarg. i2v chaining is triggered by `MODE_ROLE_REQUIREMENTS["i2v"]` containing `init_image` (in `src/kinoforge/core/interfaces.py`), independent of profile config. The existing chained-continuity test at line 190 of the same file is the template for the chained test here.

- [ ] **Step 2: Run new tests; verify they fail**

```bash
pixi run test tests/pipeline/test_generate_clip.py -k "unchained_branch or chained_branch_remains or one_job_native_skips" -v
```

Expected: the first test fails because the stage is currently serial — releaser times out at 1 second never seeing 3 in flight. The third test may pass (only 1 job submitted is invariant regardless of branch) but the assertion against `submit_log == 1` is the meaningful one. The chained test passes coincidentally on the current code (always serial); it locks in the regression check for after the branch lands.

- [ ] **Step 3: Add the branch in `src/kinoforge/pipeline/generate_clip.py`**

In the `run` method, find the existing for-loop (around line 102) that starts:

```python
        should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())
        results: list[Artifact] = []
        for i, job in enumerate(jobs):
```

Wrap with the new branch. Replace from the `should_chain` line through the `results.append(art)` line with:

```python
        should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())
        if not should_chain and len(jobs) > 1:
            # Layer G: t2v non-chained fallback fans out via pool.map.
            # Chained continuity (i2v) and trivial 1-job paths take the
            # serial loop below.
            results = list(self.pool.map(jobs))
        else:
            results = []
            for i, job in enumerate(jobs):
                if i > 0 and should_chain:
                    tail_bytes = self.engine.extract_last_frame(results[-1])
                    tail_name = f"seg-{i - 1}-tail.png"
                    stored = self.store.put_bytes(self.run_id, tail_name, tail_bytes)
                    tail_artifact = replace(stored, filename=tail_name)
                    tail_asset = ConditioningAsset(
                        kind="image",
                        role="init_image",
                        ref=tail_artifact,
                    )
                    job = inject_tail_frame(job, tail_asset)
                    self.engine.validate_spec(job)
                art = self.pool.submit(job).result()
                results.append(art)
        last = results[-1]
```

Comments on `else` branch comments and inner comments: the existing comments documenting the chained continuity rationale and the Layer F `validate_spec` call MUST be preserved verbatim — they explain WHY the loop pattern exists. Above paste includes the code but elides those comments for brevity; the actual edit must keep them.

To make the edit mechanical, the simplest implementation pattern is:

1. Open `src/kinoforge/pipeline/generate_clip.py`.
2. Find the line `should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())`.
3. Immediately after it, insert two new lines:
   ```python
           if not should_chain and len(jobs) > 1:
               results = list(self.pool.map(jobs))
           else:
   ```
4. Indent the existing `results: list[Artifact] = []` line and the entire following for-loop by one level (4 spaces).
5. Leave the comments verbatim in place; they stay attached to their original code.

- [ ] **Step 4: Run all stage tests; verify the 3 new tests pass + existing tests still pass**

```bash
pixi run test tests/pipeline/test_generate_clip.py -v 2>&1 | tail -20
```

Expected: all existing stage tests + 3 new tests green. If `_profile_with_init_image` was added wrong, you'll see an i2v test fail — fix the helper to mirror the existing i2v test setup.

- [ ] **Step 5: Full suite check**

```bash
pixi run test -v 2>&1 | tail -10
```

Expected: previously 544; now 544 + 3 = 547 tests, all green.

- [ ] **Step 6: Quality gate + commit**

```bash
pixi run pre-commit run --files src/kinoforge/pipeline/generate_clip.py tests/pipeline/test_generate_clip.py
pixi run typecheck
git add src/kinoforge/pipeline/generate_clip.py tests/pipeline/test_generate_clip.py
git commit -m "feat(stage): branch GenerateClipStage on should_chain for pool.map

Non-chained N>1 jobs (t2v fallback) dispatch via pool.map for parallel
fan-out under ConcurrentPool. Chained continuity (i2v) and 1-job native
paths keep the existing serial loop verbatim — required by data
dependency (i2v) or trivial by job count (native).

3 new tests cover unchained parallel dispatch, chained serial enforcement,
and 1-job path bypassing map.

Layer G Task 4 of 6."
```

---

## Task 5: Orchestrator swap — `SequentialPool` → `ConcurrentPool` with shutdown

**Goal:** In `core/orchestrator.py:476`, replace `pool = SequentialPool(backend)` and the stage construction + run + return that follow it with a `with ConcurrentPool() as pool: pool.add(backend, max_in_flight=cfg.lifecycle.max_in_flight); ...`. Add one test that observes shutdown via a public flag check.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (lines ~474–489 — the Step-9 block)
- Modify: `tests/core/test_orchestrator.py` (1 new test)

**Acceptance Criteria:**
- [ ] `orchestrator.generate(...)` constructs a `ConcurrentPool`, adds the backend with `max_in_flight=cfg.lifecycle.max_in_flight`, and returns the artifact from within the `with` block.
- [ ] After `generate()` returns, the pool's `_closed` flag is `True` (verified via spy or via observing that a second `submit()` raises `RuntimeError("pool closed")`).
- [ ] All existing `test_orchestrator.py` tests pass unmodified.
- [ ] `SequentialPool` import in `orchestrator.py` is removed (no longer used in the module).

**Verify:** `pixi run test tests/core/test_orchestrator.py -v` → all green; `pixi run lint` → clean (no unused import).

**Steps:**

- [ ] **Step 1: Write the failing test in `tests/core/test_orchestrator.py`**

Find an existing test that exercises the full happy path of `orchestrator.generate(...)` — there should be several (commits `0f3d0f6` Task 16). Use its setup pattern (fake provider, fake engine, fake backend, FakeClock, etc.) and add this new test at an appropriate location:

```python
def test_generate_closes_concurrent_pool_after_run():
    """orchestrator.generate() closes its ConcurrentPool on return.

    Spies on ConcurrentPool by monkey-patching its close() to record the call.
    """
    from kinoforge.core import pool as pool_mod

    close_calls: list[bool] = []
    original_close = pool_mod.ConcurrentPool.close

    def _spy_close(self: pool_mod.ConcurrentPool) -> None:
        close_calls.append(True)
        original_close(self)

    pool_mod.ConcurrentPool.close = _spy_close  # type: ignore[method-assign]
    try:
        # Reuse the existing happy-path fixture setup; this is illustrative.
        # The implementer should match the orchestrator-test patterns already
        # present in the file (see test_generate_happy_path or similar).
        result = _run_generate_happy_path()  # existing helper or inline setup
        assert result is not None
        assert close_calls == [True], (
            f"orchestrator did not close the pool exactly once; "
            f"close_calls={close_calls}"
        )
    finally:
        pool_mod.ConcurrentPool.close = original_close  # type: ignore[method-assign]
```

If `_run_generate_happy_path` does not exist as a shared helper, inline the minimal generate() invocation by copying the setup from the closest existing happy-path test in the file.

- [ ] **Step 2: Run new test; verify it fails**

```bash
pixi run test tests/core/test_orchestrator.py::test_generate_closes_concurrent_pool_after_run -v
```

Expected: fails with `AssertionError: orchestrator did not close the pool exactly once; close_calls=[]` — orchestrator still uses `SequentialPool`, never instantiates `ConcurrentPool`.

- [ ] **Step 3: Update `src/kinoforge/core/orchestrator.py`**

a) Find line 37 (the SequentialPool import):

```python
from kinoforge.core.pool import SequentialPool
```

Replace with:

```python
from kinoforge.core.pool import ConcurrentPool
```

b) Find the Step-9 block (around lines 474–489), currently:

```python
    # ------------------------------------------------------------------
    # Step 9 — run the pipeline stage
    # ------------------------------------------------------------------
    pool = SequentialPool(backend)
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds=accepted_kinds,
        base_params={},
        base_spec={},
        engine=resolved_engine,
    )
    artifact = stage.run(request, segments_override=prompt_segments)
    _log.info("generate completed — artifact uri=%r", artifact.uri)
    return artifact
```

Replace with:

```python
    # ------------------------------------------------------------------
    # Step 9 — run the pipeline stage
    # ------------------------------------------------------------------
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=cfg.lifecycle.max_in_flight)
        stage = GenerateClipStage(
            profile=profile,
            pool=pool,
            store=store,
            run_id=run_id,
            accepted_kinds=accepted_kinds,
            base_params={},
            base_spec={},
            engine=resolved_engine,
        )
        artifact = stage.run(request, segments_override=prompt_segments)
        _log.info("generate completed — artifact uri=%r", artifact.uri)
        return artifact
```

c) Also find the docstring around line 291 mentioning `SequentialPool(backend)` and update it to mention `ConcurrentPool(...).add(backend, max_in_flight=cfg.lifecycle.max_in_flight)` so the prose-spec inside the orchestrator stays accurate.

- [ ] **Step 4: Run the new test + all orchestrator tests; verify all pass**

```bash
pixi run test tests/core/test_orchestrator.py -v 2>&1 | tail -20
```

Expected: all green, including the new spy test and the 12 existing AC tests from commit `0f3d0f6`.

- [ ] **Step 5: Full suite check**

```bash
pixi run test -v 2>&1 | tail -10
```

Expected: 547 + 1 = 548 tests, all green.

- [ ] **Step 6: Quality gate + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
pixi run typecheck
pixi run lint
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
git commit -m "feat(orchestrator): use ConcurrentPool with max_in_flight from config

generate() now wraps stage construction + run + return inside
\`with ConcurrentPool() as pool: pool.add(backend, max_in_flight=cfg.lifecycle.max_in_flight)\`.

Default max_in_flight=1 from LifecycleConfig preserves today's behaviour
(one job at a time). Higher caps unlock t2v non-chained fan-out via
GenerateClipStage's pool.map branch from Task 4.

SequentialPool no longer used by orchestrator; import removed.
Pool shutdown is deterministic via context-manager exit.

Layer G Task 5 of 6."
```

---

## Task 6: Docs + final quality gate + merge

**Goal:** Document the new `max_in_flight` configuration in `README.md`; record Phase 17 (Layer G) progress in `PROGRESS.md`; run the full quality gate; merge via `--no-ff` following the established layer-merge pattern.

**Files:**
- Modify: `README.md` (new "Concurrency" subsection)
- Modify: `PROGRESS.md` (Phase 17 entry mirroring Phase 14/15/16 style; `Single next action` updated)

**Acceptance Criteria:**
- [ ] `README.md` contains a "Concurrency" subsection (under whatever top-level section is appropriate — Configuration or Architecture) explaining `LifecycleConfig.max_in_flight`, with a brief yaml example showing `lifecycle: {max_in_flight: 4}`.
- [ ] `PROGRESS.md` has a new `### Phase 17 — concurrent backend scheduler (deferred layer G, GitHub issue #3)` block with one-line entries per task (commits), and the `Single next action` section now points to the next layer (or "no immediate next layer; backlog clean").
- [ ] `pixi run test && pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files` clean.
- [ ] Merge commit on `main` via `git merge --no-ff build/layer-g` with a substantive body referencing the layer name, the 6 task commits, and `Closes #3`.

**Verify:** `pixi run test && pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files` → clean.

**Steps:**

- [ ] **Step 1: Add "Concurrency" subsection to `README.md`**

Find an appropriate top-level section (likely "Configuration" or similar — check the existing README structure first). Add a subsection like:

```markdown
### Concurrency

kinoforge dispatches generation jobs through a `ConcurrentPool` that respects
each backend's `max_in_flight` cap. By default, `max_in_flight = 1` — one job
at a time, identical in observable behaviour to the original SequentialPool.

To enable intra-request parallelism (only meaningful for t2v fallback with
N > 1 segments — see below) or to drive a backend that handles concurrent
prompts (e.g. one Comfy server serving 4 requests in parallel), raise the cap
in your engine yaml:

```yaml
engine:
  comfyui:
    lifecycle:
      max_in_flight: 4
```

**What this unlocks:**

- **t2v fallback with N > 1 segments** (intra-request fan-out): segments are
  independent by design (text-to-video has no cross-segment frame
  conditioning), so they run in parallel up to the cap.
- **i2v continuity chains stay serial**: segment N consumes segment N-1's
  tail frame; data dependency forces sequential execution regardless of cap.
- **Multiple concurrent `generate()` calls** sharing one pool (multi-tenant /
  batch render): each call's stage uses the shared pool's capacity.

The pool is constructed inside `orchestrator.generate()` and closed via
`with` on return — in-flight jobs are drained before the pool tears down,
preventing torn HTTP connections or orphaned remote jobs.
```

- [ ] **Step 2: Add Phase 17 entry to `PROGRESS.md`**

Open `PROGRESS.md` and find the "Post-MVP" section. After the Phase 16 block, append:

```markdown
### Phase 17 — concurrent backend scheduler (post-MVP Layer G, GitHub issue #3)
- [x] Task 1: BackendPool ABC `close()` + SequentialPool no-op + parity tests — commit `<SHA-1>`
- [x] Task 2: BlockingFakeBackend helper + ConcurrentPool core dispatch (submit/close, no map) + 15 ACs — commit `<SHA-2>`
- [x] Task 3: ConcurrentPool.map with fail-fast cancellation + 5 ACs — commit `<SHA-3>`
- [x] Task 4: GenerateClipStage branch on `should_chain` for `pool.map` + 3 ACs — commit `<SHA-4>`
- [x] Task 5: orchestrator.generate() swaps `SequentialPool` for `ConcurrentPool` (context-manager) + shutdown spy — commit `<SHA-5>`
- [x] Task 6: README Concurrency section + PROGRESS Phase 17 + merge — commit `<SHA-6>` (merge commit `<MERGE-SHA>`, closes #3)
```

Replace `<SHA-N>` with the actual short SHAs from `git log --oneline -10` after each task's commit lands. The final entry's SHAs are filled in just before / during this task's commit.

Update the `## Single next action` section. Current text points to Layer G; replace with one of:
- **If user has identified the next layer:** rewrite to describe that layer and the next entry point (e.g. "Layer H — orchestrator multi-deploy. ...")
- **Else:** "Layer G (concurrent backend scheduler) complete. **Next: choose from open GitHub issues #2 (audio sync), #4 (keyframe stage), #7 (cross-process discovery lock), #8 (HF bare-repo listing), #9 (aria2c fast-path), or open new work.**"

Also: update the "GitHub issues status" table to mark issue #3 as `CLOSED (Layer G)`.

Also: append to "Established patterns for layer development" if Layer G introduces a new repeatable pattern (e.g. "Thread-safe pool dispatch via ThreadPoolExecutor-per-replica with utilization-weighted selection — new template for any future N-way scheduler in the codebase"). Skip if no new pattern emerged worth recording.

Also: update the test count in the file (528 → 548) wherever it appears (Phase 16 callout, "Single next action").

- [ ] **Step 3: Run the full gate**

```bash
pixi run test && pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files
```

Expected: all clean. If anything fails, fix it before commit.

- [ ] **Step 4: Commit docs**

```bash
git add README.md PROGRESS.md
git commit -m "docs: README Concurrency section + PROGRESS Phase 17 (Layer G)

Documents max_in_flight configuration for ConcurrentPool; records all 6
Layer G task commits; updates Single next action and GitHub issues table
(#3 → CLOSED).

Layer G Task 6 of 6."
```

- [ ] **Step 5: Merge to main via `--no-ff`**

Assumes the work has been on a `build/layer-g` branch following past-layer convention. If it has been on `main`, this step is a no-op.

```bash
git checkout main
git merge --no-ff build/layer-g -m "$(cat <<'EOF'
Merge branch 'build/layer-g': concurrent backend scheduler (Layer G)

Drop-in ConcurrentPool behind the existing BackendPool ABC:
- One concurrent.futures.ThreadPoolExecutor per backend, sized to
  max_in_flight cap; least-loaded-by-utilization dispatch.
- map(jobs) fail-fast: cancel queued, drain in-flight, raise first.
- Explicit close() + context-manager protocol on the ABC; SequentialPool
  gains no-op close for parity.
- GenerateClipStage branches on should_chain so t2v non-chained
  fallback exploits pool.map; chained continuity and 1-job native
  paths preserved verbatim.
- orchestrator.generate() wraps stage in `with ConcurrentPool() as pool:`
  for deterministic shutdown; max_in_flight sourced from LifecycleConfig.

20-AC test suite for ConcurrentPool, 4 SequentialPool parity tests,
3 stage branch tests, 1 orchestrator shutdown spy. 528 → 548 tests.

Per-task commits:
  Task 1 — <SHA>: feat(pool): add close() + context-manager to BackendPool ABC
  Task 2 — <SHA>: feat(pool): ConcurrentPool core dispatch (submit/close, no map yet)
  Task 3 — <SHA>: feat(pool): ConcurrentPool.map with fail-fast cancellation
  Task 4 — <SHA>: feat(stage): branch GenerateClipStage on should_chain for pool.map
  Task 5 — <SHA>: feat(orchestrator): use ConcurrentPool with max_in_flight from config
  Task 6 — <SHA>: docs: README Concurrency section + PROGRESS Phase 17 (Layer G)

Closes #3.
EOF
)"
```

Replace `<SHA>` placeholders with the actual task-commit short SHAs from `git log --oneline -7` (run before composing the merge message).

- [ ] **Step 6: Backfill PROGRESS SHAs if any are still placeholders**

If Phase 17 entries in `PROGRESS.md` still carry literal `<SHA-N>` placeholders after the merge, fix and commit:

```bash
# Edit PROGRESS.md, replace each <SHA-N> with the real short SHA.
git add PROGRESS.md
git commit -m "docs(progress): backfill Layer G task commit SHAs"
```

---

## Verification Summary

Run after Task 6 merges:

```bash
pixi run test 2>&1 | tail -3
git log --oneline -10
```

Expected:
- 548 tests passing (528 pre-layer + 4 SequentialPool parity + 15 ConcurrentPool core + 5 ConcurrentPool.map + 3 stage branch + 1 orchestrator shutdown).
- Top of `git log` shows the merge commit followed by the 6 task commits in reverse order.
- GitHub issue #3 closed automatically by the merge trailer.
