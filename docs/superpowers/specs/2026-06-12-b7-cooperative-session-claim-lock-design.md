# B7 — Cooperative session-claim lock — Design

**Status:** approved at brainstorm (2026-06-12).
**Prereq:** B5a heartbeat substrate (CLOSED, commit `5aa2dcb`).
**Unblocks:** B1 (sweeper daemon), B3 (in-session warm-reuse retrofit).
**Cross-references:**
- `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` §5 Risk 3, §6.
- `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` (Ledger.touch sentinel-gate contract).
- `docs/superpowers/specs/2026-06-01-layer-p-task7-item2-warm-reuse-design.md` (warm-supplied `instance=` kwarg surface).
- `warm-reuse-tasks.txt:169–313` (brainstorm scratchpad).

---

## 1. Goal & scope

Close the `deploy_session`-vs-sweep race documented at Layer V spec §5 Risk 3: when a fresh `deploy_session.__enter__` is mid-`engine.provision` and an operator (or future B1 sweeper daemon) runs `kinoforge reap --apply` at the same wall-clock window, the new session has not yet recorded a heartbeat tick → `classify` sees sentinel-stale → `act_on_verdict` destroys the pod the session is mid-boot on.

After B7: orchestrator holds the `provision:<id>` lock continuously from the moment it commits to a specific `instance.id` through the moment `Ledger.touch` records the first `heartbeat_thread_tick`. Reaper non-blocking-probes the same key before destroying and skips entries whose orchestrator is mid-claim.

### 1.1 Three deploy paths

| Path | Trigger | Lock behavior |
|------|---------|---------------|
| Cold | `instance=None` | create instance → hold lock → `engine.provision` → `HeartbeatLoop.start` → first tick → release. |
| Warm-supplied | `instance=<existing>` (future B3 consumer) | caller's instance → hold lock → idempotent `engine.provision` → `HeartbeatLoop.start` → first tick → release. |
| Hosted | `requires_compute=False` | `nullcontext()` — no instance, no provider, no HeartbeatLoop. Unchanged. |

### 1.2 Race window B7 closes

Today the orchestrator's `provision:<id>` lock (ttl=300s, acquired inside `_provision_compute_once` at `core/orchestrator.py:231`) covers ONLY `engine.provision`. After that lock releases, deploy_session.__enter__ continues with verify (step 8) + pool setup + HeartbeatLoop.start (step 8.5, line ~740). The first heartbeat tick lands in the ledger sometime after `HeartbeatLoop.start()` returns (eager-first-tick semantics at `core/heartbeat_loop.py:152`).

Between `provision:<id>` lock release and first-tick-landed, a concurrent `kinoforge reap --apply` against the same id reads `heartbeat_thread_tick = 0` (or missing) → `classify` returns a stale verdict → `act_on_verdict` destroys.

B7 extends the lock's scope to cover that window AND grows the reaper to probe it before destroying.

---

## 2. Decisions locked at brainstorm

