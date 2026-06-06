# Layer U — implementation plan (heartbeat persistence)

Design: `docs/superpowers/specs/2026-06-05-layer-u-heartbeat-persistence-design.md`.

Six tasks, red/green TDD per task, atomic commits, pre-commit + full
suite gate on each. No live cloud spend at any step.

Task graph:

```
T1 (Ledger.touch)
   └── T2 (HeartbeatLoop)
          └── T3 (deploy_session wiring)  ←── T4 (config field)
                 └── T6 (docs + smoke + merge)
   └── T5 (CLI status surface)  ──→ T6
```

T4 and T5 can run in parallel with T2; T3 needs both T2 and T4
(`cfg.lifecycle().heartbeat_interval_s` is the gate).

---

## T1 — Ledger.touch for in-place entry updates

**Files**
- `tests/core/test_ledger_touch.py` (new) — ~150 LOC
- `src/kinoforge/core/lifecycle.py` (Ledger only, +~30 LOC near line 489)

**RED — write tests first**

`tests/core/test_ledger_touch.py` contains exactly these tests (all
failing against missing `Ledger.touch`):

```python
def test_touch_unknown_id_returns_false_and_does_not_append():
    """touch on an unknown id is a no-op."""
    # record(A), then touch("nonexistent", last_heartbeat=42.0)
    # assert returns False; entries() still has exactly 1 element;
    # the existing entry has no "last_heartbeat" key.

def test_touch_sets_last_heartbeat_on_existing_entry_returns_true():
    """touch updates last_heartbeat on an existing entry."""
    # record(A); touch(A.id, last_heartbeat=1234.5) -> True
    # entries()[0]["last_heartbeat"] == 1234.5

def test_touch_unchanged_value_is_noop_writes_zero_times():
    """skip-unchanged guard: second touch with same value writes nothing."""
    # record(A); touch(A.id, last_heartbeat=1.0) -> True
    # spy on _write_entries; touch(A.id, last_heartbeat=1.0) -> False
    # assert _write_entries call count after second touch == prior count

def test_touch_with_all_none_kwargs_is_noop():
    """all None kwargs returns False without acquiring the lock."""
    # record(A); spy lock; touch(A.id) -> False
    # assert lock.acquire_lock spy never called

def test_touch_filters_protected_keys():
    """protected keys passed via **extra are silently ignored."""
    # record(A, provider="local"); touch(A.id, last_heartbeat=1.0,
    #     provider="evil", id="hijacked", created_at=0.0) -> True
    # entries()[0]["provider"] == "local"; ["id"] == A.id; ["created_at"] == A.created_at
    # entries()[0]["last_heartbeat"] == 1.0

def test_touch_acquires_ledger_lock_with_expected_key_and_ttl():
    """touch uses the same lock key + ttl as record/forget."""
    # FakeStore spy records acquire_lock calls
    # touch(A.id, last_heartbeat=1.0)
    # spy received key == f"ledger/{run_id}" and ttl_s == mutate_ttl_s (default 30.0)

def test_touch_visible_across_process_boundary(tmp_path):
    """subprocess: process A records + touches, process B reads via entries()."""
    # script in subprocess: record + touch with last_heartbeat=99.0
    # parent process: Ledger(store=LocalArtifactStore(tmp_path), run_id=...).entries()
    # entries()[0]["last_heartbeat"] == 99.0

def test_touch_after_forget_returns_false_no_resurrect():
    """forget+touch race: touch on a forgotten id does not resurrect."""
    # record(A); forget(A.id); touch(A.id, last_heartbeat=1.0) -> False
    # entries() == []
```

Run: `pixi run pytest tests/core/test_ledger_touch.py -x` → expect 8
failures (AttributeError: `'Ledger' object has no attribute 'touch'`).

**GREEN — implement Ledger.touch**

Add to `src/kinoforge/core/lifecycle.py` immediately after `forget`
(line ~506):

```python
_PROTECTED_LEDGER_KEYS: frozenset[str] = frozenset(
    {"id", "provider", "tags", "created_at", "cost_rate_usd_per_hr"}
)

# inside class Ledger:

def touch(
    self,
    instance_id: str,
    *,
    last_heartbeat: float | None = None,
    **extra: float | int | str | None,
) -> bool:
    """Update fields on an existing ledger entry in place.

    Strict update (no upsert). Returns True iff a disk write happened.
    See module docstring for sentinel-gate contract on last_heartbeat.
    """
    proposed: dict[str, float | int | str] = {}
    if last_heartbeat is not None:
        proposed["last_heartbeat"] = float(last_heartbeat)
    for k, v in extra.items():
        if k in _PROTECTED_LEDGER_KEYS or v is None:
            continue
        proposed[k] = v
    if not proposed:
        return False
    with self._store.acquire_lock(
        f"ledger/{self._run_id}", ttl_s=self._mutate_ttl_s
    ):
        entries = self._read_entries()
        for e in entries:
            if e.get("id") == instance_id:
                changed = False
                for k, v in proposed.items():
                    if e.get(k) != v:
                        e[k] = v
                        changed = True
                if changed:
                    self._write_entries(entries)
                return changed
    return False
```

