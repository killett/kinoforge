# Layer U — `last_heartbeat` persistence on the production ledger

Closes the "Layer S forward-compat seam, not yet wired" follow-up at
`PROGRESS.md:162`. Layer S (Phase 33) wired the **read** half — the CLI
status formatter at `src/kinoforge/cli/_commands.py:487–491` already
surfaces a `last_heartbeat=<ISO-timestamp>` line whenever the ledger
entry carries that field. Layer S deliberately stopped short of the
write half, leaving an explicit forward-compat seam: "when a future
layer wires production-side persistence, the operator-visible side will
light up automatically with no further `_cmd_status` work" (PROGRESS:990).

Layer U is that future layer. It ships a write path so `kinoforge
status` shows operator-visible "last seen" times across process
restarts, while defending against a non-obvious failure mode that
exploration surfaced before any code was written.

The work is fully offline-tested. No live cloud spend.

---

## 1. Goals + scope

**In scope:**

- A new `Ledger.touch(instance_id, *, last_heartbeat=None, **extra) -> bool`
  method that updates a single ledger entry in place under the existing
  cross-process lock.
- A new `HeartbeatLoop` background-thread class that calls
  `provider.heartbeat(id)` (source) and `ledger.touch(id, hb)`
  (persister) on a configured cadence.
- `deploy_session` integration so the loop's lifetime tracks the
  orchestration session, not the process. Both `generate()` and
  `batch_generate()` benefit automatically.
- A config field `LifecycleConfig.heartbeat_interval_s: float | None`
  (default `None` = feature disabled, backwards-compat).
- A sentinel field `heartbeat_thread_tick` written alongside
  `last_heartbeat` on every successful tick — the seam that lets future
  reaper code distinguish "fresh heartbeat" from "thread silently
  crashed two hours ago".
- `kinoforge status` advisory line when the sentinel is stale relative
  to the configured cadence.

**Out of scope:**

- Wiring a heartbeat-aware reaper that destroys pods based on
  `last_heartbeat`. The sentinel-gate contract is documented in this
  layer for a future Layer V; no destructive code is added here.
- Implementing real `provider.heartbeat()` for `RunPodProvider` and
  `SkyPilotProvider`. Both keep their existing no-op
  implementations — they have their own dead-man mechanisms
  (RunPod in-pod selfterm, SkyPilot native autostop).
- Persisting heartbeat outside `deploy_session` (e.g., from a long-
  running `kinoforge sweeper` subcommand). Possible Layer V candidate.

