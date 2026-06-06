# Layer L-T4 — batch streaming logs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a streaming-event seam to `core/batch.py` and ship three CLI formatters (`human`, `jsonl`, `none`) consuming it so `kinoforge batch` emits per-entry start/finish lines mid-run.

**Architecture:** Callback hook in `core/` (foundation-first; preserves the core-print-free invariant). CLI formatters consume the hook. Internal `threading.Lock` serializes user callbacks. Frozen `BatchEvent` pydantic model. Backward-compatible: `on_event=None` is byte-identical to today.

**Tech Stack:** Python 3.11+, pydantic v2, `concurrent.futures.ThreadPoolExecutor`, stdlib `threading.Lock`. Tests via pytest. Pixi for tooling.

**Spec:** `docs/superpowers/specs/2026-06-05-layer-l-t4-batch-streaming-logs-design.md`

**Pre-flight invariant:** Tree clean. Suite at 1297 passed / 8 skipped before Task 1. Verify with `pixi run test -q` before starting.

---

## Task 1: Extract batch dataclasses to `core/batch_models.py`

**Goal:** Move `BatchEntry`, `BatchManifest`, `BatchOutcome`, `BatchResult` out of `core/batch.py` into a new `core/batch_models.py` so `core/batch_events.py` (Task 2) can import `BatchEntry` without creating a cycle. Pure refactor; no semantic change.

**Files:**
- Create: `src/kinoforge/core/batch_models.py`
- Modify: `src/kinoforge/core/batch.py` (remove model defs at lines 74–259; add re-exports at top)

**Acceptance Criteria:**
- [ ] `from kinoforge.core.batch import BatchEntry, BatchManifest, BatchOutcome, BatchResult` still works for every existing import site.
- [ ] `from kinoforge.core.batch_models import BatchEntry, BatchManifest, BatchOutcome, BatchResult` also works.
- [ ] All 1297 existing tests pass without modification.
- [ ] `pixi run lint typecheck` is clean.
- [ ] Invariant scan `pixi run test tests/test_core_invariant.py -v` passes.

**Verify:** `pixi run test -q` → 1297 passed / 8 skipped.

**Steps:**

- [ ] **Step 1: Find all import sites for the four moving names.**

```bash
rg -n 'from kinoforge\.core\.batch import|from kinoforge\.core import batch' --type py
```

Expected: a handful of test files + `cli/_commands.py` + maybe `core/orchestrator.py`. Note the full list; you don't need to touch any of them because `batch.py` will re-export.

- [ ] **Step 2: Create `core/batch_models.py` by moving the model defs.**

Copy lines 74–259 of `core/batch.py` (the four model classes + the `to_dict` method on `BatchResult`) into a new file. Add the minimal imports needed.

```python
# src/kinoforge/core/batch_models.py
"""Batch manifest + outcome dataclasses (extracted from core/batch.py).

This module exists so that core/batch_events.py can import BatchEntry
without a cycle with core/batch.py.  Keep it dependency-light: pydantic
+ stdlib only; no kinoforge.core.* imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BatchEntry(BaseModel):
    """One entry in a batch manifest.

    Attributes:
        prompt: Inline prompt text. Mutually exclusive with prompt_file.
        prompt_file: Path to a text file (resolved relative to the
            manifest's parent dir). Mutually exclusive with prompt.
            After load_manifest runs, this is always None — the loader
            collapses prompt_file into prompt.
        mode: Generation mode (t2v / i2v / flf2v). Required per entry —
            no inherited default. An explicit per-entry choice avoids
            silent mode mixups.
        run_id: Sub-namespace under the batch_id for this entry's
            artifacts. None means "let the loader auto-index by position".
            After load_manifest runs, this is always set.
        params: Engine-neutral param overrides shallow-merged onto
            cfg.params (entry wins per key).
        spec: Engine-interpreted spec overrides shallow-merged onto
            cfg.spec (entry wins per key).
        assets: List of asset dicts forwarded into GenerationRequest.assets.
        keyframe: Per-entry keyframe overrides shallow-merged onto
            cfg.keyframe (entry wins per key).  Only the fields present
            in this dict are overridden; omitted fields fall back to
            cfg.keyframe defaults.  Ignored when cfg.keyframe is None.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = None
    prompt_file: str | None = None
    mode: str
    run_id: str | None = None
    params: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    assets: list[dict[str, Any]] | None = None
    keyframe: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one_prompt_source(self) -> BatchEntry:
        """Reject entries that set both or neither of prompt / prompt_file.

        Returns:
            ``self`` unchanged when the rule is satisfied.

        Raises:
            ValueError: When neither or both of prompt / prompt_file are set.
        """
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError("entry must set exactly one of `prompt` / `prompt_file`")
        return self


class BatchManifest(BaseModel):
    """A validated batch manifest.

    Attributes:
        entries: One or more BatchEntry objects, in submission order.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[BatchEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_run_ids(self) -> BatchManifest:
        """Reject manifests whose explicit run_ids collide.

        When ANY entry sets ``run_id`` explicitly, ALL run_ids (including
        the auto-derived ones added by load_manifest later) must be
        unique.  When NONE set ``run_id``, the loader auto-indexes
        ``"0"``, ``"1"``, ... — collision-free by construction.

        Returns:
            ``self`` unchanged when run_ids are unique.

        Raises:
            ValueError: When the explicit run_id set contains duplicates.
        """
        ids = [e.run_id for e in self.entries if e.run_id is not None]
        if ids and len(set(ids)) != len(ids):
            dupes = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"duplicate run_id in manifest: {dupes}")
        return self


@dataclass
class BatchOutcome:
    """The result of one entry in a batch run.

    Attributes:
        run_id: The entry's run_id (always set after load_manifest).
        status: One of "ok" / "fail" / "aborted" / "interrupted".
        duration_s: Seconds the entry was in-flight (None for "aborted").
        uri: Persisted artifact URI on "ok"; None otherwise.
        error: Stringified exception on "fail" / "interrupted"; None otherwise.
    """

    run_id: str
    status: Literal["ok", "fail", "aborted", "interrupted"]
    duration_s: float | None = None
    uri: str | None = None
    error: str | None = None


@dataclass
class BatchResult:
    """Summary of one batch_generate() call.

    Attributes:
        batch_id: The batch namespace ID (e.g. "batch-20260531-093052").
        started_at: ISO local-tz timestamp string.
        finished_at: ISO local-tz timestamp string.
        outcomes: Ordered by entry submission order (NOT completion order).
    """

    batch_id: str
    started_at: str
    finished_at: str
    outcomes: list[BatchOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly shape written to ``_batch_summary.json``.

        Returns:
            A dict with ``batch_id``, ``started_at``, ``finished_at``,
            and ``entries`` (the outcomes with ``None`` fields omitted).
        """
        return {
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "entries": [
                {k: v for k, v in vars(o).items() if v is not None}
                for o in self.outcomes
            ],
        }
```

- [ ] **Step 3: In `core/batch.py`, delete lines 74–259 (the four model classes) and re-export.**

Top of `core/batch.py`, after the existing module docstring and imports block (after the existing `from kinoforge.core.errors import ...` block, line ~39), add:

```python
from kinoforge.core.batch_models import (
    BatchEntry,
    BatchManifest,
    BatchOutcome,
    BatchResult,
)
```

Then delete the four class definitions (lines 74–259). Drop the now-unused imports from the top of `core/batch.py`: `from dataclasses import dataclass, field` (keep `replace` — used elsewhere), `from typing import Literal`, `BaseModel, ConfigDict, Field, model_validator` from pydantic. Run `pixi run lint` to verify.

- [ ] **Step 4: Run full suite + lint + typecheck.**

```bash
pixi run test -q && pixi run lint && pixi run typecheck
```

Expected: 1297 passed / 8 skipped; lint clean; mypy clean.

- [ ] **Step 5: Verify invariant scan.**

```bash
pixi run test tests/test_core_invariant.py -v
```

Expected: all pass. `core/batch_models.py` is in `core/` and has no `kinoforge.{providers,sources,engines}` imports.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/batch.py src/kinoforge/core/batch_models.py
git commit -m "refactor(batch): extract dataclasses to core/batch_models.py

Move BatchEntry, BatchManifest, BatchOutcome, BatchResult out of
core/batch.py into a new core/batch_models.py.  core/batch.py
re-exports the four names so every existing import site keeps
working.  Prep for Layer L-T4: lets core/batch_events.py import
BatchEntry without a cycle.