| # | Decision | Choice | Reason |
|---|---|---|---|
| D1 | Lock key | Reuse `provision:<id>` (sketch's K2). | Existing key, 300s TTL already production-validated, semantic match ("provisioning in flight"). No new substrate key. |
| D2 | TTL | `cfg.lifecycle().boot_timeout_s + 2*cfg.lifecycle().heartbeat_interval_s`; margin defaults to 60s when `heartbeat_interval_s` is None. | Operator-tunable knob already exists; no new background thread. Default 900s+ gives 200%+ margin over documented ComfyUI+Wan 5-min worst case. |
| D3 | Module | New `src/kinoforge/core/session_claim.py`. | Mirrors `reaper.py` / `reaper_actor.py` purity split; testable in isolation. |
| D4 | Orchestrator acquire mode | Blocking. | Two concurrent sessions for the same `instance.id` SHOULD serialize. |
| D5 | Reaper acquire mode | Non-blocking probe-only (acquire-then-immediate-release). | Reaper wants fail-fast skip, not "wait for orchestrator". |
| D6 | Sentinel-gate honesty | `hold_until_first_tick` reads `heartbeat_thread_tick` for a release-decision, NOT a destructive decision. | `classify` remains the single place gating destructive verdicts (Layer U §3.4 forward-compat contract preserved). |

---

## 3. Architecture

### 3.1 Module map

**New:**

1. **`src/kinoforge/core/session_claim.py`** — substrate. Hosts `hold_until_first_tick` context manager + `FirstTickTimeout` error. ~30 LOC.

**Edits:**

2. **`src/kinoforge/core/lifecycle.py`** — `Ledger` grows `read(instance_id: str) -> dict | None` method. Returns the matching entry from `self._read_entries()`, or `None` when absent. ~6 LOC. Mirrors the `record`/`forget`/`touch` per-id surface; preferred over forcing callers to scan `entries()` manually.
3. **`src/kinoforge/core/orchestrator.py`** — `_provision_compute_once` (line 231): drop `acquire_lock` line and dedent body. `deploy_session.__enter__`: wrap "from `instance` available through `hb_loop.start()` returns" with `hold_until_first_tick`. Gate matches HeartbeatLoop's spawn gate: `interval is not None and interval > 0 and instance is not None and resolved_provider is not None`. Otherwise `nullcontext()`.
4. **`src/kinoforge/core/reaper_actor.py`** — `act_on_verdict` (line 123): add non-blocking probe of `provision:<id>` after entering `reaper/<id>`; on contention return early with `"deferred-session-claim"` action.
5. **`src/kinoforge/core/reaper.py`** — `ActionResult.action` literal-set widens by one entry: `"deferred-session-claim"`.
6. **`src/kinoforge/cli.py:_cmd_reap`** — 5s watchdog logs holder_pid-aware WARN on contention.

`LocalProvider` / `FakeProvider` unchanged. No new YAML fields. No new CLI subcommand. No new ABC.

### 3.2 `hold_until_first_tick` contract

```python
# src/kinoforge/core/session_claim.py
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.errors import KinoforgeError
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger(__name__)


class FirstTickTimeout(KinoforgeError):
    """Raised when the HeartbeatLoop did not record a tick in time.

    The orchestrator's cold-path teardown surface catches this and
    destroys the orphaned instance before re-raising — same shape as
    CapabilityMismatch teardown.
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
) -> Iterator[None]:
    """Hold ``provision:<instance_id>`` until first heartbeat_thread_tick.

    Contract:

      1. Acquires ``store.acquire_lock(f"provision:{instance_id}", ttl_s=ttl_s)``
         blocking. Lock release happens in the outer ``with`` regardless of
         which exit path the body takes.
      2. Records ``start = clock.now()``.
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

    Hosted-edge: ``ledger.read`` returning ``None`` indefinitely means
    the caller never recorded the instance (test-substrate edge). The
    helper raises ``FirstTickTimeout`` at ``timeout_s`` — same loud
    failure as a crashed HeartbeatLoop. Callers SHOULD route the
    hosted-path branch through ``nullcontext()`` rather than this
    helper (orchestrator does — see §3.3).

    Sentinel-gate honesty: ``heartbeat_thread_tick`` is the same field
    ``classify`` consults. Reading it here is a release-decision, not a
    destructive decision — ``classify`` remains the single place gating
    destructive verdicts (Layer U §3.4 forward-compat contract).
    """
```

### 3.3 Orchestrator wire-in

`core/orchestrator.py:deploy_session.__enter__`. Today's flow after `_provision_instance_and_build_backend` returns (around line 700):

```text
... step 7: instance + backend resolved ...
step 8: verify (skip when just-discovered); CapabilityMismatch → destroy + re-raise
step 8.5: ConcurrentPool + DeploySession assembly
... HeartbeatLoop spawn (line ~740) ...
yield session
```

B7 wraps from "instance is resolved AND we have a provider" through "HeartbeatLoop.start() returns AND first tick is recorded":

```python
ledger = Ledger(store=store)
interval = cfg.lifecycle().heartbeat_interval_s
margin = (2.0 * interval) if (interval is not None and interval > 0) else 60.0
ttl = cfg.lifecycle().boot_timeout_s + margin

# Gate MUST match HeartbeatLoop's spawn gate at line ~756: when no HB
# loop will tick, hold_until_first_tick would FirstTickTimeout forever.
if (
    interval is not None
    and interval > 0
    and instance is not None
    and resolved_provider is not None
):
    claim_ctx = hold_until_first_tick(
        store=store,
        instance_id=instance.id,
        ledger=ledger,
        ttl_s=ttl,
        timeout_s=ttl,
    )
else:
    claim_ctx = nullcontext()

with claim_ctx:
    # existing step 8 (verify) + step 8.5 (HeartbeatLoop.start) live here
    ...
```

`_provision_compute_once` (line 231): delete `with store.acquire_lock(f"provision:{instance.id}", ttl_s=300):` and dedent body. The outer `hold_until_first_tick` already owns the key. Marker check + `provisioner.provision` + `write_marker` flow unchanged.

**Layering note:** the outer `hold_until_first_tick` is acquired BEFORE step 8 (`verify`). The `CapabilityMismatch` teardown branch already exists at step 8 — when verify raises, the existing teardown destroys the instance and re-raises. With B7, the lock release happens in the outer `with` unwind as part of that re-raise, BEFORE `destroy_instance` runs against the reaper-side lock. No ordering conflict.

### 3.4 Reaper-side change

`core/reaper_actor.py:act_on_verdict`, immediately after entering `reaper/<id>`:

```python
with store.acquire_lock(f"reaper/{instance_id}", ttl_s=_LOCK_TTL_S):
    # B7: non-blocking probe of provision:<id>. If an orchestrator
    # process holds it, this entry is mid-session-claim — skip this
    # sweep, log at INFO, retry next pass.
    probe_lock = store.acquire_lock(f"provision:{instance_id}", ttl_s=0.0)
    try:
        token = probe_lock.acquire(blocking=False)
    except LockTimeout:
        token = None
    if token is None:
        _log.info(
            "instance %s mid-session-claim; deferring to next sweep",
            instance_id,
        )
        return ActionResult(
            instance_id=instance_id,
            snapshot_verdict=snapshot_verdict,
            actual_verdict=snapshot_verdict,
            action="deferred-session-claim",
            cost_rate_usd_per_hr=float(entry.get("cost_rate_usd_per_hr", 0.0)),
        )
    probe_lock.release(token)  # probe-only — don't HOLD during destroy
    # existing re-classify + destroy flow follows
    live_ids = {i.id for i in provider.list_instances()}
    ...
```

Probe-only: acquire-then-immediate-release. The reaper doesn't hold `provision:<id>` while destroying; it just confirms no orchestrator is mid-claim at the moment of decision. The TTL collision concern at the brainstorm (sketch's mitigation discussion) is irrelevant here because the reaper never holds the key for any meaningful duration.