---

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Write trigger = dedicated periodic poll inside `deploy_session`. | Idle-but-alive pods get heartbeats; reuses the existing session context-manager pattern; no orchestrator hot-path coupling. Q1 re-opened after exploration found `is_liveness_OK` has zero production callers. |
| D2 | Call site = inside `deploy_session` ctx; thread lifetime tracks session, not process. | One-shot CLI commands (`status`, `forget`, `list`) don't spawn the thread. Persistence is bounded by orchestration session lifetime. |
| D3 | Scope = generation + persistence (both source and persister in the same thread). | Q3 re-opened after exploration showed pure-pipe ships shelfware: no caller of `provider.heartbeat()` exists in production, so a persistence-only layer would carry no data. |
| D4 | Three-layer crash-safety defense (try/except + sentinel + daemon thread). | User-requested explicit constraint. Silent thread death cannot translate to healthy-pod destruction via dead-man window. See §3.4. |
| D5 | Sentinel field `heartbeat_thread_tick` written on every successful tick. | Lets any future reaper distinguish fresh heartbeat from stale (crashed) heartbeat. Sentinel-gate contract documented in `Ledger.touch` docstring + `PROGRESS.md` Layer U entry. CLI surfaces a user-visible advisory when stale. |
| D6 | Config-gated, default-off (`heartbeat_interval_s: float \| None = None`). | Backwards-compat for every existing YAML config. Operator opts in. |
| D7 | `Ledger.touch` is strict-update, not upsert. | Unknown id → returns `False`, no entry created. Avoids masking sweeper-vs-record races (`record` is the sole insert path). |
| D8 | Skip-unchanged guard inside `touch` (compare disk value before write). | Pre-mitigation for the day a sub-second-cadence consumer adopts the API. Lock acquired but no disk write if value unchanged. |
| D9 | Protected ledger keys (`id`, `provider`, `tags`, `created_at`, `cost_rate_usd_per_hr`) filtered from `**extra`. | Defends against accidental overwrite of `record`-owned fields by future Layer V consumers that grow `touch`'s payload. |
| D10 | Injectable `heartbeat_loop_factory` seam on `deploy_session`. | Tests substitute a synchronous (non-threaded) impl for deterministic assertions; mirrors the project's existing "every adapter has injected I/O seams" pattern (PROGRESS:86). |
| D11 | First tick is eager (no sleep). | Short-lived sessions still write at least one heartbeat. `wait(interval_s)` runs only after the first tick. |
| D12 | `daemon=True` thread + bounded `join(timeout=2.0)` on stop. | Wedged thread cannot block process exit. Acceptable fallback: provider-native cleanup (RunPod selfterm, SkyPilot autostop, LocalProvider process containment) catches any orphan pod. |
| D13 | Timestamps use `clock.now()` (already local TZ per project rule). | `feedback_local_timezone_only` in user memory; `Clock` ABC already injected. |

---

## 3. Architecture

### 3.1 Module map

```
src/kinoforge/
  core/
    lifecycle.py             MODIFIED  +30 LOC  (Ledger.touch)
    heartbeat_loop.py        NEW       ~150 LOC (HeartbeatLoop class)
    orchestrator.py          MODIFIED  +15 LOC  (deploy_session integration)
    config.py                MODIFIED  +5  LOC  (LifecycleConfig field)
  cli/
    _commands.py             MODIFIED  +10 LOC  (sentinel-staleness advisory)
tests/
  core/
    test_ledger_touch.py            NEW  ~8 tests
    test_heartbeat_loop.py          NEW  ~8 tests
    test_orchestrator_heartbeat.py  NEW  ~5 tests
    test_config.py                  MODIFIED  +4 tests
  test_cli.py                       MODIFIED  +4 tests
examples/
  configs/*.yaml             MODIFIED  +heartbeat_interval_s comment lines
README.md                    MODIFIED  +Operator section
PROGRESS.md                  MODIFIED  +Phase entry
```

### 3.2 `Ledger.touch` contract

Sketch:

```python
def touch(
    self,
    instance_id: str,
    *,
    last_heartbeat: float | None = None,
    **extra: float | int | str | None,
) -> bool:
    """Update fields on an existing ledger entry in place.

    Args:
        instance_id: Identity of the entry to mutate. Unknown id is a no-op.
        last_heartbeat: Float seconds-since-epoch. None skips the field.
        **extra: Forward-compat seam for additional fields. Keys in the
            protected set are filtered; None values are skipped.

    Returns:
        True iff a disk write happened. False on unknown id, no-op kwargs,
        or value-unchanged (skip-unchanged guard).

    Threading:
        Acquires ``acquire_lock(f"ledger/{run_id}", ttl_s=mutate_ttl_s)``
        — the same key/ttl as ``record`` and ``forget``.

    Sentinel-gate contract:
        Code that consults ``last_heartbeat`` for a reaping or destructive
        decision MUST first check ``heartbeat_thread_tick`` (the sentinel
        written by ``HeartbeatLoop._tick_once``). If
        ``now - heartbeat_thread_tick > 3 * heartbeat_interval_s``, treat
        ``last_heartbeat`` as untrustworthy. No production reaper consults
        the field today; this docstring documents the contract for any
        future Layer V consumer.
    """
```