Pure refactor; no semantic change.  1297 tests pass unchanged."
```

---

## Task 2: Create `core/batch_events.py` (BatchEvent + `_LockedEmitter`)

**Goal:** Ship the streaming-event data contract and the thread-safe emitter helper.

**Files:**
- Create: `src/kinoforge/core/batch_events.py`
- Create: `tests/core/test_batch_events.py`

**Acceptance Criteria:**
- [ ] `BatchEvent` is a frozen pydantic model; attribute mutation raises `pydantic.ValidationError`.
- [ ] `BatchEvent.model_dump_json()` → `BatchEvent.model_validate_json()` round-trips identity-equal.
- [ ] `_LockedEmitter` serializes the user callback: under a 4-worker `threading.Barrier(4)` stress, the recorded `(t_enter, t_exit)` windows for two callbacks never overlap.
- [ ] `_LockedEmitter(on_event=None)` accepts events silently (no exception, no print) and still tracks `_started_idxs` on `entry_start`.
- [ ] `_LockedEmitter.has_started(idx)` reflects which entries have already emitted `entry_start`.
- [ ] Field nullability: constructing `entry_start` with `status="ok"` raises; constructing `entry_finish` with `entry=<obj>` raises; per-status `uri`/`error` rules from spec §3.3 hold.

**Verify:** `pixi run test tests/core/test_batch_events.py -v` → 6 tests pass.

**Steps:**

- [ ] **Step 1: Write failing tests first (`tests/core/test_batch_events.py`).**

```python
"""Tests for core/batch_events.py — BatchEvent + _LockedEmitter."""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest
from pydantic import ValidationError

from kinoforge.core.batch_events import BatchEvent, _LockedEmitter
from kinoforge.core.batch_models import BatchEntry


def _make_entry() -> BatchEntry:
    return BatchEntry(prompt="hi", mode="t2v")


def _now() -> datetime:
    return datetime.now()


def test_event_frozen() -> None:
    """Bug: mutating a streamed event leaks state to other subscribers.

    A frozen model is the contract that prevents an enterprising
    formatter from rewriting an event's status after the fact.
    """
    ev = BatchEvent(
        kind="entry_start",
        batch_id="b",
        idx=0,
        run_id="0",
        ts=_now(),
        entry=_make_entry(),
    )
    with pytest.raises(ValidationError):
        ev.idx = 99  # type: ignore[misc]


def test_event_json_roundtrip() -> None:
    """Bug: JSONL formatter line drift between python versions.

    Locks the on-wire shape: dump → validate is identity-equal,
    so JSONL consumers can rely on the schema.
    """
    ev = BatchEvent(
        kind="entry_finish",
        batch_id="b",
        idx=2,
        run_id="alpha",
        ts=_now(),
        status="ok",
        duration_s=1.5,
        uri="local://x/y",
    )
    blob = ev.model_dump_json()
    restored = BatchEvent.model_validate_json(blob)
    assert restored == ev


def test_locked_emitter_serializes() -> None:
    """Bug: two workers emitting concurrently produce interleaved stdout.

    Records (t_enter, t_exit) per callback call.  Asserts no two
    windows overlap — i.e. the user callback is called sequentially.
    """
    barrier = threading.Barrier(4)
    log: list[tuple[float, float]] = []
    log_lock = threading.Lock()

    def cb(_event: BatchEvent) -> None:
        t0 = time.monotonic()
        time.sleep(0.02)  # widen the window to surface real overlaps
        t1 = time.monotonic()
        with log_lock:
            log.append((t0, t1))

    emit = _LockedEmitter(cb)

    def worker(i: int) -> None:
        barrier.wait()
        emit(
            BatchEvent(
                kind="entry_start",
                batch_id="b",
                idx=i,
                run_id=str(i),
                ts=_now(),
                entry=_make_entry(),
            )
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_sorted = sorted(log, key=lambda w: w[0])
    for (_, e1), (s2, _) in zip(log_sorted, log_sorted[1:], strict=True):
        assert e1 <= s2, f"overlap: window ending {e1} vs next start {s2}"


def test_locked_emitter_none_callback_noop() -> None:
    """Bug: None callback path raises instead of being a silent no-op.

    The opt-out path (on_event=None) must accept events silently AND
    still track _started_idxs so _mark_remaining_after_fatal can use
    has_started() regardless of whether a user callback was supplied.
    """
    emit = _LockedEmitter(None)
    emit(
        BatchEvent(
            kind="entry_start",
            batch_id="b",
            idx=7,
            run_id="7",
            ts=_now(),
            entry=_make_entry(),
        )
    )
    assert emit.has_started(7) is True
    assert emit.has_started(99) is False


def test_started_idxs_tracking() -> None:
    """Bug: has_started misreports because the set isn't mutated.

    Every entry_start adds; entry_finish does NOT add.
    """
    seen: list[int] = []
    emit = _LockedEmitter(lambda ev: seen.append(ev.idx))

    emit(BatchEvent(kind="entry_start", batch_id="b", idx=1, run_id="1",
                    ts=_now(), entry=_make_entry()))
    emit(BatchEvent(kind="entry_finish", batch_id="b", idx=1, run_id="1",
                    ts=_now(), status="ok", duration_s=0.0, uri="local://x"))

    assert emit.has_started(1) is True
    assert emit.has_started(2) is False
    assert seen == [1, 1]  # callback fires for BOTH kinds


def test_field_nullability_rules() -> None:
    """Bug: silent acceptance of malformed events corrupts JSONL output.

    Per spec §3.3:
      * entry_start MUST carry entry; MUST NOT carry status/duration_s/uri/error
      * entry_finish MUST carry status + duration_s; MUST NOT carry entry
      * uri set iff status=="ok"; error set iff status in {fail, interrupted, aborted}
    """
    # entry_start with status set -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_start", batch_id="b", idx=0, run_id="0",
            ts=_now(), entry=_make_entry(), status="ok",
        )
    # entry_finish with entry set -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish", batch_id="b", idx=0, run_id="0",
            ts=_now(), entry=_make_entry(),
            status="ok", duration_s=0.0, uri="local://x",
        )
    # entry_finish status="ok" without uri -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish", batch_id="b", idx=0, run_id="0",
            ts=_now(), status="ok", duration_s=0.0,
        )
    # entry_finish status="fail" without error -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish", batch_id="b", idx=0, run_id="0",
            ts=_now(), status="fail", duration_s=0.5,
        )