**`ttl_s=0.0` on probe_lock:** the FileLock constructor accepts any `ttl_s`; the probe acquires the lock, reads the held state, and releases without writing meaningful sidecar TTL. `ttl_s=0.0` reflects "we are not claiming this lock for any duration." When the acquire succeeds, the sidecar is overwritten with an expired-immediately TTL, then immediately released. No semantic conflict because no other process is in a "wait for this lock to expire" path — orchestrators always do blocking acquire.

### 3.5 `ActionResult` enum widening

`core/reaper.py`:

```python
@dataclass(frozen=True)
class ActionResult:
    instance_id: str
    snapshot_verdict: Verdict
    actual_verdict: Verdict
    action: Literal[
        "destroyed",
        "skipped-verdict-flipped",
        "failed",
        "forgot",
        "deferred-session-claim",  # NEW (B7)
    ]
    cost_rate_usd_per_hr: float
```

**Downstream consumers:**
- **B1 (sweeper daemon, not yet implemented):** trivially handles the new literal — the per-pass loop continues to the next entry.
- **B2 (cost dashboard, not yet implemented):** new literal surfaces at design time. Treat as "still LIVE for accounting purposes."
- **Existing CLI summary printing in `_cmd_reap`:** ONE-line print currently keys on `action`; add a case for `"deferred-session-claim"` rendering as `"deferred"` in the table.