Protected key set: `{"id", "provider", "tags", "created_at", "cost_rate_usd_per_hr"}`.
Whitelisted extras as of Layer U: `last_heartbeat`, `heartbeat_thread_tick`.

### 3.3 `HeartbeatLoop` shape

```python
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
    ): ...

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick_once()
            self._stop.wait(self._interval_s)

    def _tick_once(self) -> None:
        try:
            self._provider.heartbeat(self._instance_id)
            hb = self._provider.last_heartbeat(self._instance_id)
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

### 3.4 Three-layer crash-safety defense

**Layer 1 — inner `try/except Exception` per tick.** Any exception from
`provider.heartbeat`, `provider.last_heartbeat`, or `ledger.touch` is
caught, logged via `logger.exception` (full stack trace), and the loop
continues to the next interval. A single bad tick cannot kill the loop.
Only an interpreter-level catastrophe (segfault, OOM kill) takes the
thread.

**Layer 2 — sentinel field.** Every successful tick writes
`heartbeat_thread_tick = clock.now()` alongside `last_heartbeat`. The
contract: any code that consults `last_heartbeat` for a reaping or
destructive decision MUST first check `heartbeat_thread_tick`; if
`now - heartbeat_thread_tick > 3 × heartbeat_interval_s`, treat
`last_heartbeat` as untrustworthy. No production reaper consumes the
field today, so the gate is forward-compat — but documented in
`Ledger.touch` docstring + `PROGRESS.md` Layer U entry + a status-side
user-visible advisory, so a future Layer V reaper author cannot miss
it.

**Layer 3 — bounded process-exit.** Thread is `daemon=True`, so a
wedged thread cannot block process exit. `HeartbeatLoop.stop()` calls
`join(timeout=2.0)` — if the thread is hung, the parent process exits
anyway. Pod outlives the kinoforge session, but provider-native
cleanup catches it: RunPod selfterm, SkyPilot autostop, or
LocalProvider process containment. Documented as acceptable fallback.

### 3.5 `deploy_session` integration

```python
@contextmanager
def deploy_session(
    cfg: Config, *, store, provider, engine, ...,
    heartbeat_loop_factory: Callable[..., HeartbeatLoop] | None = None,
) -> Iterator[DeploySession]:
    # ... existing setup ...
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
```

Injectable `heartbeat_loop_factory` mirrors the project's pervasive
"injected I/O seams" pattern. Lets tests substitute a synchronous
non-threaded impl for deterministic assertions.

### 3.6 Config

```yaml
lifecycle:
  heartbeat_interval_s: 30   # null disables — default null
```

`null` = feature disabled. Existing configs untouched. Operator
guidance documented in README: `heartbeat_interval_s ≥ 10` to avoid
lock contention at scale.

### 3.7 CLI status surface

After fetching the ledger entry, before printing the status block,
`_cmd_status` checks the sentinel and emits an advisory when stale:

```python
hb_tick = entry.get("heartbeat_thread_tick")
if hb_tick is not None and entry.get("last_heartbeat") is not None:
    age = time.time() - float(hb_tick)
    stale_window = 3 * (cfg.lifecycle().heartbeat_interval_s or 30)
    if age > stale_window:
        advisory = f"heartbeat thread stale ({age:.0f}s since last tick)"