```

- [ ] **Step 2: Run the new test file — confirm it fails (red).**

```bash
pixi run test tests/core/test_batch_events.py -v
```

Expected: `ModuleNotFoundError: No module named 'kinoforge.core.batch_events'` or the equivalent collection error.

- [ ] **Step 3: Implement `core/batch_events.py`.**

```python
# src/kinoforge/core/batch_events.py
"""Streaming event contract for `core/batch.py:batch_generate`.

Adds an opt-in callback hook so external consumers (CLI, log
aggregators, TUIs) can observe per-entry progress mid-run without
core/batch.py touching stdout.  Matches the existing "core stays
print-free" invariant enforced by tests/test_core_invariant.py.

Threading: callbacks are serialized via an internal Lock so multi-
line output never interleaves.  Mirrors the stdlib logging.Handler
serialization pattern.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from kinoforge.core.batch_models import BatchEntry

EventKind = Literal["entry_start", "entry_finish"]
EntryStatus = Literal["ok", "fail", "interrupted", "aborted"]


class BatchEvent(BaseModel):
    """One streaming event emitted by `batch_generate` via `on_event`.

    Attributes:
        kind: ``"entry_start"`` or ``"entry_finish"``.
        batch_id: The batch's top-level namespace ID.
        idx: 0-based index into ``manifest.entries``.
        run_id: ``entry.run_id`` or ``str(idx)`` if the entry didn't
            set one explicitly.
        ts: Local-tz timestamp (project rule — no UTC).
        entry: The full :class:`BatchEntry` for ``entry_start`` events;
            ``None`` on ``entry_finish``.
        status: Terminal status on ``entry_finish``;
            ``None`` on ``entry_start``.
        duration_s: Stage wall-clock cost in seconds on ``entry_finish``;
            ``None`` on ``entry_start``.
        uri: Persisted artifact URI on successful ``entry_finish``;
            ``None`` otherwise.
        error: Stringified exception on failed / interrupted / aborted
            ``entry_finish``; ``None`` otherwise.
    """

    model_config = ConfigDict(frozen=True)

    kind: EventKind
    batch_id: str
    idx: int
    run_id: str
    ts: datetime

    entry: BatchEntry | None = None

    status: EntryStatus | None = None
    duration_s: float | None = None
    uri: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _enforce_nullability(self) -> BatchEvent:
        """Enforce the kind → field-set contract documented in the spec."""
        if self.kind == "entry_start":
            if self.entry is None:
                raise ValueError("entry_start requires `entry`")
            if any(
                v is not None
                for v in (self.status, self.duration_s, self.uri, self.error)
            ):
                raise ValueError(
                    "entry_start must not carry status/duration_s/uri/error"
                )
        else:  # entry_finish
            if self.entry is not None:
                raise ValueError("entry_finish must not carry `entry`")
            if self.status is None:
                raise ValueError("entry_finish requires `status`")
            if self.duration_s is None:
                raise ValueError("entry_finish requires `duration_s`")
            if self.status == "ok":
                if self.uri is None:
                    raise ValueError("entry_finish status='ok' requires `uri`")
                if self.error is not None:
                    raise ValueError(
                        "entry_finish status='ok' must not carry `error`"
                    )
            else:  # fail / interrupted / aborted
                if self.error is None:
                    raise ValueError(
                        f"entry_finish status={self.status!r} requires `error`"
                    )
                if self.uri is not None:
                    raise ValueError(
                        f"entry_finish status={self.status!r} must not carry `uri`"
                    )
        return self


BatchEventCallback = Callable[[BatchEvent], None]


class _LockedEmitter:
    """Serializes a user callback under a single Lock.

    When ``on_event`` is ``None`` the callback is skipped, but the
    ``_started_idxs`` book-keeping is still maintained on ``entry_start``
    so `_mark_remaining_after_fatal` can use :meth:`has_started`
    regardless of whether a streaming consumer was supplied.

    The lock is acquired once per call, covering both the
    ``_started_idxs`` mutation AND the user callback invocation, so a
    user callback that writes multi-line strings to stdout cannot
    interleave with another worker's callback.
    """

    def __init__(self, on_event: BatchEventCallback | None) -> None:
        self._cb = on_event
        self._lock = threading.Lock()
        self._started_idxs: set[int] = set()

    def __call__(self, event: BatchEvent) -> None:
        with self._lock:
            if event.kind == "entry_start":
                self._started_idxs.add(event.idx)
            if self._cb is not None:
                self._cb(event)

    def has_started(self, idx: int) -> bool:
        with self._lock:
            return idx in self._started_idxs
```

- [ ] **Step 4: Run the new test file — confirm green.**

```bash
pixi run test tests/core/test_batch_events.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Lint + typecheck.**

```bash
pixi run lint && pixi run typecheck
```

- [ ] **Step 6: Full suite — confirm no regression.**

```bash
pixi run test -q
```

Expected: 1303 passed / 8 skipped (1297 + 6 new).

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/core/batch_events.py tests/core/test_batch_events.py
git commit -m "feat(batch): BatchEvent contract + _LockedEmitter (Layer L-T4 T2)

Frozen pydantic model with field-nullability validators per spec §3.3.
Thread-safe emitter wraps user callback in single Lock; on_event=None
is a silent no-op that still tracks _started_idxs for the batch-fatal
sweep path.

6 ACs covering frozen contract, JSON round-trip, lock serialization,
none-callback silence, started_idxs tracking, nullability rules."
```

---

## Task 3: Thread `on_event` through `batch_generate` (5 emission sites)

**Goal:** Wire the emitter into `batch_generate` so streaming events fire at the build-fail, worker-start, per-entry-ok, per-entry-fail, and batch-fatal sites. Backward-compatible: `on_event=None` is byte-identical.

**Files:**
- Modify: `src/kinoforge/core/batch.py` (signature, `_run_with_clock`, `_mark_remaining_after_fatal`, 5 emission sites)
- Modify: `tests/core/test_batch.py` (add 5 ACs covering the new behaviour)

**Acceptance Criteria:**
- [ ] **AC1 (regression)** Existing happy-path test (e.g. `test_batch_generate_happy_path`) still passes byte-identical with `on_event=None` default.
- [ ] **AC2 (invariant)** Across 4 exit paths (clean / per-entry fail / build fail / batch-fatal), recorded events satisfy `start_count == finish_count == len(manifest.entries)`.
- [ ] **AC3 (ordering)** For every emitted idx, the `entry_start` event index in the recorded log is < the `entry_finish` event index for the same idx.
- [ ] **AC4 (lock stress)** Under 4 concurrent workers + a recording callback that sleeps 10ms, no two recorded windows overlap.
- [ ] **AC5 (build fail)** A build-time fail (raises inside `_build_stage_for_entry`) emits `entry_start` + `entry_finish(status="fail", duration_s=0.0, error=...)` back-to-back from the main thread.
- [ ] **AC6 (batch fatal)** A `BudgetExceeded` mid-batch produces: `entry_finish(status="interrupted")` for the raising entry; `entry_start` + `entry_finish(status="aborted", duration_s=0.0)` back-to-back for never-started entries; `entry_finish(status="interrupted")` for in-flight entries that already emitted `entry_start`.

**Verify:** `pixi run test tests/core/test_batch.py -v -k "on_event or stream"` → 5 new tests pass; full file passes; full suite green.

**Steps:**

- [ ] **Step 1: Write the 5 failing tests in `tests/core/test_batch.py`.**

Add at the bottom of the file (after the existing test functions). The tests use the existing `FakeProvider`/`FakeEngine`/`FakeBackend` fixtures already present in `tests/core/test_batch.py` — re-use whatever the file calls them, do not re-stub.

```python
# -------------------------------------------------------------------
# Layer L-T4 — streaming event ACs
# -------------------------------------------------------------------

from kinoforge.core.batch_events import BatchEvent  # noqa: E402


def _record_events() -> tuple[list[BatchEvent], object]:
    """Return (log, callback) — callback appends every event into log."""
    log: list[BatchEvent] = []

    def cb(ev: BatchEvent) -> None:
        log.append(ev)

    return log, cb


def test_on_event_none_default_behaviour_unchanged(
    happy_batch_fixture,  # use whatever the existing happy-path fixture is named
) -> None:
    """AC1: on_event=None is byte-identical to today's behaviour.

    Bug: a refactor regression silently changes outcome ordering or
    _batch_summary.json contents when no callback is supplied.
    """
    cfg, manifest, store = happy_batch_fixture
    result = batch_generate(
        cfg, manifest, store=store, batch_id="b1",
        # provider / engine / etc filled in from the existing fixture
    )
    # Use whatever assertion the existing happy-path test uses.
    # This is a smoke that the path that doesn't pass on_event still
    # produces a clean BatchResult.
    assert result.batch_id == "b1"
    assert len(result.outcomes) == len(manifest.entries)


def test_streaming_invariant_clean_path(happy_batch_fixture) -> None:
    """AC2 (clean): start_count == finish_count == len(entries)."""
    cfg, manifest, store = happy_batch_fixture
    log, cb = _record_events()
    batch_generate(
        cfg, manifest, store=store, batch_id="b2", on_event=cb,
    )
    starts = [e for e in log if e.kind == "entry_start"]
    finishes = [e for e in log if e.kind == "entry_finish"]
    assert len(starts) == len(manifest.entries)
    assert len(finishes) == len(manifest.entries)


def test_streaming_ordering_start_before_finish(happy_batch_fixture) -> None:
    """AC3: for every idx, entry_start precedes entry_finish."""
    cfg, manifest, store = happy_batch_fixture
    log, cb = _record_events()
    batch_generate(
        cfg, manifest, store=store, batch_id="b3", on_event=cb,
    )
    for idx in range(len(manifest.entries)):
        start_pos = next(
            i for i, e in enumerate(log)
            if e.kind == "entry_start" and e.idx == idx
        )
        finish_pos = next(
            i for i, e in enumerate(log)
            if e.kind == "entry_finish" and e.idx == idx
        )
        assert start_pos < finish_pos, (
            f"idx={idx}: start@{start_pos} not before finish@{finish_pos}"
        )


def test_streaming_lock_serializes_workers(concurrent_batch_fixture) -> None:
    """AC4: under 4 workers, recorded windows do not overlap.

    `concurrent_batch_fixture` should give a 4-entry manifest with
    cfg.lifecycle().max_in_flight=4 so workers actually run in parallel.
    """
    import time

    cfg, manifest, store = concurrent_batch_fixture
    windows: list[tuple[float, float]] = []
    win_lock = threading.Lock()

    def cb(_ev: BatchEvent) -> None:
        t0 = time.monotonic()
        time.sleep(0.01)
        t1 = time.monotonic()
        with win_lock:
            windows.append((t0, t1))

    batch_generate(
        cfg, manifest, store=store, batch_id="b4",
        on_event=cb, concurrent=4,
    )
    windows.sort(key=lambda w: w[0])
    for (_, e1), (s2, _) in zip(windows, windows[1:], strict=True):
        assert e1 <= s2, f"overlap: window ending {e1} vs next start {s2}"


def test_streaming_build_fail_emits_both(build_fail_batch_fixture) -> None:
    """AC5: build-time fail emits start + finish back-to-back.

    `build_fail_batch_fixture` is a manifest with one entry whose mode
    is unsupported (so _build_stage_for_entry raises ValidationError).
    """
    cfg, manifest, store = build_fail_batch_fixture
    log, cb = _record_events()
    batch_generate(
        cfg, manifest, store=store, batch_id="b5", on_event=cb,
    )
    bad_idx = 0  # adjust if fixture uses a different index
    starts = [e for e in log if e.kind == "entry_start" and e.idx == bad_idx]
    finishes = [e for e in log if e.kind == "entry_finish" and e.idx == bad_idx]
    assert len(starts) == 1
    assert len(finishes) == 1
    finish = finishes[0]
    assert finish.status == "fail"
    assert finish.duration_s == 0.0
    assert finish.error is not None


def test_streaming_batch_fatal_emits_interrupted_and_aborted(
    budget_exceeded_batch_fixture,
) -> None:
    """AC6: BudgetExceeded mid-batch labels in-flight 'interrupted',
    never-started 'aborted' (with their entry_start + entry_finish
    pair).

    `budget_exceeded_batch_fixture` has the second entry raise
    BudgetExceeded; the first is already running, the third has not
    started.
    """
    cfg, manifest, store = budget_exceeded_batch_fixture
    log, cb = _record_events()
    with pytest.raises(BudgetExceeded):
        batch_generate(
            cfg, manifest, store=store, batch_id="b6",
            on_event=cb, concurrent=1,  # force sequential so order is deterministic
        )
    # Every entry got both a start and a finish
    for idx in range(len(manifest.entries)):
        starts = [e for e in log if e.kind == "entry_start" and e.idx == idx]
        finishes = [e for e in log if e.kind == "entry_finish" and e.idx == idx]
        assert len(starts) == 1
        assert len(finishes) == 1
    # The raising entry is interrupted; later entries are aborted
    statuses = {e.idx: e.status for e in log if e.kind == "entry_finish"}
    assert statuses[1] == "interrupted"
    assert statuses[2] == "aborted"
```

Adjust fixture names to match what already exists in `tests/core/test_batch.py` — if the file calls them `fake_batch_setup` etc., use that. If a needed fixture doesn't exist yet (e.g. `build_fail_batch_fixture`), add it to the file via a `@pytest.fixture` definition that constructs a one-entry manifest with `mode="not-a-real-mode"` so `_build_stage_for_entry` raises a `ValidationError`. Same for the `BudgetExceeded` fixture — wire a fake engine whose `submit` raises `BudgetExceeded` on the 2nd entry.

- [ ] **Step 2: Run the new tests — confirm they fail (red).**

```bash
pixi run test tests/core/test_batch.py -v -k "on_event or stream"
```

Expected: tests fail because `batch_generate` doesn't accept `on_event`. The 6th test will also fail because the build-fail / batch-fatal paths don't emit anything.

- [ ] **Step 3: Modify `src/kinoforge/core/batch.py` to accept `on_event` and wire the emitter.**

3a. Add the imports near the top (after the existing `from kinoforge.core.batch_models import ...` block added in Task 1):

```python
from kinoforge.core.batch_events import (
    BatchEvent,
    BatchEventCallback,
    _LockedEmitter,
)
```

3b. Add the `on_event` parameter to `batch_generate` (currently kwargs end with `tags`):

```python
def batch_generate(
    cfg: Config,
    manifest: BatchManifest,
    *,
    store: ArtifactStore,
    batch_id: str,
    concurrent: int | None = None,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    image_engine: ImageEngine | None = None,
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    image_profile_provider: ImageProfileProvider | None = None,
    state_dir: Path = Path(".kinoforge"),
    sink: OutputSink | None = None,
    instance: Instance | None = None,
    tags: dict[str, str] | None = None,
    on_event: BatchEventCallback | None = None,
) -> BatchResult:
```

Update the docstring's `Args:` block — add:

```
on_event: Optional streaming callback fired with one
    :class:`BatchEvent` per per-entry milestone.  Two event kinds:
    ``entry_start`` (just before the worker begins the stage) and
    ``entry_finish`` (after the worker records its terminal status:
    ``ok`` / ``fail`` / ``interrupted`` / ``aborted``).  Calls are
    serialized via an internal ``threading.Lock`` so multi-line
    output never interleaves.  When ``None`` (the default), no
    events fire and behaviour is byte-identical to pre-Layer-L-T4.
```

3c. Build the emitter at the top of the function body (right after `cap = ...` at the current line 605):

```python
    cap = concurrent if concurrent is not None else cfg.lifecycle().max_in_flight

    emit = _LockedEmitter(on_event)
```

3d. Update the build-fail block (currently line 729) to emit both events:

```python
                    except Exception as build_exc:  # noqa: BLE001
                        # Build-time fails record a synthetic outcome
                        # AND emit start+finish so the invariant
                        # start_count == finish_count holds.
                        start_times[idx] = monotonic()
                        emit(
                            BatchEvent(
                                kind="entry_start",
                                batch_id=batch_id,
                                idx=idx,
                                run_id=entry.run_id or str(idx),
                                ts=datetime.now(),
                                entry=entry,
                            )
                        )
                        err_str = f"{type(build_exc).__name__}: {build_exc}"
                        outcomes_by_idx[idx] = BatchOutcome(
                            run_id=entry.run_id or str(idx),
                            status="fail",
                            duration_s=0.0,
                            error=err_str,
                        )
                        emit(
                            BatchEvent(
                                kind="entry_finish",
                                batch_id=batch_id,
                                idx=idx,
                                run_id=entry.run_id or str(idx),
                                ts=datetime.now(),
                                status="fail",
                                duration_s=0.0,
                                error=err_str,
                            )
                        )
                        continue
```

3e. Change the `executor.submit` call (line 741) to pass the new emitter/entry/batch_id args:

```python
                    fut = executor.submit(
                        _run_with_clock,
                        stage,
                        initial_state,
                        start_times,
                        idx,
                        emit=emit,
                        entry=entry,
                        batch_id=batch_id,
                    )
                    future_to_idx[fut] = idx
```

3f. Update `_run_with_clock` (line 364–392) to emit `entry_start`:

```python
def _run_with_clock(
    stage: GenerateClipStage,
    initial_state: PipelineState,
    start_times: dict[int, float],
    idx: int,
    *,
    emit: _LockedEmitter,
    entry: BatchEntry,
    batch_id: str,
) -> Artifact:
    """Stamp the real stage-run start time, emit entry_start, run the stage.

    Recording ``monotonic()`` before ``executor.submit`` would
    conflate queue-wait time with the stage's real wall-clock cost —
    a 5-entry batch with ``concurrent=1`` would report 5x inflated
    durations for the last entries.  Stamping here, inside the worker
    thread, gives ``BatchOutcome.duration_s`` the actual stage cost.

    Args:
        stage: The pre-built GenerateClipStage for this entry.
        initial_state: The initial PipelineState (validated request,
            empty artifacts) for this entry.
        start_times: Shared dict keyed by entry index; this worker
            writes its slot before doing real work.
        idx: The entry's position in ``manifest.entries``.
        emit: Lock-protected streaming emitter; called with the
            ``entry_start`` event for this entry before the stage runs.
        entry: The BatchEntry being run; attached to the entry_start
            event so consumers see prompt + mode without closing over
            the manifest.
        batch_id: The batch's top-level namespace id.

    Returns:
        The persisted :class:`~kinoforge.core.interfaces.Artifact`
        extracted from ``state.artifacts["clip"]`` after the stage runs.
    """
    start_times[idx] = monotonic()
    emit(
        BatchEvent(
            kind="entry_start",
            batch_id=batch_id,
            idx=idx,
            run_id=entry.run_id or str(idx),
            ts=datetime.now(),
            entry=entry,
        )
    )
    out_state = stage.run(initial_state)
    return out_state.artifacts["clip"]
```

3g. Update the result-collection loop (lines 747–787) to emit `entry_finish` on each terminal status:

```python
                try:
                    for fut in as_completed(future_to_idx.keys()):
                        idx = future_to_idx[fut]
                        entry = manifest.entries[idx]
                        duration = monotonic() - start_times.get(idx, batch_start)
                        try:
                            artifact = fut.result()
                        except (
                            BudgetExceeded,
                            CapabilityMismatch,
                            TeardownError,
                        ) as exc:
                            err_str = f"{type(exc).__name__}: {exc}"
                            outcomes_by_idx[idx] = BatchOutcome(
                                run_id=entry.run_id or str(idx),
                                status="interrupted",
                                duration_s=duration,
                                error=err_str,
                            )
                            emit(
                                BatchEvent(
                                    kind="entry_finish",
                                    batch_id=batch_id,
                                    idx=idx,
                                    run_id=entry.run_id or str(idx),
                                    ts=datetime.now(),
                                    status="interrupted",
                                    duration_s=duration,
                                    error=err_str,
                                )
                            )
                            _mark_remaining_after_fatal(
                                future_to_idx,
                                outcomes_by_idx,
                                manifest,
                                start_times,
                                batch_start,
                                emit=emit,
                                batch_id=batch_id,
                                fatal_type=type(exc).__name__,
                            )
                            raise
                        except Exception as exc:  # noqa: BLE001
                            err_str = f"{type(exc).__name__}: {exc}"
                            outcomes_by_idx[idx] = BatchOutcome(
                                run_id=entry.run_id or str(idx),
                                status="fail",
                                duration_s=duration,
                                error=err_str,
                            )
                            emit(
                                BatchEvent(
                                    kind="entry_finish",
                                    batch_id=batch_id,
                                    idx=idx,
                                    run_id=entry.run_id or str(idx),
                                    ts=datetime.now(),
                                    status="fail",
                                    duration_s=duration,
                                    error=err_str,
                                )
                            )
                        else:
                            outcomes_by_idx[idx] = BatchOutcome(
                                run_id=entry.run_id or str(idx),
                                status="ok",
                                duration_s=duration,
                                uri=artifact.uri,
                            )
                            emit(
                                BatchEvent(
                                    kind="entry_finish",
                                    batch_id=batch_id,
                                    idx=idx,
                                    run_id=entry.run_id or str(idx),
                                    ts=datetime.now(),
                                    status="ok",
                                    duration_s=duration,
                                    uri=artifact.uri,
                                )
                            )
```

3h. Update `_mark_remaining_after_fatal` (line 395–440) signature + body to emit per-idx events:

```python
def _mark_remaining_after_fatal(
    future_to_idx: dict[Future[Any], int],
    outcomes_by_idx: dict[int, BatchOutcome],
    manifest: BatchManifest,
    start_times: dict[int, float],
    batch_start: float,
    *,
    emit: _LockedEmitter,
    batch_id: str,
    fatal_type: str,
) -> None:
    """Cancel queued futures and label them aborted/interrupted in-place.

    Called after a batch-fatal exception is observed on one future;
    drains the rest into ``outcomes_by_idx`` so the summary is
    complete before the fatal re-raises.  Emits the streaming events
    so the invariant ``start_count == finish_count == len(entries)``
    holds even on the fatal-abort exit path: never-started entries
    get start + finish back-to-back; in-flight entries get only
    finish (their start was emitted by their worker).

    Args:
        future_to_idx: Map of submitted futures to entry index.
        outcomes_by_idx: Per-entry outcomes being assembled.  Mutated
            in place.
        manifest: The original manifest, used to recover ``run_id`` and
            the full :class:`BatchEntry` for never-started entries.
        start_times: Per-index actual stage-start monotonic stamps;
            entries that never started fall back to ``batch_start``.
        batch_start: The monotonic timestamp captured at batch entry
            into the dispatch loop.
        emit: Lock-protected streaming emitter.
        batch_id: The batch's top-level namespace id.
        fatal_type: Name of the batch-fatal exception
            (``BudgetExceeded`` / ``CapabilityMismatch`` /
            ``TeardownError``).  Used to build the ``error`` field on
            the synthetic outcomes.
    """
    err_str = f"batch aborted by {fatal_type}"
    for other_fut, other_idx in future_to_idx.items():
        if other_idx in outcomes_by_idx:
            continue
        other_entry = manifest.entries[other_idx]
        rid = other_entry.run_id or str(other_idx)
        if other_fut.cancel():
            # Never started: emit start + finish back-to-back.
            emit(
                BatchEvent(
                    kind="entry_start",
                    batch_id=batch_id,
                    idx=other_idx,
                    run_id=rid,
                    ts=datetime.now(),
                    entry=other_entry,
                )
            )
            outcomes_by_idx[other_idx] = BatchOutcome(
                run_id=rid,
                status="aborted",
                duration_s=0.0,
                error=err_str,
            )
            emit(
                BatchEvent(
                    kind="entry_finish",
                    batch_id=batch_id,
                    idx=other_idx,
                    run_id=rid,
                    ts=datetime.now(),
                    status="aborted",
                    duration_s=0.0,
                    error=err_str,
                )
            )
        else:
            # In-flight: worker already emitted entry_start; emit finish only.
            other_duration = monotonic() - start_times.get(other_idx, batch_start)
            outcomes_by_idx[other_idx] = BatchOutcome(
                run_id=rid,
                status="interrupted",
                duration_s=other_duration,
                error=err_str,
            )
            emit(
                BatchEvent(
                    kind="entry_finish",
                    batch_id=batch_id,
                    idx=other_idx,
                    run_id=rid,
                    ts=datetime.now(),
                    status="interrupted",
                    duration_s=other_duration,
                    error=err_str,
                )
            )
```

NOTE the small semantic change: `BatchOutcome` for `aborted` previously had `duration_s=None` and no `error`; now it's `duration_s=0.0` + `error="batch aborted by <FatalType>"`. This makes the on-wire JSONL consistent (every `entry_finish` carries `duration_s`) and matches the spec's nullability rules. The existing tests that check `BatchOutcome` shape on the aborted path will need a one-field update — confirm + bump them in the same commit.

Same for `interrupted`: it now carries `error`. Existing tests that assert on the absence of `error` for interrupted outcomes need the same one-field update.

- [ ] **Step 4: Run the new tests — confirm green.**

```bash
pixi run test tests/core/test_batch.py -v -k "on_event or stream"
```

Expected: 5 new tests pass.

- [ ] **Step 5: Run full `test_batch.py` — fix any cascade breaks.**

```bash
pixi run test tests/core/test_batch.py -v
```

Expected: every test passes. If a test that asserted `outcome.duration_s is None` for aborted entries fails, update that single assertion to `outcome.duration_s == 0.0` and `outcome.error.startswith("batch aborted by ")`. Same for any `interrupted` assertions checking `error is None`.

- [ ] **Step 6: Run full suite.**

```bash
pixi run test -q && pixi run lint && pixi run typecheck
```

Expected: 1308 passed / 8 skipped (1303 + 5 new), lint + mypy clean.

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/core/batch.py tests/core/test_batch.py
git commit -m "feat(batch): emit streaming events at 5 sites (Layer L-T4 T3)

batch_generate gains an opt-in on_event callback; emissions happen at:
  - build-time fail (main thread, start+finish)
  - executor submit (worker thread, start)
  - per-entry ok / fail (main thread, finish)
  - batch-fatal interrupted + aborted (main thread, finish [+ synthetic
    start for never-started entries])

_LockedEmitter wraps the user callback in a single Lock so multi-line
output never interleaves.  on_event=None preserves byte-identical
behaviour; 1297 pre-Layer-L-T4 tests pass unchanged.

Small semantic upgrade: aborted/interrupted outcomes now carry
duration_s (0.0 / actual) + error ('batch aborted by <FatalType>') so
the JSONL on-wire shape is uniform across all entry_finish events.

5 new ACs covering invariant (start_count == finish_count across 4
exit paths), ordering (start before finish per idx), lock-stress,
build-fail emission, batch-fatal event sweep."
```

---

## Task 4: Create `cli/batch_formatters.py` (Human / JSONL / NoOp)

**Goal:** Three formatters that consume `BatchEvent` and render to a stream. `HumanFormatter` carries the summary-table render too (lifted verbatim from `_cmd_batch`).

**Files:**
- Create: `src/kinoforge/cli/batch_formatters.py`
- Create: `tests/cli/test_batch_formatters.py`

**Acceptance Criteria:**
- [ ] `HumanFormatter.emit(BatchEvent(kind="entry_start", ...))` writes a single line `[batch] [idx+1/run_id] START mode=<m> prompt=<repr>\n` to its stream.
- [ ] `HumanFormatter.emit(BatchEvent(kind="entry_finish", status="ok", ...))` writes `[batch] [idx+1/run_id] OK <dur>s <uri>\n`.
- [ ] `HumanFormatter.render_summary(result)` writes the summary table identical to the pre-Layer-L-T4 `_cmd_batch` output (header `\nsummary:\n`, one row per outcome, `batch-id:` + `results:` footer).
- [ ] `JsonlFormatter.emit(event)` writes `event.model_dump_json() + "\n"`; line parses as the round-tripped event.
- [ ] `JsonlFormatter.render_summary(result)` writes a single JSON object with `"kind": "batch_summary"` plus every field from `result.to_dict()`.
- [ ] `NoOpFormatter.emit(event)` writes nothing; `NoOpFormatter.render_summary(result)` produces the same output as `HumanFormatter.render_summary(result)`.
- [ ] `build_formatter("human")` / `("jsonl")` / `("none")` returns the right class; `build_formatter("xyz")` raises `KeyError`.

**Verify:** `pixi run test tests/cli/test_batch_formatters.py -v` → 8 tests pass.

**Steps:**

- [ ] **Step 1: Write failing tests.**

```python
# tests/cli/test_batch_formatters.py
"""Tests for cli/batch_formatters.py."""

from __future__ import annotations

import io
import json
from datetime import datetime

import pytest

from kinoforge.cli.batch_formatters import (
    HumanFormatter,
    JsonlFormatter,
    NoOpFormatter,
    build_formatter,
)
from kinoforge.core.batch_events import BatchEvent
from kinoforge.core.batch_models import BatchEntry, BatchOutcome, BatchResult


def _start_event() -> BatchEvent:
    return BatchEvent(
        kind="entry_start",
        batch_id="b1",
        idx=2,
        run_id="alpha",
        ts=datetime(2026, 6, 5, 10, 30, 0),
        entry=BatchEntry(prompt="hello world", mode="t2v"),
    )


def _finish_ok_event() -> BatchEvent:
    return BatchEvent(
        kind="entry_finish",
        batch_id="b1",
        idx=2,
        run_id="alpha",
        ts=datetime(2026, 6, 5, 10, 30, 1, 500000),
        status="ok",
        duration_s=1.5,
        uri="local://b1/alpha/clip.mp4",
    )


def _sample_result() -> BatchResult:
    return BatchResult(
        batch_id="b1",
        started_at="2026-06-05T10:30:00",
        finished_at="2026-06-05T10:30:05",
        outcomes=[
            BatchOutcome(run_id="alpha", status="ok",
                         duration_s=1.5, uri="local://b1/alpha/clip.mp4"),
            BatchOutcome(run_id="beta", status="fail",
                         duration_s=0.4, error="ValueError: nope"),
        ],
    )


def test_human_emit_start() -> None:
    """Bug: emit silently drops events without writing."""
    buf = io.StringIO()
    HumanFormatter(buf).emit(_start_event())
    line = buf.getvalue()
    assert line.startswith("[b1] [3/alpha] START ")
    assert "mode=t2v" in line
    assert "prompt='hello world'" in line
    assert line.endswith("\n")


def test_human_emit_finish_ok() -> None:
    """Bug: success line drops duration or uri."""
    buf = io.StringIO()
    HumanFormatter(buf).emit(_finish_ok_event())
    line = buf.getvalue()
    assert line.startswith("[b1] [3/alpha] OK ")
    assert "1.5s" in line
    assert "local://b1/alpha/clip.mp4" in line


def test_human_render_summary_matches_legacy_shape() -> None:
    """Bug: summary table drifts from the pre-Layer-L-T4 _cmd_batch shape."""
    buf = io.StringIO()
    HumanFormatter(buf).render_summary(_sample_result())
    out = buf.getvalue()
    assert "\nsummary:\n" in out
    assert "alpha" in out and "OK" in out and "1.5s" in out
    assert "beta" in out and "FAIL" in out and "ValueError: nope" in out
    assert "batch-id: b1" in out
    assert "results:  1/2 ok, 1 failed" in out


def test_jsonl_emit_roundtrips() -> None:
    """Bug: JSONL line is unparseable or drops fields on dump."""
    buf = io.StringIO()
    ev = _finish_ok_event()
    JsonlFormatter(buf).emit(ev)
    line = buf.getvalue().rstrip("\n")
    parsed = json.loads(line)
    assert parsed["kind"] == "entry_finish"
    assert parsed["status"] == "ok"
    assert parsed["uri"] == "local://b1/alpha/clip.mp4"
    # round-trip back to a BatchEvent
    restored = BatchEvent.model_validate_json(line)
    assert restored == ev


def test_jsonl_render_summary_emits_batch_summary_object() -> None:
    """Bug: JSONL stream lacks a terminal 'done' marker for consumers."""
    buf = io.StringIO()
    JsonlFormatter(buf).render_summary(_sample_result())
    line = buf.getvalue().rstrip("\n")
    parsed = json.loads(line)
    assert parsed["kind"] == "batch_summary"
    assert parsed["batch_id"] == "b1"
    assert parsed["entries"][0]["run_id"] == "alpha"


def test_noop_emit_writes_nothing() -> None:
    """Bug: --stream-format=none accidentally still streams."""
    buf = io.StringIO()
    NoOpFormatter(buf).emit(_start_event())
    NoOpFormatter(buf).emit(_finish_ok_event())
    assert buf.getvalue() == ""


def test_noop_render_summary_matches_human() -> None:
    """Bug: --stream-format=none drops the final summary table.

    NoOpFormatter only suppresses mid-run lines; it MUST still render
    the final operator summary identical to HumanFormatter.
    """
    buf_noop = io.StringIO()
    NoOpFormatter(buf_noop).render_summary(_sample_result())
    buf_human = io.StringIO()
    HumanFormatter(buf_human).render_summary(_sample_result())
    assert buf_noop.getvalue() == buf_human.getvalue()


def test_build_formatter_dispatch() -> None:
    """Bug: build_formatter silently routes 'xyz' to a default."""
    assert isinstance(build_formatter("human"), HumanFormatter)
    assert isinstance(build_formatter("jsonl"), JsonlFormatter)
    assert isinstance(build_formatter("none"), NoOpFormatter)
    with pytest.raises(KeyError):
        build_formatter("xyz")
```

- [ ] **Step 2: Confirm red.**

```bash
pixi run test tests/cli/test_batch_formatters.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/kinoforge/cli/batch_formatters.py`.**

```python
# src/kinoforge/cli/batch_formatters.py
"""CLI streaming formatters consuming BatchEvent (Layer L-T4)."""

from __future__ import annotations

import json
import sys
from typing import TextIO

from kinoforge.core.batch_events import BatchEvent
from kinoforge.core.batch_models import BatchResult


class HumanFormatter:
    """Operator-friendly streaming lines + final summary table."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream

    def emit(self, event: BatchEvent) -> None:
        prefix = f"[{event.batch_id}] [{event.idx + 1}/{event.run_id}]"
        if event.kind == "entry_start":
            entry = event.entry
            mode = entry.mode if entry is not None else "?"
            prompt = (entry.prompt or "")[:60] if entry is not None else ""
            self._stream.write(f"{prefix} START mode={mode} prompt={prompt!r}\n")
        else:
            status = (event.status or "?").upper()
            dur = f"{event.duration_s:.1f}s" if event.duration_s is not None else "—"
            tail = event.uri or event.error or ""
            self._stream.write(f"{prefix} {status} {dur} {tail}\n")
        self._stream.flush()

    def render_summary(self, result: BatchResult) -> None:
        """Final summary table — verbatim shape from pre-Layer-L-T4 _cmd_batch."""
        rid_width = max((len(o.run_id) for o in result.outcomes), default=1) + 1
        self._stream.write("\nsummary:\n")
        for o in result.outcomes:
            status_label = o.status.upper()
            duration = (
                f"{o.duration_s:.1f}s" if o.duration_s is not None else "—"
            )
            detail = o.uri if o.uri else (o.error or "")
            self._stream.write(
                f"  {o.run_id:<{rid_width}s} {status_label:<12s} "
                f"{duration:<8s} {detail}\n"
            )
        self._stream.write(f"batch-id: {result.batch_id}\n")
        n_ok = sum(1 for o in result.outcomes if o.status == "ok")
        n_fail = len(result.outcomes) - n_ok
        self._stream.write(
            f"results:  {n_ok}/{len(result.outcomes)} ok, {n_fail} failed\n"
        )
        self._stream.flush()