### 3.6 Operator-UX diagnostic

`cli.py:_cmd_reap` wraps the per-entry reaper-side lock acquire chain in a 5s wall-clock watchdog. On expiry, the watchdog reads `holder_pid` from the `provision:<id>` FileLock sidecar JSON (path: `<state_dir>/locks/provision_<id>.lock`) and logs:

```text
WARNING waiting on provision lock for <id> (held by pid <n>) —
        orchestrator is mid-session-claim
```

Avoids operator-kills-the-reaper-out-of-confusion on long cold provisions. B1 (sweeper daemon) inherits the same diagnostic via structured logging when it lands.

### 3.7 TTL derivation rationale

`cfg.lifecycle().boot_timeout_s` (default 900s = 15 min) bounds the worst-case `engine.provision`. The brainstorm's documented worst case is ComfyUI+Wan cold provision at 1–5 min, giving 200–1500% margin against the default. Operators on heavier engines (future Veo, larger model stacks) bump `boot_timeout_s` already; B7's TTL tracks it for free.

The `+ 2*heartbeat_interval_s` term covers the time between `HeartbeatLoop.start()` returning and the first eager tick landing in the ledger. Eager-first-tick fires immediately (`core/heartbeat_loop.py:152` — `while not stop: tick; wait`), so this term is conservative — typically ~10 ms on Local, ~100 ms on cloud-store. The `2*` is a safety multiplier against transient store-write delays.

When `heartbeat_interval_s` is `None` or `<= 0` (no HeartbeatLoop), the wire-in gate (§3.3) routes to `nullcontext()` and the helper is never entered — TTL term is moot. Layer V's `classify` returns `HEARTBEAT_UNKNOWN` (non-destructive) for entries with no `heartbeat_thread_tick` at all, so the race B7 closes doesn't exist on the HB-disabled path.

---

## 4. Failure modes

| # | Mode | Handling |
|---|---|---|
| F1 | `engine.provision` exceeds TTL (pathologically slow Wan boot beyond `boot_timeout_s + 2*heartbeat_interval_s`) | Lease stealable by reaper after `ttl_s`; `hold_until_first_tick`'s polling phase fires `FirstTickTimeout` when `timeout_s` elapses. Cold-path teardown via existing CapabilityMismatch-shaped teardown surface in `deploy_session.__enter__` (orchestrator catches FirstTickTimeout, calls `resolved_provider.destroy_instance(instance.id)`, re-raises). Both reaper-side WARN and orchestrator-side FirstTickTimeout are emitted. Loud, not silent. |
| F2 | HeartbeatLoop crashes silently between `start()` and first tick (broad try/except in `_tick_once` swallows everything; `_thread` continues looping but ledger never updates) | Same as F1 — `FirstTickTimeout` fires, cold-path teardown runs. |
| F3 | Operator interrupts mid-claim (SIGINT during cold provision) | Existing cancel_token unwind path. `hold_until_first_tick` exits via KeyboardInterrupt → outer `with` releases lock → `pool.close(cancel_pending=True)` drain runs (Phase 50 cancel-token plumbing). |
| F4 | Two concurrent `deploy_session` for same `instance.id` | Serialize on `provision:<id>` blocking acquire. Pre-existing 300s lock semantics; B7 only changes scope, not concurrency contract. Second session waits up to `ttl_s` and proceeds. |
| F5 | Cross-host on cloud-store backend (S3/GCS) | Single-host today (orchestrator + reaper run on the same workstation; ledger-uri config makes this explicit). S3-backed `acquire_lock` cross-host semantics deferred to B16-neighborhood spec (distributed sweeper). Documented as out-of-scope. |
| F6 | Reaper-side probe acquire raises non-LockTimeout exception | The `try/except LockTimeout` catches only timeout; other exceptions propagate. Strategy: catch is narrow on purpose — store-level errors should fail the sweep entry, not silently swallow. The outer `act_on_verdict` already returns `ActionResult(action="failed", ...)` on store errors. |

