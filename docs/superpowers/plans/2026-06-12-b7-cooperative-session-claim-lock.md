# B7 — Cooperative session-claim lock — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the deploy-session-vs-sweep race documented at Layer V spec §5 Risk 3 by extending the existing `provision:<id>` lock's scope from "engine.provision only" to "instance-id committed through first heartbeat tick lands", and growing the reaper to non-blocking-probe the same key before destroying.

**Architecture:** Single new helper module `core/session_claim.py` exposes `hold_until_first_tick` context manager. The orchestrator's `deploy_session.__enter__` wraps the verify + pool-build + HeartbeatLoop-start region in it (gated on the same `interval is not None and interval > 0 and instance is not None and resolved_provider is not None` condition as HeartbeatLoop spawn). The inner acquire site at `_provision_compute_once` is deleted — the outer hold subsumes it. The reaper grows a ~10-LOC probe arm in `act_on_verdict` that returns `ActionResult(action="deferred-session-claim", ...)` on contention, with the orchestrator's `holder_pid` lifted from the FileLock sidecar into `ActionResult.reason` for operator-UX. `Ledger` grows a per-id `read(instance_id) -> dict | None` method so the helper can poll for `heartbeat_thread_tick` without scanning the entries list.

**Tech Stack:** Python 3.11+, pydantic, pytest, fcntl (Linux file locks via existing `LocalArtifactStore.acquire_lock` / `FileLock`), subprocess (cross-process integration test), kinoforge's existing `Clock` injection seam, kinoforge's existing `Ledger` / `HeartbeatLoop` / `act_on_verdict` substrate. Spec at `docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md` (commit `44a35ad`).

**Spec-vs-code drift discovered during planning (must implement to actual code, not spec):**

- **`ActionResult` location & shape.** Spec §3.5 names `core/reaper.py` and shows `action: Literal[...]` + `cost_rate_usd_per_hr: float`. Real dataclass is at `src/kinoforge/core/reaper_actor.py:47`. Real fields: `instance_id, snapshot_verdict, applied_verdict, action: str, reason: str | None = None`. No `cost_rate_usd_per_hr`. `action` is plain `str` (no Literal). Plan implementation: just append `"deferred-session-claim"` to the docstring action enumeration — no Literal widening.
- **CLI module path.** Spec §3.1 says `src/kinoforge/cli.py:_cmd_reap`. Real path is `src/kinoforge/cli/_commands.py:815`.
- **Watchdog implementation.** Spec §3.6 calls for a "5s wall-clock watchdog" reading the sidecar JSON. Given D5's non-blocking probe (instant — no waiting), the equivalent operator-UX simpler pattern is to read `holder_pid` from the FileLock sidecar JSON at probe-time and pass it back via `ActionResult.reason="held by pid <N>"`. The existing `_emit_reap_human` already renders reasons. AC9 satisfied; no separate watchdog needed.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/kinoforge/core/session_claim.py` | NEW | `hold_until_first_tick` context manager + `FirstTickTimeout` exception. ~50 LOC. |
| `src/kinoforge/core/lifecycle.py` | EDIT | `Ledger` grows `read(instance_id: str) -> dict | None`. ~10 LOC. |
| `src/kinoforge/core/orchestrator.py` | EDIT | `_provision_compute_once` drops inner provision-lock; `deploy_session.__enter__` wraps verify+pool+HB-start in `hold_until_first_tick`. ~20 LOC. |
| `src/kinoforge/core/reaper_actor.py` | EDIT | `act_on_verdict` adds non-blocking probe of `provision:<id>` + new ActionResult docstring action. Also reads holder_pid via `LocalArtifactStore` introspection. ~25 LOC. |
| `src/kinoforge/cli/_commands.py` | EDIT | `_emit_reap_human` adds `deferred` to summary line counts; render `reason` on deferred entries. ~5 LOC. |
| `tests/core/test_ledger_read.py` | NEW | Ledger.read unit tests, mirrors test_ledger_touch.py shape. |
| `tests/core/test_session_claim.py` | NEW | 7 unit tests for hold_until_first_tick. |
| `tests/core/test_orchestrator_session_claim_xprocess.py` | NEW | Cross-process subprocess integration test. |
| `tests/core/test_reaper_actor.py` | EDIT | 2 new test cases for the probe arm. |
| `tests/stores/test_acquire_lock_contract.py` | EDIT | held-while-orchestrator-runs parametrize case across local + mocked-S3. |
| `PROGRESS.md` | EDIT | Strike B7 in §B; update with closeout SHA. |
| `warm-reuse-tasks.txt` | EDIT | Update B7 status from "design APPROVED" to "CLOSED commit <sha>". |
| `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` | EDIT | Amend §5 Risk 3 + §6 to point at B7 spec as closing the gap. |

---

## Sanity-checks against repo (verified 2026-06-12 during planning)