```

Reuses the existing advisory mechanism added in Layer S (the `advisory`
field of `_print_status_block`).

---

## 4. Acceptance criteria

| # | Criterion | Verified by |
|---|---|---|
| AC1 | `Ledger.touch` updates `last_heartbeat` on an existing entry under the cross-process lock. | `tests/core/test_ledger_touch.py::test_touch_sets_last_heartbeat_*` + lock-spy test |
| AC2 | Unknown id is a no-op (returns False, no entry created). | `test_touch_unknown_id_returns_false_and_does_not_append` |
| AC3 | Same-value second call is a no-op (no disk write). | `test_touch_unchanged_value_is_noop_writes_zero_times` |
| AC4 | Protected keys cannot be overwritten via `**extra`. | `test_touch_filters_protected_keys` |
| AC5 | Touched value survives the process boundary. | Subprocess test: process A writes, process B reads. |
| AC6 | `HeartbeatLoop` calls `provider.heartbeat` + `ledger.touch` per interval; sentinel monotonic. | `tests/core/test_heartbeat_loop.py::test_loop_ticks_*` + sentinel test |
| AC7 | Exception in `provider.heartbeat` / `ledger.touch` does NOT kill the loop. | `caplog` tests at ERROR level |
| AC8 | `stop()` returns within `join_timeout_s` even when thread is mid-sleep or wedged. | `test_stop_joins_within_timeout_*` |
| AC9 | `deploy_session` with `heartbeat_interval_s=None` does NOT spawn a thread. | Factory spy assertion |
| AC10 | `deploy_session` exit stops the loop even when body raises. | Exception-bubble test |
| AC11 | `LifecycleConfig.heartbeat_interval_s` round-trips through YAML; negative values rejected. | `tests/core/test_config.py` |
| AC12 | `kinoforge status` surfaces `last_heartbeat=<ISO>` when the ledger entry has the field. | `tests/test_cli.py::test_status_surfaces_last_heartbeat_*` |
| AC13 | `kinoforge status` omits `last_heartbeat` when absent (negative — guards against silent no-op regression). | `test_status_omits_last_heartbeat_when_absent` |
| AC14 | `kinoforge status` emits `advisory=heartbeat thread stale (...)` when sentinel is stale; no advisory when fresh. | Two AC tests in `tests/test_cli.py` |
| AC15 | Full test suite green; ruff/ruff-format/mypy clean; invariant scan passes. | `pixi run pre-commit run --all-files` |

---

## 5. Risks

1. **CI flake from thread timing.** `HeartbeatLoop` tests use
   `threading.Barrier` and `FakeClock`-driven `Event.wait` rather than
   wall-clock sleeps. Production default `join_timeout_s=2.0`; test
   join timeouts widened to 5.0s for slack. If T2 flakes in CI, the
   first fix is to widen barrier timeouts, not to introduce retry
   loops.
2. **Lock contention at high cadence.** At `heartbeat_interval_s=30`
   with 10 instances, ~0.3 lock acquisitions/sec — negligible. At
   `heartbeat_interval_s=1` with 100 instances, 100/sec — measurable.
   Operator guidance documents `heartbeat_interval_s ≥ 10`. No hard
   guard in code (trust operator).
3. **Sentinel gate is forward-compat only.** No reaper consumes
   `last_heartbeat` for destructive decisions today, so the documented
   gate exists for a future Layer V. Risk: a future contributor reaps
   without checking the sentinel → kills healthy pods. Mitigation:
   docstring + PROGRESS entry + sentinel-staleness AC tests serve as
   living documentation of the contract.
4. **`daemon=True` thread does not run cleanup at process kill.**
   Acceptable: provider-native termination (RunPod selfterm, SkyPilot
   autostop, LocalProvider process containment) catches orphan pods.
5. **JSON encoding of `heartbeat_thread_tick` (float epoch seconds).**
   Same encoding as `created_at` already in the ledger schema;
   `json.dumps` handles natively. No new edge case.

---

## 6. Out of scope (carry-forward candidates)

- **Layer V — heartbeat-aware reaper.** Wires `is_liveness_OK` (or a
  successor) into production code paths. The sentinel-gate contract
  shipped in Layer U is the upstream dependency.
- **Layer V or W — heartbeat persistence outside `deploy_session`.** A
  long-running `kinoforge sweeper` daemon would also write heartbeats
  for pods owned by other sessions. Out of scope here.
- **Provider `heartbeat()` real implementations for RunPod / SkyPilot.**
  Both have native dead-man mechanisms; no current operator pain point.