---

## 5. Test plan

### 5.1 Offline unit — `tests/core/test_session_claim.py`

```python
def test_acquires_yields_and_releases_on_first_tick(tmp_path):
    """Happy path: acquire → yield → poll → release."""

def test_first_tick_timeout_raises(tmp_path):
    """timeout_s elapses with no fresh tick → FirstTickTimeout."""

def test_hosted_edge_immediate_release(tmp_path):
    """ledger.read returns None → no polling, immediate release."""

def test_yielded_block_exception_propagates(tmp_path):
    """Caller raises → lock releases → exception re-raised; no polling."""

def test_blocking_acquire_serializes_concurrent_calls(tmp_path):
    """Second concurrent hold_until_first_tick blocks until first releases."""

def test_clock_injection_used_for_start_time(tmp_path):
    """start = clock.now() (not time.time())."""

def test_poll_uses_injected_interval(tmp_path):
    """poll_interval_s drives sleep cadence (verify via spy clock)."""
```

### 5.2 Cross-process subprocess — `tests/core/test_orchestrator_session_claim_xprocess.py`

Mirrors `PROGRESS.md:1130` Layer U cross-process visibility test shape.

```python
def test_reaper_defers_while_orchestrator_mid_provision(tmp_path):
    """End-to-end race: orchestrator A is mid-provision; reaper B fires;
    B defers; A finishes; A's heartbeat lands; B's next sweep reaps."""
    # Subprocess A: starts deploy_session with sleepy FakeProvider
    #   (engine.provision sleeps 2s); HeartbeatLoop set to interval_s=0.1.
    # Wait 0.2s — A is mid-provision, holding provision:<id>.
    # Subprocess B: runs `kinoforge reap --apply` against same id.
    # Assert B's stdout contains "deferred-session-claim" or "deferring".
    # Wait for A to finish (~2.5s).
    # Subprocess C: runs `kinoforge reap --apply` again.
    # Assert C either destroys (if expired) OR returns LIVE (if heartbeat fresh).
```

### 5.3 Reaper unit — `tests/core/test_reaper_actor.py` (delta)

```python
def test_act_on_verdict_defers_when_provision_lock_held(tmp_path):
    """Orchestrator-side acquires provision:<id>; reaper probe returns
    None; act_on_verdict returns action='deferred-session-claim'."""

def test_act_on_verdict_proceeds_after_lock_released(tmp_path):
    """After orchestrator releases, second act_on_verdict call probes
    successfully and proceeds to re-classify + destroy."""
```

### 5.4 Lock contract — `tests/stores/test_lock_contract.py` (delta)

```python
@pytest.mark.parametrize("store_factory", [_local_store, _mocked_s3_store])
def test_held_while_orchestrator_runs(store_factory):
    """Outer thread acquires provision:<id>; inner non-blocking probe
    returns None; outer releases; inner non-blocking probe succeeds."""
```

### 5.5 Live spend

None. `FakeProvider` + `LocalProvider` + mocked-S3 cover the lock contract.

---

## 6. Acceptance criteria