Module-level docstring on `Ledger` gets a new section documenting the
sentinel-gate contract (forward-compat — no reaper consumes it yet).

Run: `pixi run pytest tests/core/test_ledger_touch.py -x` → 8 pass.
Full suite + pre-commit clean. Commit:

```
feat(lifecycle): Ledger.touch for in-place entry updates (Layer U T1)
```

**Verify**: `pixi run pytest -q && pixi run pre-commit run --all-files`.

---

## T2 — HeartbeatLoop threaded poll

**Files**
- `tests/core/test_heartbeat_loop.py` (new) — ~250 LOC
- `src/kinoforge/core/heartbeat_loop.py` (new) — ~120 LOC

**RED — tests**

```python
def test_loop_ticks_provider_heartbeat_and_ledger_touch_each_interval():
    """one tick calls provider.heartbeat then ledger.touch once."""

def test_loop_eager_first_tick_writes_before_any_sleep():
    """first tick fires before _stop.wait(interval_s) — short sessions still write."""

def test_loop_provider_heartbeat_raises_loop_continues_logs(caplog):
    """provider.heartbeat side-effect Exception: caught + logged at ERROR; next tick still runs."""

def test_loop_ledger_touch_raises_loop_continues_logs(caplog):
    """ledger.touch Exception: caught + logged at ERROR; next tick still runs."""

def test_loop_sentinel_thread_tick_advances_monotonically():
    """heartbeat_thread_tick written to ledger increases tick over tick."""

def test_stop_joins_within_timeout_when_thread_mid_sleep():
    """stop() during inter-tick sleep returns within join_timeout_s."""

def test_stop_does_not_hang_when_thread_wedged():
    """inject a provider whose heartbeat() blocks forever; stop() still returns within join_timeout_s + slack (daemon thread)."""

def test_two_concurrent_loops_on_different_instances_do_not_collide():
    """two HeartbeatLoops on different ids tick independently; their ledger.touch calls land on separate entries."""
```

Run: `pixi run pytest tests/core/test_heartbeat_loop.py -x` → expect
8 failures (ModuleNotFoundError).

**GREEN — implement**

`src/kinoforge/core/heartbeat_loop.py`:

```python
"""Threaded periodic heartbeat poll + ledger persistence.

Sentinel-gate contract: every successful _tick_once writes
heartbeat_thread_tick alongside last_heartbeat. Any future code that
consults last_heartbeat for a destructive decision MUST check
heartbeat_thread_tick freshness first; if now - tick >
3 * heartbeat_interval_s, treat last_heartbeat as untrustworthy.
"""

from __future__ import annotations

import logging
import threading

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.interfaces import ComputeProvider
from kinoforge.core.lifecycle import Ledger

logger = logging.getLogger(__name__)


class HeartbeatLoop:
    def __init__(
        self,
        *,
        ledger: Ledger,
        provider: ComputeProvider,
        instance_id: str,
        interval_s: float,
        clock: Clock | None = None,
        logger_: logging.Logger | None = None,
        join_timeout_s: float = 2.0,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self._ledger = ledger
        self._provider = provider
        self._instance_id = instance_id
        self._interval_s = interval_s
        self._clock = clock or RealClock()
        self._logger = logger_ or logger
        self._join_timeout_s = join_timeout_s
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"kinoforge-hb-{instance_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(self._join_timeout_s)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick_once()
            self._stop.wait(self._interval_s)

    def _tick_once(self) -> None:
        try:
            self._provider.heartbeat(self._instance_id)
            hb = self._provider.last_heartbeat(self._instance_id)  # type: ignore[attr-defined]
            self._ledger.touch(
                self._instance_id,
                last_heartbeat=hb,
                heartbeat_thread_tick=self._clock.now(),
            )
        except Exception:  # noqa: BLE001 — never let the loop die
            self._logger.exception(
                "heartbeat tick failed for %s", self._instance_id
            )
```

Run: `pixi run pytest tests/core/test_heartbeat_loop.py -x` → 8 pass.

**Verify**: `pixi run pytest -q && pixi run pre-commit run --all-files`.

Commit:

```
feat(heartbeat): HeartbeatLoop threaded poll with crash-safe try/except + sentinel (Layer U T2)
```

---

## T3 — deploy_session integration

**Files**
- `tests/core/test_orchestrator_heartbeat.py` (new) — ~180 LOC
- `src/kinoforge/core/orchestrator.py` (deploy_session signature +
  ctx-manager body, ~+15 LOC near 483–495)