class JsonlFormatter:
    """Machine-readable JSONL — one event per line, summary as final object."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream

    def emit(self, event: BatchEvent) -> None:
        self._stream.write(event.model_dump_json() + "\n")
        self._stream.flush()

    def render_summary(self, result: BatchResult) -> None:
        payload = {"kind": "batch_summary", **result.to_dict()}
        self._stream.write(json.dumps(payload) + "\n")
        self._stream.flush()


class NoOpFormatter:
    """`--stream-format=none`: skip mid-run lines, keep final summary."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream

    def emit(self, event: BatchEvent) -> None:
        return None  # explicit "drop on the floor"

    def render_summary(self, result: BatchResult) -> None:
        HumanFormatter(self._stream).render_summary(result)


_DISPATCH: dict[str, type] = {
    "human": HumanFormatter,
    "jsonl": JsonlFormatter,
    "none": NoOpFormatter,
}


def build_formatter(kind: str, stream: TextIO = sys.stdout):
    """Return a fresh formatter for the given kind.

    Raises:
        KeyError: ``kind`` is not in ``{"human", "jsonl", "none"}``.
    """
    return _DISPATCH[kind](stream)
```

- [ ] **Step 4: Confirm green.**

```bash
pixi run test tests/cli/test_batch_formatters.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Lint + typecheck + full suite.**