- **AC1.** `hold_until_first_tick` acquires `provision:<id>` blocking, yields, polls `ledger.read(instance_id)` reading `heartbeat_thread_tick` at `poll_interval_s` cadence, releases on `tick >= start`.
- **AC2.** `FirstTickTimeout` raises when the ledger never records the tick within `timeout_s` (including the `ledger.read(...) is None` edge — same loud failure shape).
- **AC3.** `Ledger.read(instance_id) -> dict | None` returns the matching entry or None when absent; mirrors the per-id surface of `record`/`forget`/`touch`.
- **AC4.** Hosted-path AND HB-disabled (`heartbeat_interval_s is None or <= 0`) branches of `deploy_session.__enter__` enter `nullcontext()` and skip the lock entirely.
- **AC5.** `_provision_compute_once` no longer acquires `provision:<id>` independently — only the outer `hold_until_first_tick` site acquires it.
- **AC6.** `act_on_verdict` non-blocking-probes `provision:<id>` after entering `reaper/<id>`; on contention returns `action="deferred-session-claim"`.
- **AC7.** Cross-process subprocess test demonstrates reaper-defers → orchestrator-finishes → no destroy occurred during the race window.
- **AC8.** Reaper logs `"instance <id> mid-session-claim; deferring to next sweep"` at INFO when the probe finds the lock held.
- **AC9.** `_cmd_reap` logs holder_pid-aware WARN at the 5s contention watchdog.
- **AC10.** TTL derives from `cfg.lifecycle().boot_timeout_s + 2*cfg.lifecycle().heartbeat_interval_s`. Branches without an HB loop never enter the helper (AC4), so the interval-is-None TTL term is moot.
- **AC11.** Lock-contract test exercises held-while-orchestrator-runs across local + mocked-S3 stores.
- **AC12.** `ActionResult.action` Literal accepts `"deferred-session-claim"`; CLI summary table renders it as `"deferred"`.

---

## 7. Risks

- **R1. Lease-steal under pathological slow provision.** `boot_timeout_s` default 900s gives 200%+ margin over documented 5-min ComfyUI+Wan cold-boot worst case. Mitigated by F1 fail-loud path (FirstTickTimeout + cold-path teardown).
- **R2. fcntl per-fd flock semantics inside same process.** Avoided by single-acquire-site refactor (Approach A) — `_provision_compute_once` loses its acquire entirely; the outer `hold_until_first_tick` is the only acquire site for `provision:<id>`.
- **R3. Cross-host single-flock assumption.** Single-workstation today; deferred to B16-neighborhood (distributed sweeper). Documented out-of-scope.
- **R4. `ActionResult` enum widening propagates to downstream layers.** B1 (sweeper) loop continues on the new literal; B2 (cost dashboard) not yet implemented, will see at design time.
- **R5. Reaper-side probe overhead.** One extra `acquire_lock` per swept entry. Local-store acquire is ~10 µs (fcntl flock); cloud-store probe is ~50 ms (S3 PutObject + sidecar read). For N=10 entries that's ~500 ms added per `--apply` pass. Acceptable.

---

## 8. Out of scope