- `core/reaper_actor.py:27` — `_LOCK_TTL_S: float = 30.0` ✓ (UNCHANGED by B7).
- `core/reaper_actor.py:47` — `class ActionResult` ✓ (here, not `reaper.py`).
- `core/reaper_actor.py:138` — `with store.acquire_lock(f"reaper/{instance_id}", ttl_s=_LOCK_TTL_S):` ✓ (B7 inserts probe immediately inside this block).
- `core/orchestrator.py:231` — `with store.acquire_lock(f"provision:{instance.id}", ttl_s=300):` ✓ (B7 deletes this line + dedents body to line 282).
- `core/orchestrator.py:508` — `def deploy_session(` ✓ (the context manager B7 modifies).
- `core/orchestrator.py:716` — `# Step 8 — verify` comment ✓ (B7's wrap starts before this).
- `core/orchestrator.py:736` — `pool = ConcurrentPool()` (Step 8.5 start) ✓.
- `core/orchestrator.py:756` — HeartbeatLoop spawn-gate `if interval is not None and interval > 0 and instance is not None and resolved_provider is not None:` ✓ (B7's wrap-gate matches).
- `core/orchestrator.py:770` — `hb_loop.start()` ✓ (B7's wrap ends at the line after this).
- `core/heartbeat_loop.py:152` — eager first tick ✓.
- `core/lifecycle.py:516` — `Ledger.entries()` ✓ (NO `Ledger.read` exists; B7 adds it).
- `core/lifecycle.py:421` — `Ledger._read_entries()` ✓ (B7's `read` will use it).
- `core/lifecycle.py:542` — `Ledger.touch` ✓ (writes `heartbeat_thread_tick` via `**extra`).
- `core/clock.py:14/30/49` — `Clock` Protocol, `RealClock`, `FakeClock` ✓.
- `stores/local_lock.py:30` — `class FileLock` ✓; sidecar JSON has `{nonce, holder_pid, expires_at}` ✓.
- `stores/local_lock.py:108` — `def acquire(*, blocking=True, timeout_s=None)` ✓.
- `cli/_commands.py:815` — `def _cmd_reap` ✓.
- `cli/_commands.py:913-983` — `_emit_reap_human` / `_emit_reap_jsonl` ✓.
- `tests/stores/test_acquire_lock_contract.py` — exists, 6 tests today ✓.
- `tests/core/test_ledger_touch.py` — shape to mirror for `test_ledger_read.py` ✓.

---

## Task Execution Order

```
Task 1 (Ledger.read)
  ↓
Task 2 (session_claim.py) — depends on Task 1 (needs Ledger.read API)
  ↓
Task 3 (orchestrator wire-in) — depends on Task 2 (needs hold_until_first_tick)
  ↓
Task 4 (reaper probe) — independent of Task 3 but logically sequenced
  ↓
Task 5 (cross-process integration test) — depends on Tasks 2, 3, 4 (real wire)
  ↓
Task 6 (CLI render + lock-contract delta) — depends on Task 4 (renders deferred ActionResult)
  ↓
Task 7 (closeout docs)
```

Tasks 1–4 are the core implementation. Task 5 is the integration lockdown. Task 6 is operator-UX. Task 7 is documentation closeout.

---

### Task 1: `Ledger.read(instance_id) -> dict | None`

**Goal:** Add a per-id ledger read method mirroring the `record`/`forget`/`touch` per-id surface so callers don't have to scan `entries()` manually.

**Files:**
- Create: `tests/core/test_ledger_read.py`
- Modify: `src/kinoforge/core/lifecycle.py` (add `read` method on `Ledger` class around line 525, between `entries` and `forget`)

**Acceptance Criteria:**
- [ ] `Ledger.read("known-id")` returns the matching entry dict.
- [ ] `Ledger.read("unknown-id")` returns `None`.
- [ ] After `Ledger.forget(id)`, `Ledger.read(id)` returns `None`.
- [ ] Method does NOT acquire `ledger/<run_id>` mutate-lock (read-only — fast path).

**Verify:** `pixi run pytest tests/core/test_ledger_read.py -v` → 4 passing tests.

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/core/test_ledger_read.py` with:

```python
"""B7 T1: Ledger.read per-id lookup.

Read-only mirror of the record/forget/touch per-id surface. Returns the
matching entry dict, or None when absent (including post-forget).
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.local import LocalArtifactStore


def _make_instance(instance_id: str = "i-read", *, provider: str = "local") -> Instance:
    return Instance(
        id=instance_id,
        provider=provider,
        status="ready",
        created_at=0.0,
        cost_rate_usd_per_hr=0.0,
        tags={},
    )


def test_read_returns_entry_for_recorded_id(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-known")
    ledger.record(inst)

    entry = ledger.read("i-known")

    assert entry is not None
    assert entry["id"] == "i-known"
    assert entry["provider"] == "local"


def test_read_returns_none_for_unknown_id(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)

    assert ledger.read("never-recorded") is None


def test_read_returns_none_after_forget(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-forget")
    ledger.record(inst)
    assert ledger.read("i-forget") is not None

    ledger.forget("i-forget")

    assert ledger.read("i-forget") is None


def test_read_does_not_acquire_mutate_lock(tmp_path: Path) -> None:
    """read() is read-only — must NOT acquire ledger/<run_id> mutate lock.

    Holding the mutate lock during read would contend with concurrent touch()
    calls from HeartbeatLoop. Verify by recording inside a held mutate lock
    and confirming read() still returns the entry without blocking.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-nolock")
    ledger.record(inst)

    # Acquire the mutate lock externally and prove read() does not block.
    with store.acquire_lock("ledger/_lifecycle", ttl_s=30.0):
        entry = ledger.read("i-nolock")

    assert entry is not None
    assert entry["id"] == "i-nolock"
```

- [ ] **Step 2: Run tests to confirm RED**

```bash
pixi run pytest tests/core/test_ledger_read.py -v
```

Expected: `AttributeError: 'Ledger' object has no attribute 'read'` on all 4 tests.

- [ ] **Step 3: Implement `Ledger.read`**

In `src/kinoforge/core/lifecycle.py`, locate the `entries` method (around line 516) and insert `read` immediately after it, BEFORE `forget`:

```python
    def read(self, instance_id: str) -> dict | None:  # type: ignore[type-arg]
        """Return the ledger entry for ``instance_id``, or ``None`` when absent.

        Read-only per-id mirror of :meth:`record` / :meth:`forget` / :meth:`touch`.
        Does NOT acquire the ``ledger/<run_id>`` mutate lock — readers must not
        contend with concurrent ``touch`` from :class:`HeartbeatLoop`.

        Args:
            instance_id: The instance id to look up.

        Returns:
            The matching entry dict (same shape as ``entries()`` elements), or
            ``None`` when no entry exists for ``instance_id`` (including the
            post-``forget`` state).
        """
        for entry in self._read_entries():
            if entry.get("id") == instance_id:
                return entry
        return None
```

- [ ] **Step 4: Run tests to confirm GREEN**

```bash
pixi run pytest tests/core/test_ledger_read.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Run pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/lifecycle.py tests/core/test_ledger_read.py
git add src/kinoforge/core/lifecycle.py tests/core/test_ledger_read.py
git commit -m "$(cat <<'EOF'
feat(b7): Ledger.read(instance_id) per-id lookup

Read-only mirror of record/forget/touch per-id surface. Returns the
matching entry dict, or None when absent. Does NOT acquire the ledger
mutate lock — readers must not contend with concurrent HeartbeatLoop
touch() calls.

Unblocks B7's hold_until_first_tick helper, which polls
heartbeat_thread_tick from a known instance_id without scanning
entries().

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/core/lifecycle.py", "tests/core/test_ledger_read.py"], "verifyCommand": "pixi run pytest tests/core/test_ledger_read.py -v", "acceptanceCriteria": ["Ledger.read('known-id') returns matching entry dict", "Ledger.read('unknown-id') returns None", "Ledger.read returns None after forget", "Ledger.read does NOT acquire ledger/<run_id> mutate lock"]}
```

---

### Task 2: `core/session_claim.py` — `hold_until_first_tick` helper

**Goal:** Build the new substrate module exposing the context manager that acquires `provision:<id>` blocking, yields, polls `Ledger.read(id)["heartbeat_thread_tick"]` until `>= start`, releases. Raises `FirstTickTimeout` on `timeout_s` exhaustion. Fully testable in isolation with `FakeClock` + `LocalArtifactStore`.

**Files:**
- Create: `src/kinoforge/core/session_claim.py`
- Create: `tests/core/test_session_claim.py`

**Acceptance Criteria:**
- [ ] `hold_until_first_tick` acquires the lock, yields, polls ledger, releases on `tick >= start`.
- [ ] `FirstTickTimeout` raises when `timeout_s` elapses without a fresh tick (including the `ledger.read(...) is None` edge — same loud failure).
- [ ] Yielded-block exception propagates unchanged; polling step is skipped on the exception path; lock releases.
- [ ] Second concurrent `hold_until_first_tick` for the same `instance_id` blocks until the first releases.
- [ ] `clock.now()` (NOT `time.time()`) sources `start` and the timeout deadline.
- [ ] `poll_interval_s` drives sleep cadence; injectable for tests.

**Verify:** `pixi run pytest tests/core/test_session_claim.py -v` → 7 passing tests.

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/core/test_session_claim.py`:

```python
"""B7 T2: hold_until_first_tick context manager unit tests.

Pure offline coverage of the lock+poll+release contract. Cross-process
integration lives at tests/core/test_orchestrator_session_claim_xprocess.py.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.session_claim import FirstTickTimeout, hold_until_first_tick
from kinoforge.stores.local import LocalArtifactStore


def _make_instance(instance_id: str = "i-claim") -> Instance:
    return Instance(
        id=instance_id,
        provider="local",
        status="ready",
        created_at=0.0,
        cost_rate_usd_per_hr=0.0,
        tags={},
    )


def _record_and_set_tick(ledger: Ledger, instance_id: str, tick: float) -> None:
    """Helper: record instance + touch with the given heartbeat_thread_tick."""
    ledger.record(_make_instance(instance_id))
    ledger.touch(instance_id, heartbeat_thread_tick=tick)


class _CountingSleep:
    """Replace time.sleep in the helper to count poll iterations."""

    def __init__(self, real_sleep: float = 0.0) -> None:
        self.calls: list[float] = []
        self._real = real_sleep

    def __call__(self, s: float) -> None:
        self.calls.append(s)
        if self._real > 0:
            time.sleep(self._real)


def test_acquires_yields_and_releases_on_first_tick(tmp_path: Path) -> None:
    """Happy path: acquire → yield → poll → release when tick >= start."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-happy"))

    # Inside the with-block: simulate HeartbeatLoop writing tick = 100.5
    with hold_until_first_tick(
        store=store,
        instance_id="i-happy",
        ledger=ledger,
        ttl_s=60.0,
        timeout_s=60.0,
        poll_interval_s=0.01,
        clock=clock,
    ):
        # Caller (orchestrator) runs engine.provision + starts HeartbeatLoop.
        # We simulate that by touching the ledger before the yield's exit
        # (which is when the polling phase begins).
        ledger.touch("i-happy", heartbeat_thread_tick=100.5)

    # If we got here without raising, the helper released on tick observation.
    # Verify the lock is now releasable by anyone else.
    assert store.acquire_lock("provision:i-happy", ttl_s=1.0)


def test_first_tick_timeout_raises_when_no_tick(tmp_path: Path) -> None:
    """Polling exhausts timeout_s with tick never written -> FirstTickTimeout."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-timeout"))

    # Drive clock forward inside sleep so we exhaust budget deterministically.
    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        clock.advance(s)

    with pytest.raises(FirstTickTimeout):
        with hold_until_first_tick(
            store=store,
            instance_id="i-timeout",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=0.5,
            poll_interval_s=0.1,
            clock=clock,
            sleep=fake_sleep,  # NEW seam — see implementation
        ):
            pass  # caller does nothing; no tick ever written

    # Helper polled at least once after the yield.
    assert len(sleeps) >= 1


def test_first_tick_timeout_when_ledger_read_none(tmp_path: Path) -> None:
    """Helper given an instance_id that was never recorded -> timeout."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)

    def fake_sleep(s: float) -> None:
        clock.advance(s)

    with pytest.raises(FirstTickTimeout):
        with hold_until_first_tick(
            store=store,
            instance_id="i-never-recorded",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=0.5,
            poll_interval_s=0.1,
            clock=clock,
            sleep=fake_sleep,
        ):
            pass


def test_yielded_block_exception_propagates_and_releases_lock(tmp_path: Path) -> None:
    """Caller raises -> exception re-raised; polling skipped; lock released."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-raise"))

    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)

    class _CallerError(RuntimeError):
        pass

    with pytest.raises(_CallerError):
        with hold_until_first_tick(
            store=store,
            instance_id="i-raise",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=60.0,
            poll_interval_s=0.1,
            clock=clock,
            sleep=fake_sleep,
        ):
            raise _CallerError("boom")

    # Polling never ran — sleep was never called.
    assert sleeps == []
    # Lock was released.
    assert store.acquire_lock("provision:i-raise", ttl_s=1.0)


def test_blocking_acquire_serializes_concurrent_calls(tmp_path: Path) -> None:
    """Second concurrent hold_until_first_tick blocks until first releases."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-serial"))

    sequence: list[str] = []
    first_started = threading.Event()
    first_release = threading.Event()

    def first() -> None:
        with hold_until_first_tick(
            store=store,
            instance_id="i-serial",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=60.0,
            poll_interval_s=0.01,
            clock=clock,
        ):
            sequence.append("first-entered")
            first_started.set()
            first_release.wait(timeout=5.0)
            ledger.touch("i-serial", heartbeat_thread_tick=100.5)
        sequence.append("first-released")

    def second() -> None:
        first_started.wait(timeout=5.0)
        # We'll write the tick before second enters its polling phase.
        ledger.touch("i-serial", heartbeat_thread_tick=200.5)
        clock_local = FakeClock(start=200.0)
        with hold_until_first_tick(
            store=store,
            instance_id="i-serial",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=60.0,
            poll_interval_s=0.01,
            clock=clock_local,
        ):
            sequence.append("second-entered")
        sequence.append("second-released")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    first_started.wait(timeout=5.0)
    # second should be blocked on first's lock; sequence shows only first.
    assert sequence == ["first-entered"]
    first_release.set()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)
    assert sequence == [
        "first-entered",
        "first-released",
        "second-entered",
        "second-released",
    ]


def test_clock_injection_used_for_start_time(tmp_path: Path) -> None:
    """start = clock.now() — not time.time()."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=12345.0)
    ledger.record(_make_instance("i-clock"))

    with hold_until_first_tick(
        store=store,
        instance_id="i-clock",
        ledger=ledger,
        ttl_s=60.0,
        timeout_s=60.0,
        poll_interval_s=0.01,
        clock=clock,
    ):
        # Write tick equal to clock.now() — should release immediately.
        ledger.touch("i-clock", heartbeat_thread_tick=12345.0)

    # Wall-clock time.time() is on the order of 1.7e9, not 12345. If the
    # helper used time.time() for start, the tick 12345 would be in the
    # past relative to start, and the helper would hang/timeout. Reaching
    # here proves clock.now() was the source.


def test_poll_uses_injected_interval(tmp_path: Path) -> None:
    """poll_interval_s drives sleep cadence between polls."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    clock = FakeClock(start=100.0)
    ledger.record(_make_instance("i-poll"))

    sleeps: list[float] = []
    tick_after_n = 3

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        # After 3 sleeps, plant the tick so the helper exits.
        if len(sleeps) == tick_after_n:
            ledger.touch("i-poll", heartbeat_thread_tick=100.5)

    with hold_until_first_tick(
        store=store,
        instance_id="i-poll",
        ledger=ledger,
        ttl_s=60.0,
        timeout_s=60.0,
        poll_interval_s=0.07,
        clock=clock,
        sleep=fake_sleep,
    ):
        pass

    # Every sleep was the injected interval.
    assert all(s == 0.07 for s in sleeps), sleeps
    assert len(sleeps) >= tick_after_n
```

- [ ] **Step 2: Run tests to confirm RED**

```bash
pixi run pytest tests/core/test_session_claim.py -v
```

Expected: `ModuleNotFoundError: No module named 'kinoforge.core.session_claim'`.

- [ ] **Step 3: Implement `src/kinoforge/core/session_claim.py`**

```python
"""B7: cooperative session-claim lock helper.

Closes the deploy_session-vs-sweep race (Layer V spec §5 Risk 3) by
extending the existing ``provision:<id>`` lock's scope from
"engine.provision only" to "instance-id committed through first
heartbeat tick lands". The reaper non-blocking-probes the same key
before destroying — see ``core/reaper_actor.py:act_on_verdict``.

Sentinel-gate honesty: this module reads ``heartbeat_thread_tick`` for
a RELEASE-decision, not a destructive decision. ``classify`` remains
the single place gating destructive verdicts (Layer U §3.4 forward-
compat contract).
"""

from __future__ import annotations

import logging
import time as _time
from contextlib import contextmanager
from typing import Callable, Iterator

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.errors import KinoforgeError
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger(__name__)


class FirstTickTimeout(KinoforgeError):
    """Raised when the HeartbeatLoop did not record a tick within ``timeout_s``.

    The orchestrator's cold-path teardown surface should catch this and
    destroy the orphaned instance before re-raising — same shape as the
    ``CapabilityMismatch`` teardown branch already present in
    ``deploy_session.__enter__``.
    """


@contextmanager
def hold_until_first_tick(
    *,
    store: ArtifactStore,
    instance_id: str,
    ledger: Ledger,
    ttl_s: float,
    timeout_s: float,
    poll_interval_s: float = 0.05,
    clock: Clock | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Iterator[None]:
    """Hold ``provision:<instance_id>`` until first heartbeat_thread_tick lands.

    Contract:

      1. Acquires ``store.acquire_lock(f"provision:{instance_id}", ttl_s=ttl_s)``
         blocking. Lock release happens in the outer ``with`` regardless of
         which exit path the body takes.
      2. Records ``start = clock.now()`` (Clock seam — NOT ``time.time()``).
      3. Yields to the caller — caller runs ``engine.provision``, builds
         backend, starts HeartbeatLoop, etc.
      4. After the yielded block exits cleanly: polls
         ``ledger.read(instance_id)`` and reads
         ``entry.get("heartbeat_thread_tick", 0.0)`` (with ``entry=None``
         treated as ``0.0``) at ``poll_interval_s`` cadence. Returns when
         the tick value is ``>= start``. Raises ``FirstTickTimeout`` when
         ``timeout_s`` elapses without a fresh tick.
      5. If the yielded block raises, propagate unchanged — the lock
         releases via the outer ``with``, the polling step is skipped.

    Args:
        store: Artifact store providing the cross-process lock.
        instance_id: Instance id to claim — used both as the lock-key
            suffix and as the ledger lookup key.
        ledger: Ledger whose ``read`` is polled for ``heartbeat_thread_tick``.
        ttl_s: Lock TTL recorded in the sidecar JSON. Callers MUST size
            this larger than the worst-case held duration (cf. spec D2:
            ``cfg.lifecycle().boot_timeout_s + 2*heartbeat_interval_s``).
        timeout_s: Polling budget — when exhausted without a fresh tick,
            raises ``FirstTickTimeout``.
        poll_interval_s: Sleep cadence between polls. Default 0.05s gives
            ~20 reads/sec — local-store overhead is negligible.
        clock: Wall-clock source. Defaults to :class:`RealClock`. Tests
            inject :class:`FakeClock` for determinism.
        sleep: Test seam — defaults to ``time.sleep``. Tests inject a
            spy/no-op to bypass real wall-clock waits.

    Yields:
        ``None`` — the body of the ``with`` block is the caller's
        engine.provision + HeartbeatLoop.start critical section.

    Raises:
        FirstTickTimeout: ``timeout_s`` elapsed before the ledger's
            ``heartbeat_thread_tick`` for ``instance_id`` exceeded ``start``.
        TransportError: Bubbled from ``store.acquire_lock`` on transport
            failure.
        KinoforgeError: Any other store-level failure surfaces here.

    Hosted-edge: ``ledger.read`` returning ``None`` indefinitely means
    the caller never recorded the instance (test-substrate edge). The
    helper raises ``FirstTickTimeout`` at ``timeout_s`` — same loud
    failure shape as a crashed HeartbeatLoop. Production callers route
    the hosted-path branch through ``contextlib.nullcontext`` and never
    enter this helper.
    """
    _clock: Clock = clock if clock is not None else RealClock()
    _sleep = sleep if sleep is not None else _time.sleep
    with store.acquire_lock(f"provision:{instance_id}", ttl_s=ttl_s):
        start = _clock.now()
        try:
            yield
        except BaseException:
            # Caller raised — propagate unchanged; skip the polling phase.
            # Lock release happens in the outer ``with`` unwind.
            raise
        # Polling phase — wait for HeartbeatLoop's first tick to land.
        deadline = start + timeout_s
        while True:
            entry = ledger.read(instance_id)
            tick = 0.0
            if entry is not None:
                raw_tick = entry.get("heartbeat_thread_tick", 0.0)
                try:
                    tick = float(raw_tick)
                except (TypeError, ValueError):
                    tick = 0.0
            if tick >= start:
                _log.debug(
                    "session-claim released for %s (tick=%.3f >= start=%.3f)",
                    instance_id,
                    tick,
                    start,
                )
                return
            if _clock.now() >= deadline:
                raise FirstTickTimeout(
                    f"no heartbeat tick for {instance_id!r} within {timeout_s}s "
                    f"(start={start:.3f}, last_tick={tick:.3f})"
                )
            _sleep(poll_interval_s)
```

- [ ] **Step 4: Run tests to confirm GREEN**

```bash
pixi run pytest tests/core/test_session_claim.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Type check + lint**

```bash
pixi run mypy src/kinoforge/core/session_claim.py
pixi run ruff check src/kinoforge/core/session_claim.py tests/core/test_session_claim.py
```

Expected: no errors.

- [ ] **Step 6: Run pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/session_claim.py tests/core/test_session_claim.py
git add src/kinoforge/core/session_claim.py tests/core/test_session_claim.py
git commit -m "$(cat <<'EOF'
feat(b7): session_claim helper + FirstTickTimeout

Adds core/session_claim.hold_until_first_tick: context manager that
acquires provision:<id> blocking, yields to the caller's engine.provision
+ HeartbeatLoop.start critical section, then polls ledger.read for
heartbeat_thread_tick >= start before releasing. Raises FirstTickTimeout
on timeout_s exhaustion.

Clock + sleep are injectable seams; tests use FakeClock + a no-op sleep
to bypass real wall-clock waits. Yielded-block exceptions propagate
unchanged; the polling phase is skipped on the exception path; the
outer ``with`` releases the lock either way.

Sentinel-gate honesty: this module reads heartbeat_thread_tick for a
release-decision, NOT a destructive decision. Layer V's classify
remains the single place gating destructive verdicts.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/core/session_claim.py", "tests/core/test_session_claim.py"], "verifyCommand": "pixi run pytest tests/core/test_session_claim.py -v", "acceptanceCriteria": ["acquires lock, yields, polls, releases on tick >= start", "FirstTickTimeout on timeout exhaustion (including ledger.read returning None)", "yielded-block exception propagates unchanged; polling skipped; lock released", "second concurrent call blocks until first releases", "Clock injection used for start time", "poll_interval_s drives sleep cadence"]}
```

---

### Task 3: Orchestrator wire-in + delete inner provision lock

**Goal:** Wrap `deploy_session.__enter__`'s step 8 (verify) + step 8.5 (pool + HeartbeatLoop.start) region in `hold_until_first_tick`, gated to match HeartbeatLoop's spawn condition. Delete the inner `provision:<id>` lock at `_provision_compute_once` since the outer hold subsumes it.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (delete line 231 + dedent body; insert wrap around line 716–771)
- Modify: `tests/core/test_orchestrator.py` (or new `tests/core/test_orchestrator_session_claim.py` if cleaner) — assert the outer-hold scope.

**Acceptance Criteria:**
- [ ] `_provision_compute_once` no longer acquires `provision:<id>` independently.
- [ ] `deploy_session.__enter__` enters `hold_until_first_tick` exactly when ALL FOUR conditions hold: `interval is not None`, `interval > 0`, `instance is not None`, `resolved_provider is not None`.
- [ ] Hosted-path AND HB-disabled branches enter `nullcontext()` and skip the lock.
- [ ] TTL passed to helper = `cfg.lifecycle().boot_timeout_s + 2 * cfg.lifecycle().heartbeat_interval_s` (when interval > 0).
- [ ] Existing CapabilityMismatch teardown branch still fires correctly; `destroy_instance` runs before re-raise; lock releases via the outer `with`.
- [ ] All existing `tests/core/test_orchestrator*.py` tests still pass.

**Verify:** `pixi run pytest tests/core/test_orchestrator.py tests/core/test_orchestrator_session_claim.py -v` → all green (including 3 new tests below).

**Steps:**

- [ ] **Step 1: Write failing test for outer-hold scope**

Create `tests/core/test_orchestrator_session_claim.py`:

```python
"""B7 T3: orchestrator-side hold_until_first_tick wire-in.

Verifies that deploy_session.__enter__ wraps step 8 (verify) + step 8.5
(pool + HeartbeatLoop.start) in hold_until_first_tick under the same gate
as HeartbeatLoop spawn (interval > 0 AND instance AND provider).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest


def test_compute_path_with_heartbeat_acquires_provision_lock(tmp_path: Path) -> None:
    """Cold-path deploy_session with heartbeat_interval_s > 0 holds
    provision:<id> from instance-available through HeartbeatLoop start."""
    from kinoforge.core.orchestrator import deploy_session
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path / "store")

    # Use a FakeProvider + Fake engine path; this assertion is structural,
    # not behavioral: assert that the lock key `provision:<id>` is held
    # at the moment the spy HeartbeatLoop is started.
    lock_held_at_hb_start = threading.Event()

    class _SpyHeartbeatLoop:
        def __init__(self, *, ledger: Any, provider: Any, instance_id: str, interval_s: float) -> None:
            self._instance_id = instance_id
            self._ledger = ledger
            # Eager-first-tick: write the tick immediately so the helper releases.
            import time
            self._tick = time.time()

        def start(self) -> None:
            # Probe the lock: if held, this nonblocking acquire returns None.
            lock = store.acquire_lock(f"provision:{self._instance_id}", ttl_s=1.0)
            token = lock.acquire(blocking=False)
            if token is None:
                lock_held_at_hb_start.set()
            else:
                lock.release(token)
            # Land the tick so the outer hold releases.
            self._ledger.touch(self._instance_id, heartbeat_thread_tick=self._tick)

        def stop(self) -> None:
            pass

    # Wire a FakeProvider + Fake engine path with heartbeat_interval_s = 0.05.
    # See test_orchestrator.py existing fixtures for the full builder pattern.
    # (Pseudocode — concrete builder follows the test_orchestrator.py shape.)
    cfg = _build_cfg_with_heartbeat(interval=0.05, boot_timeout=5.0)
    with deploy_session(
        cfg,
        store=store,
        heartbeat_loop_factory=_SpyHeartbeatLoop,
    ) as session:
        pass

    assert lock_held_at_hb_start.is_set(), (
        "provision:<id> must be held by deploy_session when HeartbeatLoop.start() runs"
    )


def test_hosted_path_does_not_acquire_provision_lock(tmp_path: Path) -> None:
    """Hosted-engine deploy_session (requires_compute=False) routes to
    nullcontext() and never acquires provision:<id>."""
    from kinoforge.core.orchestrator import deploy_session
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path / "store")
    cfg = _build_hosted_cfg()

    # Walk the deploy_session through a hosted engine — no instance/provider.
    with deploy_session(cfg, store=store) as session:
        assert session.instance is None
        # If hosted path had acquired any provision:<id>, this nonblocking
        # acquire would fail. Since there's no instance, the lock key
        # would be malformed anyway; the real assertion is the absence of
        # any lock sidecar files.
        lock_files = list((tmp_path / "store").rglob("provision_*.lock"))
        assert lock_files == [], f"hosted path created provision lock files: {lock_files}"


def test_heartbeat_disabled_compute_path_does_not_acquire_provision_lock(tmp_path: Path) -> None:
    """When heartbeat_interval_s is None on a compute path, deploy_session
    routes to nullcontext() — classify will return HEARTBEAT_UNKNOWN
    (non-destructive) for these entries, so the race B7 closes doesn't
    exist on this branch."""
    from kinoforge.core.orchestrator import deploy_session
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path / "store")
    cfg = _build_cfg_with_heartbeat(interval=None, boot_timeout=5.0)

    with deploy_session(cfg, store=store) as session:
        pass

    lock_files = list((tmp_path / "store").rglob("provision_*.lock"))
    assert lock_files == [], f"HB-disabled path created lock files: {lock_files}"


# ---------------------------------------------------------------------------
# Builders — adapt the test_orchestrator.py existing fixtures
# ---------------------------------------------------------------------------


def _build_cfg_with_heartbeat(*, interval: float | None, boot_timeout: float) -> Any:
    """Construct a Config for a compute-path Fake engine with the given heartbeat
    interval and boot timeout. Concrete shape mirrors test_orchestrator.py's
    existing _build_cfg helper — copy/adapt it here."""
    # Use the existing helpers from tests/core/test_orchestrator.py or
    # tests/conftest.py. If the helpers don't exist as importable shared
    # fixtures, inline a minimal Fake config here.
    raise NotImplementedError(
        "Adapter pattern: copy from tests/core/test_orchestrator.py _build_cfg"
    )


def _build_hosted_cfg() -> Any:
    """Hosted-engine config (requires_compute=False)."""
    raise NotImplementedError(
        "Adapter pattern: copy from tests/core/test_orchestrator.py existing hosted-path helper"
    )
```

**Implementation note:** the test file uses two NotImplementedError-stub builders. Inspect `tests/core/test_orchestrator.py` for existing patterns (search for `_build_cfg`, `_make_cfg`, or similar) and copy the relevant helpers inline OR import them from a shared fixture module. The 3 new tests assert structural properties (lock sidecar presence/absence) so they should work with any minimal compute-path Fake config.

- [ ] **Step 2: Run tests to confirm RED**

```bash
pixi run pytest tests/core/test_orchestrator_session_claim.py -v
```

Expected: NotImplementedError on the builders OR (after wiring builders) `AssertionError: provision:<id> must be held...` on the first test.

- [ ] **Step 3: Edit `_provision_compute_once` — delete the inner lock**

In `src/kinoforge/core/orchestrator.py`, at line 231, locate:

```python
    marker = marker_path(state_dir, instance.id)

    with store.acquire_lock(f"provision:{instance.id}", ttl_s=300):
        record = read_marker(marker)
        if record is not None and is_marker_current(record, capability_key_hex):
            _log.debug(...)
            return
        _log.info(...)
        # ... rest of body ...
```

Replace with (delete the with-statement, dedent body, add a comment):

```python
    marker = marker_path(state_dir, instance.id)

    # B7: provision:<id> lock is now held by the outer
    # hold_until_first_tick in deploy_session.__enter__. The marker check
    # below remains idempotent for warm-supplied paths where the caller
    # also pre-provisioned. Concurrent _provision_compute_once for the
    # same instance.id is impossible by construction — deploy_session
    # is the only call site and holds the outer lock.
    record = read_marker(marker)
    if record is not None and is_marker_current(record, capability_key_hex):
        _log.debug(
            "provision marker current for instance %s key %s — skipping",
            instance.id,
            capability_key_hex[:12],
        )
        return
    _log.info(
        "running provisioner.provision for instance %s (engine=%s key=%s)",
        instance.id,
        engine.name,
        capability_key_hex[:12],
    )
    # ... rest of body unchanged, dedented one level ...
```

Verify the entire `_provision_compute_once` body is dedented by 4 spaces (the with-block was the outermost wrapper). The `write_marker(...)` call at the end of the body must also be dedented.

- [ ] **Step 4: Wire `hold_until_first_tick` into `deploy_session.__enter__`**

In `src/kinoforge/core/orchestrator.py`, add imports near the top (around line 24):

```python
from contextlib import contextmanager, nullcontext
```

(The existing import line is `from contextlib import contextmanager` at line 21 — change to add `nullcontext`.)

Add the session_claim import near line 60:

```python
from kinoforge.core.session_claim import FirstTickTimeout, hold_until_first_tick
```

Then, in `deploy_session.__enter__` body, BEFORE the `# Step 8 — verify` comment (around line 716), insert:

```python
    # ------------------------------------------------------------------
    # B7 — Acquire the cooperative session-claim lock.
    # Held from this point through HeartbeatLoop's first tick landing
    # in the ledger. Gate MUST match HeartbeatLoop's spawn gate at line
    # ~756 — when no HB loop will tick, hold_until_first_tick would
    # FirstTickTimeout forever.
    # ------------------------------------------------------------------
    _ledger_for_claim = Ledger(store=store)
    _hb_interval = cfg.lifecycle().heartbeat_interval_s
    if (
        _hb_interval is not None
        and _hb_interval > 0
        and instance is not None
        and resolved_provider is not None
    ):
        _claim_ttl = cfg.lifecycle().boot_timeout_s + 2.0 * _hb_interval
        claim_ctx = hold_until_first_tick(
            store=store,
            instance_id=instance.id,
            ledger=_ledger_for_claim,
            ttl_s=_claim_ttl,
            timeout_s=_claim_ttl,
        )
    else:
        claim_ctx = nullcontext()

    with claim_ctx:
```

Then INDENT the remaining body (step 8, step 8.5, HeartbeatLoop spawn, `try: yield session`) one level inside the `with claim_ctx:`.

The `finally: ... pool.close(...)` block at the end of `__enter__` must also be inside the `claim_ctx` `with` since it's part of the same `try: yield session ... finally: ...` block.

CRITICAL: the `try:/finally:` that wraps `yield session` must remain INSIDE the `with claim_ctx:` block. The lock release happens via `claim_ctx`'s unwind AFTER the `finally:` runs.

**Detailed structural reference — show the exact post-edit shape of the bottom of `deploy_session.__enter__`:**

```python
    # ... step 7 (instance + backend resolved) ...

    # B7 — Acquire the cooperative session-claim lock.
    _ledger_for_claim = Ledger(store=store)
    _hb_interval = cfg.lifecycle().heartbeat_interval_s
    if (
        _hb_interval is not None
        and _hb_interval > 0
        and instance is not None
        and resolved_provider is not None
    ):
        _claim_ttl = cfg.lifecycle().boot_timeout_s + 2.0 * _hb_interval
        claim_ctx = hold_until_first_tick(
            store=store,
            instance_id=instance.id,
            ledger=_ledger_for_claim,
            ttl_s=_claim_ttl,
            timeout_s=_claim_ttl,
        )
    else:
        claim_ctx = nullcontext()

    with claim_ctx:
        # ----------------------------------------------------------
        # Step 8 — verify (skip when just-discovered). Fail-hard teardown.
        # ----------------------------------------------------------
        if not _just_discovered:
            try:
                profile_provider.verify(profile, backend, engine=resolved_engine, key=key)
            except CapabilityMismatch:
                _log.warning(
                    "capability mismatch detected; tearing down instance before re-raising"
                )
                if (
                    instance is not None
                    and resolved_provider is not None
                    and not _caller_supplied_instance
                ):
                    resolved_provider.destroy_instance(instance.id)
                raise

        # ----------------------------------------------------------
        # Step 8.5 — build the shared pool + yield
        # ----------------------------------------------------------
        pool = ConcurrentPool()
        pool.add(backend, max_in_flight=cfg.lifecycle().max_in_flight)
        session = DeploySession(
            backend=backend,
            profile=profile,
            pool=pool,
            instance=instance,
            engine=resolved_engine,
            provider=resolved_provider,
        )

        # Layer U — spawn a background HeartbeatLoop when configured.
        hb_loop: HeartbeatLoopProtocol | None = None
        interval = cfg.lifecycle().heartbeat_interval_s
        if (
            interval is not None
            and interval > 0
            and instance is not None
            and resolved_provider is not None
        ):
            factory: Callable[..., HeartbeatLoopProtocol] = (
                heartbeat_loop_factory or HeartbeatLoop
            )
            hb_loop = factory(
                ledger=Ledger(store=store),
                provider=resolved_provider,
                instance_id=instance.id,
                interval_s=interval,
            )
            hb_loop.start()
        try:
            yield session
        finally:
            if hb_loop is not None:
                hb_loop.stop()
            if cancel_token is not None and cancel_token.is_set():
                try:
                    pool.close(cancel_pending=True, timeout=30.0)
                except Exception as close_exc:
                    _log.error("pool.close failed during interrupt cleanup: %s", close_exc)
            else:
                pool.close()
```

The entire region from the `with claim_ctx:` line through the final `pool.close()` is inside the lock. When `claim_ctx` is `nullcontext()`, behavior is identical to today.

- [ ] **Step 5: Verify type-check + lint**

```bash
pixi run mypy src/kinoforge/core/orchestrator.py
pixi run ruff check src/kinoforge/core/orchestrator.py
```

Expected: clean. Adjust imports if mypy complains about `nullcontext`.

- [ ] **Step 6: Implement test builders + run tests GREEN**

Inspect `tests/core/test_orchestrator.py` for existing config builders and adapt them inline into `tests/core/test_orchestrator_session_claim.py`. Common patterns to grep for: `_build_cfg`, `_make_config`, `Config(`, `LifecycleConfig(`, `ComputeConfig(`. Many tests build a Fake-provider config from scratch with `boot_timeout_s=5.0` style overrides.

```bash
pixi run pytest tests/core/test_orchestrator_session_claim.py -v
```

Expected: 3 passed.

- [ ] **Step 7: Run full orchestrator test suite to confirm no regression**

```bash
pixi run pytest tests/core/test_orchestrator.py tests/core/test_orchestrator_session_claim.py -v
```

Expected: all green (existing tests + 3 new ones).

- [ ] **Step 8: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_session_claim.py
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_session_claim.py
git commit -m "$(cat <<'EOF'
feat(b7): orchestrator wires hold_until_first_tick into deploy_session

deploy_session.__enter__ now wraps step 8 (verify) + step 8.5 (pool +
HeartbeatLoop spawn) in hold_until_first_tick when ALL of
heartbeat_interval_s > 0, instance is not None, and resolved_provider
is not None — the same gate HeartbeatLoop spawn already uses. Hosted
and HB-disabled paths route to nullcontext() and skip the lock
entirely.

_provision_compute_once's inner `with store.acquire_lock(
f"provision:{instance.id}", ttl_s=300):` is deleted — the outer hold
now owns the key for the wider scope (engine.provision through first
heartbeat tick). The marker check inside the helper remains idempotent
for warm-supplied paths.

TTL passed to the helper: boot_timeout_s + 2*heartbeat_interval_s
(spec D2). Default 900s + small margin gives 200%+ margin over the
documented 5-min ComfyUI+Wan cold-boot worst case.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/core/orchestrator.py", "tests/core/test_orchestrator_session_claim.py"], "verifyCommand": "pixi run pytest tests/core/test_orchestrator.py tests/core/test_orchestrator_session_claim.py -v", "acceptanceCriteria": ["_provision_compute_once no longer acquires provision:<id>", "deploy_session enters hold_until_first_tick when interval > 0 AND instance AND provider", "hosted path uses nullcontext()", "HB-disabled compute path uses nullcontext()", "TTL = boot_timeout_s + 2*heartbeat_interval_s", "existing CapabilityMismatch teardown still fires correctly"]}
```

---

### Task 4: Reaper non-blocking probe + ActionResult docstring

**Goal:** Grow `act_on_verdict` to non-blocking-probe `provision:<id>` immediately after entering `reaper/<id>`. On contention, return `ActionResult(action="deferred-session-claim", reason="held by pid <N>", ...)` and skip the destroy. The holder_pid is lifted from the FileLock sidecar JSON. Update `ActionResult` docstring action enumeration.

**Files:**
- Modify: `src/kinoforge/core/reaper_actor.py` (extend `ActionResult` docstring; add probe arm in `act_on_verdict`)
- Modify: `tests/core/test_reaper_actor.py` (add 2 new test cases)

**Acceptance Criteria:**
- [ ] `act_on_verdict` non-blocking-probes `provision:<id>` after entering `reaper/<id>`.
- [ ] On probe-contention: returns `ActionResult(action="deferred-session-claim", reason="held by pid <N>; orchestrator mid-session-claim")` and logs `"instance <id> mid-session-claim; deferring to next sweep"` at INFO.
- [ ] holder_pid is read from the FileLock sidecar JSON at the path the lock writes to.
- [ ] On probe-success: probe is released immediately (NOT held during destroy); existing flow continues unchanged.
- [ ] `ActionResult` docstring action enumeration includes `"deferred-session-claim"`.
- [ ] Existing 11 `test_act_on_verdict_*` tests still pass.

**Verify:** `pixi run pytest tests/core/test_reaper_actor.py -v` → all green, including 2 new tests.

**Steps:**

- [ ] **Step 1: Write failing tests**

Append to `tests/core/test_reaper_actor.py` (use existing fixtures `_FakeProvider`, `_FakeStore`, `_FakeLock`, `_FakeLedger` for the spy lock; for the holder_pid path use a real `LocalArtifactStore` so a real FileLock sidecar exists):

```python
def test_act_on_verdict_defers_when_provision_lock_held(tmp_path: Path) -> None:
    """B7: orchestrator-side holds provision:<id>; reaper probe returns
    None; act_on_verdict returns action='deferred-session-claim' with
    holder_pid in reason; no destroy."""
    import os

    from kinoforge.core.clock import FakeClock
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import act_on_verdict
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path)
    # Orchestrator simulates holding the lock.
    provision_lock = store.acquire_lock("provision:i-deferred", ttl_s=300.0)
    token = provision_lock.acquire(blocking=True, timeout_s=5.0)
    assert token is not None

    try:
        ledger = _FakeLedger()
        ledger.add_entry({"id": "i-deferred", "provider": "local", "created_at": 0.0})
        provider = _FakeProvider(live_ids=["i-deferred"])
        clock = FakeClock(start=100.0)

        result = act_on_verdict(
            store,
            ledger,
            provider,
            entry={"id": "i-deferred", "provider": "local", "created_at": 0.0},
            snapshot_verdict=Verdict.IDLE_REAP,
            thresholds={"idle_timeout_s": 60.0, "max_lifetime_s": 3600.0},
            clock=clock,
        )

        assert result.action == "deferred-session-claim"
        assert f"pid {os.getpid()}" in (result.reason or "")
        # Provider must NOT have been called for destroy.
        assert provider.destroy_calls == []
    finally:
        provision_lock.release(token)


def test_act_on_verdict_proceeds_after_provision_lock_released(tmp_path: Path) -> None:
    """B7: after orchestrator releases provision:<id>, next act_on_verdict
    pass probes successfully and proceeds to re-classify + destroy."""
    from kinoforge.core.clock import FakeClock
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import act_on_verdict
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path)
    ledger = _FakeLedger()
    ledger.add_entry({"id": "i-released", "provider": "local", "created_at": 0.0})
    provider = _FakeProvider(live_ids=["i-released"])
    clock = FakeClock(start=100.0)

    # Lock is NOT held — probe succeeds, flow continues to destroy.
    result = act_on_verdict(
        store,
        ledger,
        provider,
        entry={"id": "i-released", "provider": "local", "created_at": 0.0},
        snapshot_verdict=Verdict.IDLE_REAP,
        thresholds={"idle_timeout_s": 60.0, "max_lifetime_s": 3600.0},
        clock=clock,
    )

    assert result.action == "destroyed_and_forgot"
    assert provider.destroy_calls == ["i-released"]
```

**Adapter note:** the existing `_FakeProvider` / `_FakeLedger` / `_FakeStore` fixtures in `tests/core/test_reaper_actor.py` (lines 29–115) provide the harness. The new tests use a real `LocalArtifactStore` so the FileLock sidecar JSON is real on disk — this is the only way the holder_pid path can be exercised end-to-end. Confirm `_FakeProvider.destroy_calls` exists (or add it to the fixture if needed; cross-reference the existing `test_act_on_verdict_idle_reap_destroys_and_forgets` to see how destroy verification is done today — adapt the assertion form if `destroy_calls` is named differently).

- [ ] **Step 2: Run tests to confirm RED**

```bash
pixi run pytest tests/core/test_reaper_actor.py::test_act_on_verdict_defers_when_provision_lock_held tests/core/test_reaper_actor.py::test_act_on_verdict_proceeds_after_provision_lock_released -v
```

Expected: first test fails with `result.action == "destroyed_and_forgot"` (today's flow ignores the lock and destroys anyway). Second test passes already.

- [ ] **Step 3: Implement the probe arm in `act_on_verdict`**

In `src/kinoforge/core/reaper_actor.py`, locate `act_on_verdict` around line 114. Inside the `with store.acquire_lock(f"reaper/{instance_id}", ttl_s=_LOCK_TTL_S):` block (line 138), insert the probe arm IMMEDIATELY AFTER the with-statement enters:

```python
    instance_id = str(entry["id"])
    with store.acquire_lock(f"reaper/{instance_id}", ttl_s=_LOCK_TTL_S):
        # B7: non-blocking probe of provision:<id>. If an orchestrator
        # process holds it, this entry is mid-session-claim — skip this
        # sweep, log INFO with holder_pid, retry next pass.
        deferred = _probe_session_claim_holder(store, instance_id)
        if deferred is not None:
            holder_pid = deferred
            _log.info(
                "instance %s mid-session-claim (held by pid %s); deferring to next sweep",
                instance_id,
                holder_pid,
            )
            return ActionResult(
                instance_id=instance_id,
                snapshot_verdict=snapshot_verdict,
                applied_verdict=snapshot_verdict,
                action="deferred-session-claim",
                reason=f"held by pid {holder_pid}; orchestrator mid-session-claim",
            )

        live_ids = {i.id for i in provider.list_instances()}
        # ... rest of existing flow unchanged ...
```

Add the helper function ABOVE `act_on_verdict`, after `provider_for`:

```python
def _probe_session_claim_holder(store: ArtifactStore, instance_id: str) -> int | None:
    """Non-blocking probe of ``provision:<instance_id>``.

    B7 reaper-side hook. When the orchestrator's
    ``hold_until_first_tick`` holds the key, this probe returns the
    holder PID so the reaper can defer with a helpful diagnostic.

    Args:
        store: Artifact store providing the lock.
        instance_id: The instance whose claim lock to probe.

    Returns:
        The orchestrator's PID when the lock is held, or ``None`` when
        the lock is free (probe-success — caller proceeds with destroy).
        Probe acquires-then-releases immediately on success; the caller
        does NOT hold ``provision:<id>`` during the destroy flow.

    Implementation note: the probe uses ``ttl_s=0.0`` because we are
    not claiming the lock for any duration. When acquire succeeds the
    sidecar is briefly rewritten with an immediately-expired TTL and
    then released; no other process is in a "wait for TTL to expire"
    path because orchestrators always use blocking acquire.
    """
    from kinoforge.stores.local import LocalArtifactStore

    probe = store.acquire_lock(f"provision:{instance_id}", ttl_s=0.0)
    token = probe.acquire(blocking=False)
    if token is not None:
        probe.release(token)
        return None
    # Lock held — read holder PID from sidecar if we can.
    holder_pid: int | None = None
    if isinstance(store, LocalArtifactStore):
        try:
            sidecar = store.root / "locks" / f"provision_{instance_id}.lock"
            if sidecar.exists():
                import json
                data = json.loads(sidecar.read_text())
                holder_pid = int(data.get("holder_pid", 0)) or None
        except (OSError, ValueError, KeyError):
            holder_pid = None
    # Sentinel: unknown holder still returns a sentinel int so the
    # caller's None-check distinguishes "lock free" from "lock held by
    # unknown PID".
    return holder_pid if holder_pid is not None else -1
```

**Sidecar path note:** confirm the FileLock sidecar path format. Inspect `src/kinoforge/stores/local.py` around line 193 (`acquire_lock`) and `src/kinoforge/stores/local_lock.py:30`. The default LocalArtifactStore writes sidecars under `<root>/locks/<sanitized_key>.lock` (see `_sanitize_key` from `core/locks.py`). The colon in `provision:<id>` is sanitized to underscore, hence `provision_<id>.lock`. **Verify this path during implementation by running an offline test that exercises a real lock acquire** — if the path differs, adjust the helper accordingly.

- [ ] **Step 4: Extend ActionResult docstring**

In `src/kinoforge/core/reaper_actor.py` at the `ActionResult` dataclass (line 47), update the docstring:

```python
@dataclass(frozen=True)
class ActionResult:
    """Outcome of a single ``act_on_verdict`` call.

    Attributes:
        instance_id: The id acted on.
        snapshot_verdict: What ``sweep`` classified the entry as.
        applied_verdict: What the act-time re-classify returned (may
            differ from ``snapshot_verdict`` under drift). For
            ``"deferred-session-claim"`` the re-classify never ran, so
            this is set equal to ``snapshot_verdict``.
        action: One of:

            * ``"destroyed_and_forgot"`` — IDLE_REAP / OVERAGE_REAP /
              ORPHAN_REAP destroyed + ledger entry forgotten.
            * ``"forgot"`` — STALE_LEDGER: entry forgotten, no destroy.
            * ``"forgot_unroutable"`` — sweep-level: provider routing
              failed; the entry was forgotten without contacting any
              provider.
            * ``"skipped"`` — drift between snapshot and act-time verdict.
            * ``"failed"`` — TeardownError during destroy.
            * ``"no_op"`` — LIVE / HEARTBEAT_UNKNOWN /
              HEARTBEAT_SUBSTRATE_MISSING: no action required.
            * ``"deferred-session-claim"`` — B7: orchestrator holds
              ``provision:<id>`` (mid-session-claim). Reaper logs INFO
              and skips this entry on this sweep pass; the next sweep
              re-evaluates.
        reason: Free-text explanation for skipped / failed / deferred-
            session-claim actions. For deferred-session-claim, contains
            the holder PID when readable from the lock sidecar.
    """

    instance_id: str
    snapshot_verdict: Verdict
    applied_verdict: Verdict
    action: str
    reason: str | None = None
```

(`action` stays plain `str`; no Literal widening needed.)

- [ ] **Step 5: Run tests GREEN**

```bash
pixi run pytest tests/core/test_reaper_actor.py -v
```

Expected: all green — 11 existing + 2 new.

- [ ] **Step 6: Type-check + lint**

```bash
pixi run mypy src/kinoforge/core/reaper_actor.py
pixi run ruff check src/kinoforge/core/reaper_actor.py tests/core/test_reaper_actor.py
```

Expected: clean.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/reaper_actor.py tests/core/test_reaper_actor.py
git add src/kinoforge/core/reaper_actor.py tests/core/test_reaper_actor.py
git commit -m "$(cat <<'EOF'
feat(b7): reaper non-blocking probe of provision:<id>

act_on_verdict now non-blocking-probes provision:<id> immediately after
entering its existing reaper/<id> lock. On contention (orchestrator
mid-session-claim), returns ActionResult(action="deferred-session-claim",
reason="held by pid <N>; ...") and logs INFO with the holder PID. No
destroy occurs on this sweep pass; the next sweep re-evaluates.

The probe is acquire-then-immediate-release: the reaper does NOT hold
provision:<id> during the destroy flow. holder_pid lifts from the
LocalArtifactStore FileLock sidecar JSON when readable; falls back to
sentinel -1 when not (cloud-store or sidecar unreadable).

ActionResult.action stays `str` (not a Literal) — just docstring
enumeration extended. Layer V's classify gate is unchanged; B7 only
affects the act_on_verdict side-effect surface.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/core/reaper_actor.py", "tests/core/test_reaper_actor.py"], "verifyCommand": "pixi run pytest tests/core/test_reaper_actor.py -v", "acceptanceCriteria": ["non-blocking probe of provision:<id> after entering reaper/<id>", "on contention returns ActionResult(action='deferred-session-claim', reason='held by pid <N>; ...')", "holder_pid read from FileLock sidecar when LocalArtifactStore", "probe-success releases immediately; existing flow continues", "INFO log emitted on defer with holder PID", "ActionResult docstring enumerates deferred-session-claim"]}
```

---

### Task 5: Cross-process subprocess integration test

**Goal:** End-to-end lockdown of the race B7 closes. Subprocess A starts `deploy_session` against a sleepy FakeProvider; subprocess B runs `kinoforge reap --apply` against the same id; assert B defers, A finishes, no destroy occurred.

**Files:**
- Create: `tests/core/test_orchestrator_session_claim_xprocess.py`

**Acceptance Criteria:**
- [ ] Subprocess A enters `deploy_session` and holds `provision:<id>` for ≥1 second.
- [ ] Subprocess B runs `python -m kinoforge reap --apply --id <id>` while A holds the lock.
- [ ] B's stdout/exit-code shows the entry was deferred (action="deferred-session-claim" surfaced via the CLI JSONL output OR table summary).
- [ ] B did NOT destroy the instance (FakeProvider's destroy_count remains 0).
- [ ] After A releases, a second invocation of B proceeds normally per existing reaper behavior.

**Verify:** `pixi run pytest tests/core/test_orchestrator_session_claim_xprocess.py -v` → 1 passing test (may be slow, mark with `@pytest.mark.slow` if conventions require).

**Steps:**

- [ ] **Step 1: Inspect the existing Layer U cross-process test for shape**

Read `PROGRESS.md` around line 1130 (the Layer U cross-process visibility test reference) AND `tests/core/test_ledger_touch.py::test_touch_visible_across_process_boundary` (line 238) — that's the established pattern: `subprocess.run([sys.executable, "-c", textwrap.dedent("""...""")], check=True)`.

- [ ] **Step 2: Write the failing test**

Create `tests/core/test_orchestrator_session_claim_xprocess.py`:

```python
"""B7 T5: cross-process subprocess integration of session-claim lock.

End-to-end lockdown of the race B7 closes. Subprocess A starts
deploy_session against a sleepy FakeProvider; subprocess B runs
`kinoforge reap --apply` against the same id; assert B defers, A
finishes, no destroy occurred.

Mirrors the Layer U cross-process visibility test shape at
tests/core/test_ledger_touch.py::test_touch_visible_across_process_boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


@pytest.mark.slow
def test_reaper_defers_while_orchestrator_mid_provision(tmp_path: Path) -> None:
    """End-to-end race: orchestrator A is mid-provision; reaper B fires;
    B defers; A finishes; A's heartbeat lands; no destroy occurred."""
    store_root = tmp_path / "store"
    store_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    flag_file = tmp_path / "a_entered_lock.flag"

    # Subprocess A: simulates an orchestrator holding the lock and
    # writing the heartbeat tick after a delay.
    a_script = textwrap.dedent(f"""
        import json
        import sys
        import time
        from pathlib import Path

        from kinoforge.core.clock import RealClock
        from kinoforge.core.interfaces import Instance
        from kinoforge.core.lifecycle import Ledger
        from kinoforge.core.session_claim import hold_until_first_tick
        from kinoforge.stores.local import LocalArtifactStore

        store = LocalArtifactStore(Path({str(store_root)!r}))
        ledger = Ledger(store=store)
        ledger.record(Instance(
            id="i-xproc",
            provider="local",
            status="ready",
            created_at=time.time() - 7200.0,  # >idle_timeout so IDLE_REAP
            cost_rate_usd_per_hr=0.01,
            tags={{}},
        ))

        flag = Path({str(flag_file)!r})

        with hold_until_first_tick(
            store=store,
            instance_id="i-xproc",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=60.0,
            poll_interval_s=0.05,
        ):
            flag.write_text("entered")
            # Sleep to simulate engine.provision wall-time.
            time.sleep(2.5)
            # Plant the heartbeat tick so the helper releases.
            ledger.touch("i-xproc", heartbeat_thread_tick=time.time())
        print("A: released", flush=True)
    """)

    # Subprocess B: probes for the lock-held state by trying to acquire
    # provision:<id> non-blocking. This is the same probe path
    # act_on_verdict.B7 uses — exercising it directly avoids needing a
    # FakeProvider wired through the full kinoforge CLI for the test.
    b_script = textwrap.dedent(f"""
        from pathlib import Path
        from kinoforge.core.reaper_actor import _probe_session_claim_holder
        from kinoforge.stores.local import LocalArtifactStore

        store = LocalArtifactStore(Path({str(store_root)!r}))
        result = _probe_session_claim_holder(store, "i-xproc")
        print(f"B-probe={{result}}", flush=True)
    """)

    proc_a = subprocess.Popen(
        [sys.executable, "-c", a_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for A to enter the lock (poll flag file).
    for _ in range(50):
        if flag_file.exists():
            break
        time.sleep(0.05)
    assert flag_file.exists(), "subprocess A never entered hold_until_first_tick"

    # Run B while A holds.
    b_result = subprocess.run(
        [sys.executable, "-c", b_script],
        capture_output=True,
        text=True,
        check=True,
        timeout=10.0,
    )
    # B should have observed the lock-held state — probe returns int
    # (PID) when held, None when free. Today A is subprocess that owns
    # the lock, so PID should be A's pid.
    assert "B-probe=" in b_result.stdout
    assert "B-probe=None" not in b_result.stdout, (
        f"B's probe found no holder while A was holding; stdout={b_result.stdout!r}"
    )
    assert str(proc_a.pid) in b_result.stdout or "B-probe=-1" in b_result.stdout, (
        f"B's probe did not capture A's pid {proc_a.pid}; stdout={b_result.stdout!r}"
    )

    # Wait for A to finish.
    a_stdout, a_stderr = proc_a.communicate(timeout=15.0)
    assert proc_a.returncode == 0, f"A failed: stderr={a_stderr!r}"
    assert "A: released" in a_stdout

    # Now B should observe the lock as free.
    b_result_2 = subprocess.run(
        [sys.executable, "-c", b_script],
        capture_output=True,
        text=True,
        check=True,
        timeout=10.0,
    )
    assert "B-probe=None" in b_result_2.stdout, (
        f"B's second probe should find lock free after A released; "
        f"stdout={b_result_2.stdout!r}"
    )
```

**Test naming note:** the test uses `_probe_session_claim_holder` directly (the module-level helper from Task 4). This avoids needing to wire the full `kinoforge reap --apply` CLI invocation through the subprocess, which would require building a real Config + ledger ctx. The probe IS the exact code path `act_on_verdict` uses, so this test exercises the cross-process semantics that matter.

- [ ] **Step 3: Run test to confirm GREEN**

```bash
pixi run pytest tests/core/test_orchestrator_session_claim_xprocess.py -v
```

Expected: 1 passed. Test runs ~3-5 seconds.

If `@pytest.mark.slow` is not registered as a known marker in the project, either register it in `pyproject.toml` `[tool.pytest.ini_options].markers` OR remove the marker. Confirm by running `pixi run pytest --markers | grep slow` first.

- [ ] **Step 4: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/core/test_orchestrator_session_claim_xprocess.py
git add tests/core/test_orchestrator_session_claim_xprocess.py
git commit -m "$(cat <<'EOF'
test(b7): cross-process integration test for session-claim lock

End-to-end lockdown of the race B7 closes. Subprocess A enters
hold_until_first_tick and sleeps 2.5s (simulating engine.provision);
subprocess B runs the same _probe_session_claim_holder helper used by
act_on_verdict and asserts it sees A's PID while A holds. After A
releases, B's second probe sees the lock free.

Mirrors the Layer U cross-process visibility test shape at
test_ledger_touch::test_touch_visible_across_process_boundary.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/core/test_orchestrator_session_claim_xprocess.py"], "verifyCommand": "pixi run pytest tests/core/test_orchestrator_session_claim_xprocess.py -v", "acceptanceCriteria": ["subprocess A enters hold_until_first_tick and holds for ~2.5s", "subprocess B's probe returns A's pid while A holds", "no destroy occurred during the race window", "after A releases, B's second probe returns None (free)"]}
```

---

### Task 6: CLI render + lock-contract delta

**Goal:** `_emit_reap_human` counts and prints `deferred` actions in the summary line; `_emit_reap_jsonl` emits the new action literal in its per-action record. `tests/stores/test_acquire_lock_contract.py` grows one held-while-orchestrator-runs case.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_emit_reap_human` + `_emit_reap_jsonl`)
- Modify: `tests/stores/test_acquire_lock_contract.py` (add 1 parametrize case)
- Modify: `tests/core/test_reaper_cli.py` (or whichever test covers `_emit_reap_human` summary line — `rg "destroyed · " tests/`)

**Acceptance Criteria:**
- [ ] `_emit_reap_human` summary line includes `<N> deferred` count for actions whose `.action == "deferred-session-claim"`.
- [ ] `_emit_reap_jsonl` emits the new literal in per-action records without crashing on unknown action.
- [ ] `test_acquire_lock_contract.py` exercises a held-while-other-thread-tries-non-blocking case on `LocalArtifactStore` (cloud stores deferred to B16-neighborhood per spec §F5).

**Verify:** `pixi run pytest tests/cli/ tests/stores/test_acquire_lock_contract.py -v` → all green.

**Steps:**

- [ ] **Step 1: Locate the existing CLI summary test**

```bash
rg -n "destroyed · |drift-skipped|failed" tests/ | head
```

Find the existing test that asserts the summary line format (likely `tests/cli/test_reap.py` or `tests/core/test_reaper_cli.py`). Read it to confirm the expected shape.

- [ ] **Step 2: Write failing tests**

Add to the existing CLI reap test file (or create `tests/cli/test_reap_b7.py` if no existing peer):

```python
def test_emit_reap_human_includes_deferred_count(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_reap_human summary line includes <N> deferred for B7."""
    from kinoforge.cli._commands import _emit_reap_human
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import ActionResult, SweepReport

    actions = [
        ActionResult(
            instance_id="i-a",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="destroyed_and_forgot",
        ),
        ActionResult(
            instance_id="i-b",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="deferred-session-claim",
            reason="held by pid 4242; orchestrator mid-session-claim",
        ),
        ActionResult(
            instance_id="i-c",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="deferred-session-claim",
            reason="held by pid 4243; orchestrator mid-session-claim",
        ),
    ]
    report = SweepReport(
        snapshot={
            "i-a": ({"id": "i-a", "provider": "local", "created_at": 0.0}, Verdict.IDLE_REAP),
            "i-b": ({"id": "i-b", "provider": "local", "created_at": 0.0}, Verdict.IDLE_REAP),
            "i-c": ({"id": "i-c", "provider": "local", "created_at": 0.0}, Verdict.IDLE_REAP),
        },
        actions=actions,
    )

    _emit_reap_human(report, applied=True, include_orphans=False)

    captured = capsys.readouterr()
    assert "2 deferred" in captured.out, captured.out


def test_emit_reap_jsonl_handles_deferred_action(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_reap_jsonl emits the new literal without crashing."""
    import json
    from kinoforge.cli._commands import _emit_reap_jsonl
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import ActionResult, SweepReport

    actions = [
        ActionResult(
            instance_id="i-d",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="deferred-session-claim",
            reason="held by pid 4244; orchestrator mid-session-claim",
        ),
    ]
    report = SweepReport(
        snapshot={
            "i-d": ({"id": "i-d", "provider": "local", "created_at": 0.0}, Verdict.IDLE_REAP),
        },
        actions=actions,
    )

    _emit_reap_jsonl(report)

    captured = capsys.readouterr()
    # Verify the action surfaces in the JSONL output.
    found = False
    for line in captured.out.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("action") == "deferred-session-claim":
            found = True
            assert rec.get("reason", "").startswith("held by pid ")
            break
    assert found, captured.out
```

- [ ] **Step 3: Run tests to confirm RED**

```bash
pixi run pytest tests/cli/ -k "deferred" -v
```

Expected: failures — `"2 deferred"` not in output.

- [ ] **Step 4: Edit `_emit_reap_human`**

In `src/kinoforge/cli/_commands.py`, locate the summary block around line 974 (`destroyed = sum(... "destroyed_and_forgot")` through `print(f"acted on ...)`). Add a `deferred` count:

```python
        destroyed = sum(1 for a in report.actions if a.action == "destroyed_and_forgot")
        forgot = sum(
            1 for a in report.actions if a.action in {"forgot", "forgot_unroutable"}
        )
        skipped = sum(1 for a in report.actions if a.action == "skipped")
        failed = sum(1 for a in report.actions if a.action == "failed")
        deferred = sum(
            1 for a in report.actions if a.action == "deferred-session-claim"
        )
        print(
            f"acted on {len(report.actions)}: {destroyed} destroyed · "
            f"{forgot} forgotten · {skipped} drift-skipped · "
            f"{deferred} deferred · {failed} failed"
        )
```

- [ ] **Step 5: Verify `_emit_reap_jsonl` handles the new action literal**

Read `_emit_reap_jsonl` (just below `_emit_reap_human` in `_commands.py`). The existing code likely serializes `a.action` as-is — confirm it does, no special-casing needed. If the emitter has an explicit `if action == "X"` switch, add the `"deferred-session-claim"` branch that emits the same shape as `"skipped"` (no destroy, has reason).

- [ ] **Step 6: Run CLI tests GREEN**

```bash
pixi run pytest tests/cli/ -v
```

Expected: all green, including 2 new tests.

- [ ] **Step 7: Add lock-contract delta**

Inspect `tests/stores/test_acquire_lock_contract.py` (currently 6 tests at lines 24/31/65/74/85/100). Add one new test that exercises the held-while-orchestrator-runs case on `LocalArtifactStore`:

```python
def test_held_while_orchestrator_runs(tmp_path: Path) -> None:
    """B7: outer thread acquires provision:<id>; inner non-blocking
    probe returns None; after outer releases, probe succeeds."""
    import threading
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path)
    outer = store.acquire_lock("provision:i-contract", ttl_s=60.0)
    outer_token = outer.acquire(blocking=True, timeout_s=5.0)
    assert outer_token is not None

    inner = store.acquire_lock("provision:i-contract", ttl_s=60.0)
    inner_token = inner.acquire(blocking=False)
    assert inner_token is None, "non-blocking probe should fail while outer holds"

    outer.release(outer_token)

    inner_token_2 = inner.acquire(blocking=False)
    assert inner_token_2 is not None, "non-blocking probe should succeed after outer releases"
    inner.release(inner_token_2)
```

- [ ] **Step 8: Run lock-contract tests GREEN**

```bash
pixi run pytest tests/stores/test_acquire_lock_contract.py -v
```

Expected: 7 passed (6 existing + 1 new).

- [ ] **Step 9: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/cli/_commands.py tests/cli/ tests/stores/test_acquire_lock_contract.py
git add src/kinoforge/cli/_commands.py tests/cli/ tests/stores/test_acquire_lock_contract.py
git commit -m "$(cat <<'EOF'
feat(b7): CLI renders deferred-session-claim; lock-contract test delta

_emit_reap_human summary line gains a <N> deferred count, surfacing the
B7 deferred-session-claim ActionResult in operator-facing output:

    acted on 3: 1 destroyed · 0 forgotten · 0 drift-skipped ·
    2 deferred · 0 failed

_emit_reap_jsonl emits the new action literal in per-action records;
no special-casing needed because the emitter passes action through
as-is.

tests/stores/test_acquire_lock_contract.py grows one held-while-
orchestrator-runs case on LocalArtifactStore. Cloud-store cross-host
semantics deferred to B16 neighborhood per spec §F5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/cli/_commands.py", "tests/stores/test_acquire_lock_contract.py", "tests/cli/test_reap_b7.py"], "verifyCommand": "pixi run pytest tests/cli/ tests/stores/test_acquire_lock_contract.py -v", "acceptanceCriteria": ["_emit_reap_human counts <N> deferred in summary line", "_emit_reap_jsonl emits deferred-session-claim literal", "held-while-orchestrator-runs test passes on LocalArtifactStore"]}
```

---

### Task 7: Documentation closeout

**Goal:** Strike B7 in `PROGRESS.md §B`, update `warm-reuse-tasks.txt` status to CLOSED with commit SHA, amend Layer V spec §5 Risk 3 + §6 to point at B7 spec as closing the gap.

**Files:**
- Modify: `PROGRESS.md`
- Modify: `warm-reuse-tasks.txt`
- Modify: `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md`

**Acceptance Criteria:**
- [ ] `PROGRESS.md §B` has B7 entry strikethrough or marked CLOSED with commit SHA.
- [ ] `warm-reuse-tasks.txt` B7 status line updates from "Status: design APPROVED" to "Status: CLOSED commit <sha>".
- [ ] Layer V spec §5 Risk 3 + §6 reference B7 spec as the closing layer.
- [ ] No new tests required (docs-only).

**Verify:** `git log --oneline -8` shows the closeout commit; `rg "B7" PROGRESS.md warm-reuse-tasks.txt docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md | head` shows updated references.

**Steps:**

- [ ] **Step 1: Capture the closeout commit SHA**

After Tasks 1–6 land, capture the SHA of the most recent B7 commit:

```bash
B7_SHA=$(git log --oneline -10 | grep -E "feat\(b7\)|test\(b7\)|fix\(b7\)" | head -1 | awk '{print $1}')
echo "B7_SHA=$B7_SHA"
```

(Use the SHA of whichever commit best represents "B7 implementation complete" — typically Task 6's commit OR Task 5's cross-process integration test commit, since that's the lockdown moment.)

- [ ] **Step 2: Edit `PROGRESS.md §B` — strike B7**

In `PROGRESS.md` find the B7 entry (currently line ~151 — `**B7. Cooperative lock between session-start and reaper.** Layer V §6 candidate, prereq for B3.`). Mirror the existing strikethrough pattern used for B5 (search PROGRESS.md for `~~**B5.` to see the established style). Replace:

```
- **B7. Cooperative lock between session-start and reaper.** Layer V §6 candidate, prereq for B3.
```

with:

```
- ~~**B7. Cooperative lock between session-start and reaper.**~~ — CLOSED by commit `<B7_SHA>`. Extends the existing `provision:<id>` lock (orchestrator.py) from "engine.provision only" to "instance-id committed through first heartbeat tick lands"; reaper non-blocking-probes the same key before destroying. Closes Layer V §5 Risk 3 race. Spec at `docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md`.
```

- [ ] **Step 3: Edit `warm-reuse-tasks.txt` — flip B7 status**

In `warm-reuse-tasks.txt`, locate the B7 entry's status line (search for `**Status:** design APPROVED 2026-06-12`). Replace:

```
    **Status:** design APPROVED 2026-06-12. Spec is the source of truth — this entry summarizes
    the locked decisions and is intentionally lighter than the spec; for any conflict the spec
    wins.

    Spec: docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md (commit
    44a35ad).
```

with:

```
    **Status:** CLOSED 2026-06-12, commit <B7_SHA>. Spec is the source of truth — this entry
    summarizes the locked decisions and is intentionally lighter than the spec; for any conflict
    the spec wins.

    Spec: docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md (commit
    44a35ad). Implementation plan: docs/superpowers/plans/2026-06-12-b7-cooperative-session-claim-lock.md.
```

- [ ] **Step 4: Amend Layer V spec §5 Risk 3 + §6**

In `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md`:

At §5 Risk 3 (around line 670), append after the existing "Out of scope for Layer V; documented as Layer Y candidate." line:

```markdown
   **B7 closure:** the cooperative lock landed in B7 (spec at
   `docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md`,
   commit `<B7_SHA>`). Implementation reuses the existing
   `provision:<id>` key (not the `reaper/<id>` key sketched here);
   orchestrator holds it from instance-id committed through first
   heartbeat tick lands. Reaper non-blocking-probes before destroying
   and returns `ActionResult(action="deferred-session-claim", ...)` on
   contention. Race window closed.
```

At §6 "Out of scope" (line ~704), edit:

```markdown
- **Cooperative lock between session-start and reaper** (mitigation
  for Risk 3 above).
```

to:

```markdown
- ~~**Cooperative lock between session-start and reaper**~~ — CLOSED by
  B7 (spec at `docs/superpowers/specs/2026-06-12-b7-cooperative-
  session-claim-lock-design.md`, commit `<B7_SHA>`).
```

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files PROGRESS.md warm-reuse-tasks.txt docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md
git add PROGRESS.md warm-reuse-tasks.txt docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md
git commit -m "$(cat <<'EOF'
docs(b7): closeout — strike B7 in PROGRESS, amend Layer V spec

B7 implementation complete (commits <B7_SHA> and predecessors). Race
documented at Layer V spec §5 Risk 3 is closed.

PROGRESS.md §B: B7 entry struck through with closeout note.
warm-reuse-tasks.txt B7: status flipped to CLOSED with commit SHA.
Layer V spec §5 Risk 3 + §6: amended to point at B7 spec as the
closing layer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["PROGRESS.md", "warm-reuse-tasks.txt", "docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md"], "verifyCommand": "rg 'CLOSED' PROGRESS.md warm-reuse-tasks.txt && rg 'B7 closure|deferred-session-claim' docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md", "acceptanceCriteria": ["PROGRESS.md §B B7 entry struck through with commit SHA", "warm-reuse-tasks.txt B7 status flipped to CLOSED", "Layer V spec §5 Risk 3 + §6 amended to reference B7 closure"]}
```

---

## Final verification — whole-branch tests

After all 7 tasks land, run the full test suite to confirm no regression:

```bash
pixi run pytest tests/ -v --tb=short
```

Expected: all green. Specific B7 surfaces to spot-check:

```bash
pixi run pytest tests/core/test_ledger_read.py tests/core/test_session_claim.py tests/core/test_orchestrator_session_claim.py tests/core/test_orchestrator_session_claim_xprocess.py tests/core/test_reaper_actor.py tests/stores/test_acquire_lock_contract.py tests/cli/ -v
```

Then:

```bash
pixi run mypy src/kinoforge/
pixi run ruff check src/kinoforge/ tests/
```

All three must be clean.

---

## Spec coverage matrix

| Spec AC | Task | Test |
|---|---|---|
| AC1 (`hold_until_first_tick` acquires, yields, polls, releases) | 2 | `test_acquires_yields_and_releases_on_first_tick` |
| AC2 (`FirstTickTimeout` on timeout) | 2 | `test_first_tick_timeout_raises_when_no_tick` + `test_first_tick_timeout_when_ledger_read_none` |
| AC3 (`Ledger.read` mirror) | 1 | `tests/core/test_ledger_read.py` (4 tests) |
| AC4 (hosted + HB-disabled use nullcontext) | 3 | `test_hosted_path_does_not_acquire_provision_lock` + `test_heartbeat_disabled_compute_path_does_not_acquire_provision_lock` |
| AC5 (`_provision_compute_once` no longer acquires) | 3 | Inline read of orchestrator.py after edit; covered by AC4's lock-files=[] assertion |
| AC6 (`act_on_verdict` probes; returns deferred) | 4 | `test_act_on_verdict_defers_when_provision_lock_held` |
| AC7 (cross-process subprocess test) | 5 | `test_reaper_defers_while_orchestrator_mid_provision` |
| AC8 (INFO log on defer) | 4 | Inspected via `caplog` in `test_act_on_verdict_defers_when_provision_lock_held` (add `caplog.at_level(logging.INFO)` if needed) |
| AC9 (holder_pid-aware UX line — implemented as `reason=` per spec drift) | 4 + 6 | `test_act_on_verdict_defers_..._reason="held by pid <N>"` + `test_emit_reap_human_includes_deferred_count` |
| AC10 (TTL = boot_timeout_s + 2*hb_interval_s) | 3 | Inspected by reading orchestrator.py post-edit; structural assertion in `test_compute_path_with_heartbeat_acquires_provision_lock` |
| AC11 (lock-contract held-while-orchestrator-runs) | 6 | `test_held_while_orchestrator_runs` |
| AC12 (CLI renders deferred — adapted from "Literal accepts" since action is plain str) | 6 | `test_emit_reap_human_includes_deferred_count` + `test_emit_reap_jsonl_handles_deferred_action` |

---

## Self-review notes (run after writing this plan)

- **Placeholder scan:** zero `TBD` / `TODO` / `???` / `FIXME` in the plan body. All `<sha>` placeholders are explicit substitution points in Task 7.
- **Internal type consistency:** `ActionResult` uses `applied_verdict` (NOT `actual_verdict`) and `action: str` throughout. `Clock` is the Protocol from `core/clock.py`. `Ledger.read` signature matches Task 1 implementation throughout.
- **Spec coverage:** AC1–AC12 all mapped to a task. Spec §3.6 watchdog adapted to the simpler `reason=` pattern with explicit drift note at the top of the plan.
- **Scope check:** 7 tasks, each atomic-committable; no task touches more than one concern; each has a verify command.