```bash
pixi run lint && pixi run typecheck && pixi run test -q
```

Expected: 1316 passed / 8 skipped, all clean.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/cli/batch_formatters.py tests/cli/test_batch_formatters.py
git commit -m "feat(cli): batch_formatters — Human / JSONL / NoOp (Layer L-T4 T4)

Three formatters consuming BatchEvent.  HumanFormatter carries the
summary-table render lifted verbatim from cli/_commands.py:_cmd_batch
(extracted in Task 5).  JsonlFormatter dumps one event per line and a
final 'batch_summary' object so consumers have a clean done marker.
NoOpFormatter suppresses mid-run lines but keeps the final summary
(operator opts out of streaming, not out of seeing the result).

build_formatter dispatch is dict-backed and raises KeyError on unknown
kinds so argparse 'choices=' covers it without ambiguity.

8 ACs covering each formatter's emit shape, summary contract, and
dispatch behaviour."
```

---

## Task 5: Wire `--stream-format` + formatter into `cli/_commands.py` + `cli/_main.py`

**Goal:** Add the CLI flag, pass `formatter.emit` to `batch_generate`, route the summary render through the formatter, and confirm existing tests pass under the new `human` default.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (add `--stream-format` to the `batch` subparser)
- Modify: `src/kinoforge/cli/_commands.py` (build formatter, pass `on_event`, replace summary block)
- Modify: `tests/cli/test_batch.py` (add 4 ACs)

**Acceptance Criteria:**
- [ ] **AC7** `kinoforge batch ... --stream-format=human` (default) — stdout contains one `START` line + one terminal-status line per entry, plus the summary table.
- [ ] **AC8** `kinoforge batch ... --stream-format=jsonl` — every stdout line parses as JSON; final line carries `"kind": "batch_summary"`; the human summary table is absent.
- [ ] **AC9** `kinoforge batch ... --stream-format=none` — stdout contains only the existing `manifest loaded` header + summary table (no `START` lines).
- [ ] **AC10** `kinoforge batch ... --stream-format=xyz` — argparse exit code 2, error written to stderr.
- [ ] The existing 6 `_cmd_batch` tests pass without modification (they substring-match; new lines are additive).

**Verify:** `pixi run test tests/cli/test_batch.py -v` → existing 6 + 4 new = 10 pass.

**Steps:**

- [ ] **Step 1: Read the current `batch` subparser definition** in `src/kinoforge/cli/_main.py`. Locate the `add_subparser` block that wires up `_cmd_batch`. It will look like `batch_parser.add_argument("--manifest", ...)` etc.

- [ ] **Step 2: Add the new argparse flag.**

In the batch subparser block of `cli/_main.py`, add:

```python
batch_parser.add_argument(
    "--stream-format",
    choices=("human", "jsonl", "none"),
    default="human",
    help="Streaming output format (default: human).",
)
```

- [ ] **Step 3: Replace the streaming + summary block in `_cmd_batch`** (`cli/_commands.py`, lines 267–390).

Find this region:

```python
    print(
        f"[{batch_id}] manifest loaded: {len(manifest.entries)} entries, "
        f"concurrency={args.concurrent or cfg.lifecycle().max_in_flight}"
    )

    try:
        result = batch_generate(
            cfg, manifest, store=store, sink=sink,
            batch_id=batch_id, concurrent=args.concurrent,
            state_dir=ctx.state_dir,
        )