**Prerequisite**: T2 + T4 both landed (T3 reads `cfg.lifecycle().heartbeat_interval_s`).

**RED — tests**

```python
def test_deploy_session_with_interval_none_does_not_spawn_loop():
    """factory spy never called when cfg.lifecycle().heartbeat_interval_s is None."""

def test_deploy_session_with_interval_spawns_starts_and_stops_loop_in_order():
    """factory spy: instantiated, start() then stop() in correct order around yield."""

def test_deploy_session_exit_stops_loop_even_when_body_raises():
    """exception inside `with deploy_session(...)` block still triggers loop.stop() in finally."""

def test_deploy_session_loop_factory_seam_supports_test_substitution():
    """custom factory receives expected kwargs: ledger, provider, instance_id, interval_s."""

def test_deploy_session_writes_last_heartbeat_to_ledger_end_to_end():
    """real HeartbeatLoop, short interval, FakeClock; after 2 ticks ledger has last_heartbeat + heartbeat_thread_tick."""
```

**GREEN — wire**

In `src/kinoforge/core/orchestrator.py`, add `heartbeat_loop_factory:
Callable[..., HeartbeatLoop] | None = None` kwarg to `deploy_session`.
Inside the context-manager body, after instance setup and before
`yield DeploySession(...)`:

```python
hb_loop: HeartbeatLoop | None = None
interval = cfg.lifecycle().heartbeat_interval_s
if interval is not None and interval > 0:
    factory = heartbeat_loop_factory or HeartbeatLoop
    hb_loop = factory(
        ledger=Ledger(store=store, run_id=run_id),
        provider=provider,
        instance_id=instance.id,
        interval_s=interval,
    )
    hb_loop.start()
try:
    yield DeploySession(...)
finally:
    if hb_loop is not None:
        hb_loop.stop()
    # ... existing finally body ...
```

`HeartbeatLoop` import is added at top of `orchestrator.py`. The import
adds a `core/` → `core/` dep (no invariant violation; both ends are in
core).

Run: full suite + pre-commit clean.

Commit:

```
feat(orchestrator): deploy_session spawns HeartbeatLoop when configured (Layer U T3)
```

---

## T4 — LifecycleConfig.heartbeat_interval_s

**Files**
- `tests/core/test_config.py` (extend +~40 LOC)
- `src/kinoforge/core/config.py` (+~5 LOC on `LifecycleConfig`)

**Prerequisite**: T1 (independent; can land before or in parallel
with T2).

**RED — tests**

```python
def test_lifecycle_heartbeat_interval_s_default_is_none():
    """default is None (feature disabled, backwards-compat)."""

def test_lifecycle_heartbeat_interval_s_accepts_float():
    """positive float accepted."""

def test_lifecycle_heartbeat_interval_s_rejects_negative():
    """ValidationError on negative or zero values."""

def test_lifecycle_heartbeat_interval_s_round_trips_via_yaml():
    """YAML lifecycle: { heartbeat_interval_s: 30 } -> cfg.lifecycle().heartbeat_interval_s == 30.0."""
```

**GREEN — extend pydantic model**

In `src/kinoforge/core/config.py`, on `LifecycleConfig`:

```python
heartbeat_interval_s: float | None = Field(
    default=None,
    description="Seconds between background ledger heartbeat writes. "
                "None disables the feature (default). Operator guidance: "
                "values < 10 risk lock contention at scale.",
)

@field_validator("heartbeat_interval_s")
@classmethod
def _heartbeat_interval_positive(cls, v: float | None) -> float | None:
    if v is not None and v <= 0:
        raise ValueError("heartbeat_interval_s must be > 0 when set")
    return v
```

`Config.lifecycle()` is already a structural accessor — verify it
threads the field through, otherwise extend it.

Commit:

```
feat(config): LifecycleConfig.heartbeat_interval_s (Layer U T4)
```

---

## T5 — kinoforge status sentinel-staleness advisory

**Files**
- `tests/test_cli.py` (extend +~60 LOC)
- `src/kinoforge/cli/_commands.py` (~+10 LOC in `_cmd_status`)

**Prerequisite**: T1 (touches Ledger.touch-written entries).

**RED — tests**

```python
def test_status_surfaces_last_heartbeat_when_present_in_entry():
    """seed ledger entry with last_heartbeat=<ts>; status output contains last_heartbeat=<ISO>."""

def test_status_omits_last_heartbeat_when_absent():
    """seed ledger entry without last_heartbeat; status output has no last_heartbeat line.

    Guards against silent no-op regression: if Layer U write path silently breaks,
    this remains green only because no value is written — but the positive test above
    will fail, so the pair locks the contract."""

def test_status_advisory_when_heartbeat_thread_tick_is_stale():
    """seed ledger entry with last_heartbeat + heartbeat_thread_tick > 3*interval ago;
    status emits `advisory=heartbeat thread stale (Xs since last tick)` line."""

def test_status_no_advisory_when_heartbeat_thread_tick_is_fresh():
    """fresh heartbeat_thread_tick: no advisory."""
```