- Cross-host lock semantics on cloud-store backends (B16 neighborhood when distributed sweeper materialises).
- Refresh-ticker TTL strategy (rejected at brainstorm — boot_timeout_s derivation simpler).
- B1 sweeper per-pass deferred-claim aggregation diagnostic (logged per-entry today; B1 may add summary line).
- B3 attach-to-warm orchestrator integration — separate layer; B7 just guarantees the lock seam exists for B3 to consume.
- Promoting `_LOCK_TTL_S` (reaper's own destroy-window TTL at `core/reaper_actor.py:27`) — unrelated to B7's session-claim scope; stays at 30s.

---

## 9. Effort estimate

- ~30 LOC `src/kinoforge/core/session_claim.py` (new).
- ~15 LOC `src/kinoforge/core/orchestrator.py` (wire-in + delete provision-lock from `_provision_compute_once`).
- ~10 LOC `src/kinoforge/core/reaper_actor.py` (non-blocking probe).
- ~3 LOC `src/kinoforge/core/reaper.py` (`ActionResult.action` Literal widening).
- ~5 LOC `src/kinoforge/cli.py` (watchdog WARN).
- ~150 LOC tests across `tests/core/test_session_claim.py`, `tests/core/test_orchestrator_session_claim_xprocess.py`, `tests/core/test_reaper_actor.py` delta, `tests/stores/test_lock_contract.py` delta.

Live spend: **$0**. Pure offline lock-contract work.

---

## 10. Task split (for /superpowers-extended-cc:write-plan)

1. **Task a — `Ledger.read` + `core/session_claim.py` + offline unit tests.** RED-first: write `tests/core/test_ledger_read.py` (mirrors `test_ledger_touch.py` shape) with `Ledger.read` cases (present-id returns dict, absent-id returns None, post-`forget` returns None); implement `Ledger.read`; GREEN. Then RED-first: write `tests/core/test_session_claim.py` with the 7 cases from §5.1; implement helper; GREEN. Two atomic commits.
2. **Task b — orchestrator wire-in + delete inner provision lock.** RED-first: write a deploy_session-level test asserting the outer-hold scope covers verify+HB-start; implement wire-in; delete `_provision_compute_once` lock; GREEN.
3. **Task c — reaper-side non-blocking probe + ActionResult widening.** RED-first: extend `tests/core/test_reaper_actor.py` with the two new cases from §5.3; implement probe + Literal widening; GREEN.
4. **Task d — cross-process subprocess test.** RED-first: write `tests/core/test_orchestrator_session_claim_xprocess.py` from §5.2; runs after a, b, c land so the integration is real.
5. **Task e — CLI watchdog WARN + lock-contract parametrize.** RED-first: extend `tests/stores/test_lock_contract.py` with the held-while-orchestrator-runs case; extend `_cmd_reap` with the watchdog; GREEN.
6. **Task f — PROGRESS.md + docs/superpowers/specs/<this-file> close-out.** Strike B7 in `PROGRESS.md §B`, replace warm-reuse-tasks.txt B7 entry with closeout summary, amend Layer V spec §5 Risk 3 + §6 to point at B7 spec closing the gap.

---

## 11. Forward-compat hooks for downstream layers

- **B1 (sweeper daemon).** Loop body invokes `sweep + act_on_verdict` per existing `core/reaper.py` shape. New `"deferred-session-claim"` literal: count in per-pass summary; do NOT retry within the same pass (next sweep pass is the natural retry).
- **B3 (in-session warm-reuse retrofit).** `deploy_session.__enter__` with `instance=<warm-pod>` enters the same `hold_until_first_tick` block as the cold path — the wire-in is unconditional on `instance is not None and resolved_provider is not None`. B3 inherits the lock semantics for free.
- **B16 (distributed sweeper, RayPool neighborhood).** Cross-host `acquire_lock` semantics on S3/GCS-backed stores. B7's contract is store-agnostic — the helper takes `ArtifactStore`, not `LocalArtifactStore` — but acceptance is offline-local-only. B16 spec MUST verify the cloud-store path under cross-host concurrency.

---

## 12. Sanity-checks against repo (verified 2026-06-12)

- `core/reaper_actor.py:27` — `_LOCK_TTL_S = 30.0` ✓ (stays as-is, B7 doesn't touch it).
- `core/reaper_actor.py:123` — `with store.acquire_lock(f"reaper/{instance_id}", ttl_s=_LOCK_TTL_S):` ✓ (B7 inserts probe immediately inside this block).
- `core/orchestrator.py:231` — `with store.acquire_lock(f"provision:{instance.id}", ttl_s=300):` ✓ (B7 deletes this line).
- `core/orchestrator.py:~700–740` — step 8 (verify) + step 8.5 (HB spawn) ✓ (B7 wraps this region).
- `core/heartbeat_loop.py:152` — eager first tick ✓ (`while not self._stop.is_set(): self._tick_once(); self._stop.wait(self._interval_s)`).
- `stores/local_lock.py:80–140` — FileLock blocking default + holder_pid sidecar ✓.
- `core/lifecycle.py:540–612` — `Ledger.touch` writes `heartbeat_thread_tick` via `**extra` ✓.
- `core/config.py:88–141` — `LifecycleConfig.boot_timeout` default 900s + `heartbeat_interval_s` Optional ✓.
- Layer V spec §5 Risk 3 quote verbatim at `2026-06-06-layer-v-heartbeat-aware-reaper-design.md:670–676` ✓.
- B5a substrate shipped (commit `bade08c` live smoke; `5aa2dcb` C25 guard) — B7's release-side `heartbeat_thread_tick` read does NOT depend on B5a's wire-level satisfier; Layer U HeartbeatLoop unconditional `Ledger.touch(heartbeat_thread_tick=...)` is the sole source ✓.