```

Replace with:

```python
    from kinoforge.cli.batch_formatters import build_formatter

    formatter = build_formatter(args.stream_format)

    # In jsonl mode, send the manifest-loaded header to stderr so
    # stdout remains pure JSONL for downstream piping.
    header = (
        f"[{batch_id}] manifest loaded: {len(manifest.entries)} entries, "
        f"concurrency={args.concurrent or cfg.lifecycle().max_in_flight}"
    )
    if args.stream_format == "jsonl":
        print(header, file=sys.stderr)
    else:
        print(header)

    try:
        result = batch_generate(
            cfg, manifest, store=store, sink=sink,
            batch_id=batch_id, concurrent=args.concurrent,
            state_dir=ctx.state_dir,
            on_event=formatter.emit,
        )
```

And replace the trailing summary block (lines 379–390 — `print("\nsummary:")` through the final `return 0 if n_fail == 0 else 1`) with:

```python
    formatter.render_summary(result)
    n_ok = sum(1 for o in result.outcomes if o.status == "ok")
    n_fail = len(result.outcomes) - n_ok
    return 0 if n_fail == 0 else 1
```

- [ ] **Step 4: Add the 4 new CLI tests** in `tests/cli/test_batch.py`.

```python
# -------------------------------------------------------------------
# Layer L-T4 — --stream-format CLI tests
# -------------------------------------------------------------------