**GREEN — extend _cmd_status**

In `src/kinoforge/cli/_commands.py::_cmd_status`, after the ledger
`entry` lookup but before `_build_ledger_block`:

```python
hb_tick = entry.get("heartbeat_thread_tick") if entry else None
hb = entry.get("last_heartbeat") if entry else None
advisory: str | None = advisory  # keep existing advisory wiring
if hb_tick is not None and hb is not None and cfg is not None:
    interval = cfg.lifecycle().heartbeat_interval_s or 30.0
    age = time.time() - float(hb_tick)
    if age > 3 * interval:
        advisory = f"heartbeat thread stale ({age:.0f}s since last tick)"
```

Commit:

```
feat(cli): kinoforge status surfaces last_heartbeat + sentinel-staleness advisory (Layer U T5)
```

---

## T6 — docs + manual smoke + merge

**Files**
- `README.md` — Operator section: heartbeat_interval_s usage,
  guidance (`≥ 10`), and the sentinel-gate contract note for future
  reaper authors.
- `PROGRESS.md` — Phase entry under "Post-MVP" mirror of Layer L-T4
  layout with SHAs of T1–T5 + merge.
- `examples/configs/local-fake.yaml` — `heartbeat_interval_s: null`
  comment line under `lifecycle:` block.
- `examples/configs/wan.yaml` + others (if precedent demands) — same
  null-default comment.

**Manual smoke (LocalProvider; no cloud spend)**

1. Add `lifecycle: { heartbeat_interval_s: 2 }` to a copy of
   `examples/configs/local-fake.yaml`.
2. Shell A:
   `pixi run kinoforge deploy -c <copy>.yaml --state-dir /tmp/kf-smoke`.
   (`deploy` exits after recording; for the smoke, use a session-
   holding command — `generate` with a long-running fake job, or a
   harness script that opens `deploy_session` and sleeps.)
3. Shell B:
   `pixi run kinoforge status --id <id> -c <copy>.yaml --state-dir /tmp/kf-smoke`.
   Stdout includes `last_heartbeat=<ISO>` line.
4. Repeat step 3 after 30s. `last_heartbeat` advances.
5. SIGSTOP shell A's process. Wait `3 × 2 = 6` seconds. Repeat
   step 3. Stdout includes `advisory=heartbeat thread stale (Xs
   since last tick)`.
6. SIGCONT or kill shell A. Cleanup `kinoforge destroy --id <id>`.

**Final gate**

```bash
pixi run pytest -q
pixi run pre-commit run --all-files
pixi run python -c "from kinoforge.core.heartbeat_loop import HeartbeatLoop; print('ok')"
```

**Merge** via `--no-ff` to `main` matching project precedent. Commit
message references Layer U, AC count, per-task SHAs, and closes
forward-compat seam at PROGRESS:162.

---

## Verification matrix

| AC | Test | Where |
|---|---|---|
| AC1 | `test_touch_sets_last_heartbeat_*`, `test_touch_acquires_ledger_lock_*` | T1 |
| AC2 | `test_touch_unknown_id_returns_false_*` | T1 |
| AC3 | `test_touch_unchanged_value_is_noop_*` | T1 |
| AC4 | `test_touch_filters_protected_keys` | T1 |
| AC5 | `test_touch_visible_across_process_boundary` | T1 |
| AC6 | `test_loop_ticks_*`, `test_loop_sentinel_*` | T2 |
| AC7 | `test_loop_provider_heartbeat_raises_*`, `test_loop_ledger_touch_raises_*` | T2 |
| AC8 | `test_stop_joins_*`, `test_stop_does_not_hang_*` | T2 |
| AC9 | `test_deploy_session_with_interval_none_does_not_spawn_loop` | T3 |
| AC10 | `test_deploy_session_exit_stops_loop_even_when_body_raises` | T3 |
| AC11 | `test_lifecycle_heartbeat_interval_s_*` | T4 |
| AC12 | `test_status_surfaces_last_heartbeat_*` | T5 |
| AC13 | `test_status_omits_last_heartbeat_*` | T5 |
| AC14 | `test_status_advisory_when_heartbeat_thread_tick_is_stale`, `test_status_no_advisory_when_heartbeat_thread_tick_is_fresh` | T5 |
| AC15 | `pixi run pre-commit run --all-files` | T6 final gate |

---

## Out of scope (carry-forward, do not implement here)

- Reaper code that destroys pods based on `last_heartbeat`.
- Real `provider.heartbeat()` impls for RunPod / SkyPilot.
- Heartbeat persistence outside `deploy_session` (long-running sweeper
  daemon).