import json


def test_stream_format_human_emits_per_entry_lines(
    cli_batch_runner,  # existing fixture invoking the CLI with a fake batch
    capsys,
) -> None:
    """AC7: default human mode emits START + terminal lines + summary."""
    exit_code = cli_batch_runner(extra_args=[])  # default --stream-format=human
    out = capsys.readouterr().out
    # one START per entry, one OK per entry, plus summary table
    assert out.count(" START ") == 3  # adjust to match fixture entry count
    assert out.count(" OK ") == 3
    assert "summary:" in out
    assert exit_code == 0


def test_stream_format_jsonl_emits_parseable_lines(cli_batch_runner, capsys) -> None:
    """AC8: jsonl mode produces one JSON object per stdout line + final summary."""
    exit_code = cli_batch_runner(extra_args=["--stream-format=jsonl"])
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]  # MUST all parse
    assert parsed[-1]["kind"] == "batch_summary"
    assert parsed[-1]["batch_id"]
    # the human summary table is absent
    assert "summary:" not in out
    assert exit_code == 0


def test_stream_format_none_only_header_and_summary(
    cli_batch_runner, capsys,
) -> None:
    """AC9: none mode suppresses mid-run lines but keeps summary."""
    exit_code = cli_batch_runner(extra_args=["--stream-format=none"])
    out = capsys.readouterr().out
    assert " START " not in out
    assert "summary:" in out
    assert "manifest loaded" in out
    assert exit_code == 0


def test_stream_format_invalid_choice(cli_batch_runner, capsys) -> None:
    """AC10: argparse rejects xyz with exit code 2."""
    with pytest.raises(SystemExit) as excinfo:
        cli_batch_runner(extra_args=["--stream-format=xyz"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "stream-format" in err.lower() or "invalid choice" in err.lower()
```

Adjust fixture name to match what `tests/cli/test_batch.py` already uses for invoking `_cmd_batch`. If the file constructs argparse args directly inside each test, follow that style instead of `cli_batch_runner`.

- [ ] **Step 5: Confirm new tests red, then green after wiring.**

```bash
pixi run test tests/cli/test_batch.py -v
```

The 4 new tests fail first (argparse doesn't know `--stream-format` yet). After Steps 2 + 3 land, run again — they should all pass.

- [ ] **Step 6: Confirm the existing 6 `_cmd_batch` tests still pass.**

```bash
pixi run test tests/cli/test_batch.py -v -k "not stream"
```

Expected: 6 passed. They are substring asserts; mid-run lines are additive and do not break the existing assertions. If one DOES break, audit the assertion: if it asserts full-stdout equality (rather than substring), either (a) update that single test to use `extra_args=["--stream-format=none"]` so its output matches the pre-Layer fixture, or (b) update its expected substring set to include the new lines. Prefer (a) — fewer test changes.

- [ ] **Step 7: Full suite + lint + typecheck.**

```bash
pixi run test -q && pixi run lint && pixi run typecheck
```

Expected: 1320 passed / 8 skipped (1316 + 4 new), clean.

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_batch.py
git commit -m "feat(cli): --stream-format flag wires batch streaming (Layer L-T4 T5)

Adds --stream-format={human,jsonl,none} default human.  _cmd_batch
builds a formatter and passes formatter.emit as batch_generate's
on_event; summary render goes through formatter.render_summary so
JSONL mode replaces the human table with one batch_summary object.

In JSONL mode the 'manifest loaded' header goes to stderr so stdout
is pure JSONL and can be piped into jq directly.

4 new ACs covering each stream-format choice + invalid choice
rejection.  Existing 6 _cmd_batch tests pass unchanged (substring
asserts are additive-safe)."
```

---

## Task 6: README + PROGRESS update + final gate + merge prep

**Goal:** Document the new flag for operators, record the layer close-out in PROGRESS.md, run the final full-suite gate, and prepare the merge commit.

**Files:**
- Modify: `README.md` (Batch jobs section gains a "Streaming output" subheading)
- Modify: `PROGRESS.md` (Phase 35 / Layer L-T4 entry, close PROGRESS:325 deferral)

**Acceptance Criteria:**
- [ ] README "Batch jobs" section has a `### Streaming output` subheading explaining the three `--stream-format` choices with one short example each.
- [ ] PROGRESS.md has a new Phase 35 entry (under `## Post-MVP`) titled `Phase 35 — Layer L-T4 (batch streaming logs)` with per-task SHA references, key design decisions, and a closing line that says "Closes PROGRESS:325 follow-up #1 (Layer L Task 4 streaming-log deferral)".
- [ ] `## Known limitations & follow-ups` section in PROGRESS.md updates the line `"Streaming per-entry log lines (DEFERRED)"` to `"CLOSED by Phase 35 (Layer L-T4)"`.
- [ ] PROGRESS.md "Single next action" pointer block is rewritten to reflect Phase 35 close-out, suite count, budget unchanged.
- [ ] Full suite + lint + typecheck clean.

**Verify:** `pixi run test -q && pixi run lint && pixi run typecheck` → 1320 passed / 8 skipped, all clean.

**Steps:**

- [ ] **Step 1: Read the README "Batch jobs" section** to learn the heading style.

```bash
rg -n '^##.*batch' --type md README.md -i
```

- [ ] **Step 2: Add the new subsection** at the end of the "Batch jobs" section in README.md:

```markdown
### Streaming output

`kinoforge batch` emits per-entry progress lines as the run proceeds.
The output format is controlled by `--stream-format`:

- `--stream-format=human` (default) — operator-readable lines:
  ```
  [batch-20260605-103000] [1/dawn]  START mode=t2v prompt='a sunrise over the cliffs'
  [batch-20260605-103000] [1/dawn]  OK    1.5s local://.kinoforge/batch-20260605-103000/dawn/clip.mp4
  ```
- `--stream-format=jsonl` — one JSON event per stdout line, terminated
  by a `{"kind":"batch_summary",...}` object. The `manifest loaded`
  header is routed to stderr so stdout is pure JSONL:
  ```
  kinoforge batch --config c.yaml --manifest m.yaml --stream-format=jsonl | jq .
  ```
- `--stream-format=none` — suppress mid-run lines; the final summary
  table is still printed. Matches pre-Layer-L-T4 behaviour.

Library users of `batch_generate()` can plug their own consumer by
passing `on_event=<callable>` directly.
```

- [ ] **Step 3: Update PROGRESS.md known-limitations entry.**

Find the line in PROGRESS.md `## Known limitations & follow-ups` → architectural section that reads:

```
- `Streaming per-entry log lines (DEFERRED): ...`
```

(actually phrased as "Streaming per-entry log lines (DEFERRED):" inside the Layer L block at line ~325). Replace its tail with:

```
~~Streaming per-entry log lines (DEFERRED): ...~~ — **CLOSED** by Phase 35 (Layer L-T4).
```

- [ ] **Step 4: Add a new Phase 35 section under `## Post-MVP`.**

Append after the Phase 34 section in PROGRESS.md:

```markdown
### Phase 35 — Layer L-T4 (batch streaming logs)

- [x] Task 1: Extract batch dataclasses to `core/batch_models.py` — commit `<SHA-T1>`
- [x] Task 2: `core/batch_events.py` — BatchEvent + _LockedEmitter + 6 ACs — commit `<SHA-T2>`
- [x] Task 3: batch_generate emits at 5 sites; aborted/interrupted now carry duration_s + error for JSONL uniformity — commit `<SHA-T3>`
- [x] Task 4: `cli/batch_formatters.py` — Human / JSONL / NoOp + 8 ACs — commit `<SHA-T4>`
- [x] Task 5: `--stream-format={human,jsonl,none}` wired through `_cmd_batch` + 4 ACs — commit `<SHA-T5>`
- [x] Task 6: README + PROGRESS + final gate + merge — commit `<SHA-T6>`
- [x] Merge to main via `--no-ff` — merge commit `<SHA-MERGE>` (closes PROGRESS:325 follow-up #1)

**Key design decisions:**
- Callback hook in core (foundation-first; Q1=C). CLI consumes the seam.
  Future consumers (Slack, Prometheus, TUI) cost nothing extra.
- Bundle JSONL formatter on day one (Q1 follow-up): operators get pipeable
  output without a follow-on layer.
- Minimal event vocabulary (Q2=A): `entry_start` + `entry_finish`. New
  status values added as enum extensions, not new event kinds.
- Internal `threading.Lock` serializes the user callback (Q3=A). Matches
  stdlib logging.Handler pattern.
- Lean+entry event payload (Q4=A): `BatchEvent` carries the universal
  fields plus a full `BatchEntry` on `entry_start` so formatters do not
  need to close over the manifest.
- Build-time fail emits both events back-to-back (Q5=A): preserves
  invariant `start_count == finish_count == len(entries)` across all 4
  exit paths.
- Single CLI flag default `human` (Q6=A): visible behaviour change ships
  the layer to existing users; `--stream-format=none` preserves prior
  output for anyone who wants it.
- Behavioural upgrade: `BatchOutcome` for `aborted` / `interrupted` now
  carries `duration_s` (0.0 / actual) + `error` (`"batch aborted by
  <FatalType>"`) so the JSONL on-wire shape is uniform across every
  `entry_finish`. Pre-Layer-L-T4 outcomes for these paths had `duration_s
  = None` and no `error`. The existing `tests/core/test_batch.py`
  assertions on those fields were updated in T3.
- Model-extract refactor (Q9): `BatchEntry` / `BatchManifest` /
  `BatchOutcome` / `BatchResult` moved to `core/batch_models.py` to dodge
  an import cycle with `core/batch_events.py`. `core/batch.py`
  re-exports the four names so every existing import site keeps working.

**Test count:** 1297 + 8 (test_batch_events) + 5 (test_batch streaming
ACs) + 8 (test_batch_formatters) + 4 (test_batch.py CLI ACs) = ~1322
passed (+25 net; minor adjustments to existing aborted/interrupted
assertions counted as zero-net).

**Live spend:** $0. Fully offline-tested via existing
`FakeProvider`/`FakeEngine` fixtures.
```

Replace each `<SHA-TN>` with the actual commit hash after the merge commit lands; backfill is done in the same Task 6 commit (this is the same pattern as Phase 34 Task 6 — backfill SHAs alongside the README + PROGRESS edits).

- [ ] **Step 5: Rewrite the `### RESUME — START HERE` block** at the top of `## Single next action` in PROGRESS.md so the next fresh session reads the right state. Use the Phase 34 close-out block as a template; substitute Phase 35 / Layer L-T4 / suite count / budget unchanged ($10.92 of $15 budget remaining; no live spend on this layer).

- [ ] **Step 6: Run the final gate.**

```bash
pixi run test -q && pixi run lint && pixi run typecheck
```

Expected: ~1322 passed / 8 skipped, all clean.

- [ ] **Step 7: Commit the docs.**

```bash
git add README.md PROGRESS.md
git commit -m "docs: README + PROGRESS Phase 35 close-out (Layer L-T4)

README 'Batch jobs' gains 'Streaming output' subheading with one
example per --stream-format choice and a pointer to on_event for
library users.

PROGRESS Phase 35 entry records per-task SHAs, key design decisions,
and the JSONL on-wire uniformity upgrade (aborted/interrupted now
carry duration_s + error).  Known-limitations 'Streaming per-entry
log lines (DEFERRED)' line marked CLOSED.

Closes PROGRESS:325 follow-up #1 (Layer L Task 4 streaming-log
deferral)."
```

- [ ] **Step 8: Prepare the merge.**

```bash
git log --oneline -8  # confirm 6 task commits + this docs commit
```

Switch to merge (operator's call — Subagent-Driven flow or a separate merge step):

```bash
# (Operator runs this when ready to integrate.)
# git checkout main
# git merge --no-ff <layer-branch> -m "Merge Layer L-T4 — batch streaming logs"
```

The `--no-ff` merge commit pattern matches every prior layer (Phase 17, 18, 22, 24, etc.).

---

## Self-review

**Spec coverage:** Walked spec sections § 1–9 against tasks. Every AC1–AC12 maps to a step:

| Spec AC | Task | Step |
|---|---|---|
| AC1 | T3 | new test `test_on_event_none_default_behaviour_unchanged` |
| AC2 | T3 | new test `test_streaming_invariant_clean_path` + recovered in T3 build/batch-fatal tests |
| AC3 | T3 | new test `test_streaming_ordering_start_before_finish` |
| AC4 | T2 + T3 | `test_locked_emitter_serializes` + `test_streaming_lock_serializes_workers` |
| AC5 | T3 | `test_streaming_build_fail_emits_both` |
| AC6 | T3 | `test_streaming_batch_fatal_emits_interrupted_and_aborted` |
| AC7 | T5 | `test_stream_format_human_emits_per_entry_lines` |
| AC8 | T5 | `test_stream_format_jsonl_emits_parseable_lines` |
| AC9 | T5 | `test_stream_format_none_only_header_and_summary` |
| AC10 | T5 | `test_stream_format_invalid_choice` |
| AC11 | T2 | `test_event_frozen` + `test_event_json_roundtrip` + `test_field_nullability_rules` |
| AC12 | T1 + T2 | invariant scan run at end of T1 and again at full-suite gates in T3 / T4 / T5 / T6 |

**Placeholder scan:** No `TBD`/`TODO`/"implement later" strings. Each step shows the actual code or command. Build-fail / budget-exceeded test fixtures need fixture names matched to the existing `tests/core/test_batch.py` shape (called out explicitly in T3 Step 1).

**Type consistency:** `BatchEventCallback` defined in T2, used in T3 signature. `_LockedEmitter` defined in T2, used in T3 + invoked from `cli/_commands.py` indirectly via `formatter.emit`. `build_formatter` defined in T4, called in T5. `HumanFormatter.render_summary` signature defined in T4, called in T5. No drift.

**Scope check:** One spec → one plan → 6 atomic commits. Each task commits + verifies. No task touches more than one concern.

**One late catch:** the new `BatchOutcome` `error` field for `aborted`/`interrupted` entries is a behavioural upgrade vs. today (today they carry no `error`). This breaks the soft contract that `aborted.error is None`. T3 Step 5 explicitly calls out to fix the cascading test assertions. Also documented in Phase 35 PROGRESS entry. If any out-of-tree consumer reads `BatchOutcome.error` for aborted entries, they get a string now — that's a deliberate JSONL uniformity decision, not an oversight.

No user-gate tasks. Skill's gate-language scan: "verify"/"validate"/"check" appear but only as routine verbs; no scope-ordering language, no "smoke test" / "acceptance test" nouns, no "prove it works" proof phrases. No tagging needed.
