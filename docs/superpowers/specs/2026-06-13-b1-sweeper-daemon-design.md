# B1 — Layer W: `kinoforge sweeper` daemon — Design

**Date:** 2026-06-13
**Status:** Design APPROVED 2026-06-13; ready for plan.
**Tracking:** PROGRESS.md §B (B1, next entry); warm-reuse-tasks.txt lines 523–555.
**Prereqs (all CLOSED before B1):**
- B5a heartbeat substrate + RunPod satisfier — commit `bade08c`; C25 wire-discovery guard `5aa2dcb`.
- B7 cooperative session-claim lock — commit `8f1ee89` (and predecessors).
- B4 cross-CLI warm-reuse exposure — commit `54d2867`.
- B2 Layer X cost dashboard — closeout `f7071c0` (sibling Prom prefix `kinoforge_*`).
**Spec hooks:**
- `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` §6 — explicit sweeper-daemon hook.
- `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` — `provider_heartbeat_supported` + `HEARTBEAT_SUBSTRATE_MISSING` contract honoured at sweeper banner level.
- `docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md` — non-blocking `provision:<id>` probe in `act_on_verdict` already wired; B1 surfaces `action="deferred-session-claim"` distinctly in daemon logs.
- `docs/superpowers/specs/2026-06-12-b2-cost-dashboard-design.md` §9 — Prom exposition format mirrored.
- `src/kinoforge/core/heartbeat_loop.py` — daemon loop shape mirrors `HeartbeatLoop` (eager first tick at `:153`, bounded shutdown via `_stop.set()` + `_thread.join(join_timeout_s)` at `:144-145`, broad `try/except` per iter at `:166-175`, structured logging).
- `src/kinoforge/core/reaper.py` + `src/kinoforge/core/reaper_actor.py` — `sweep()` + `act_on_verdict()` called once per tick; contracts unchanged.

---

## 1. Purpose

Today, `kinoforge reap` is a one-shot CLI. Between operator invocations, an idle pod that nobody cleans up continues to accrue cost — RunPod's native dead-man kills the pod on the kernel-shutdown 8-day ceiling, but every hour of idle between manual sweeps is wasted spend. Layer V deliberately shipped `reap` as the proof that the substrate works; B1 ships the consumer that proves the substrate scales.

B1 is a long-running process that calls `sweep()` on a configurable cadence (default 60s) plus a small CLI surface to start / stop / inspect it. Every architectural decision honours the constraint: B1 is a **tight ~6-8 task consumer** of B5a / B7 / B4 / B2, **not a new substrate**.

## 2. Locked constraints (carried from brief; verified vs code)

These are **not** re-litigated; they are recorded so the plan and reviewers share the same load-bearing facts.

1. **Conservative-on-ignorance sentinel-gate.** `act_on_verdict` already consults `provider_heartbeat_supported(provider_kind)` at `reaper_actor.py:239-253` and treats `HEARTBEAT_SUBSTRATE_MISSING` as `action="no_op"` with WARN-once-per-`(provider_kind, instance_id)` dedup. B1 inherits this verbatim. SkyPilot stays a no-op + WARNING until B5b ships. **B1 surfaces the gate at startup-banner level** so daemon operators see the contract in their first log line — see §6.2 banner template.
2. **Daemon loop shape mirrors `HeartbeatLoop`.** Eager first tick, bounded shutdown, per-iter `try/except Exception` so one bad classify never kills the loop, structured logging. Reference: `core/heartbeat_loop.py:151-175`.
3. **Surface.** `kinoforge sweeper start | stop | status | metrics` subcommand family + `sweeper:` YAML block.
4. **B7 inherit, no new lock surface.** `act_on_verdict` already non-blocking-probes `provision:<id>` and returns `ActionResult(action="deferred-session-claim", reason="held by pid <N>; ...")` at `reaper_actor.py:214-227`. B1 ingests these results via `stats.fold(report)` and surfaces the count + holder-pid in distinct INFO log lines per sweep + a Prom label.
5. **Live spend: zero.** `FakeProvider` + `LocalProvider` + subprocess-isolated start/stop test cover everything.
6. **Out of scope:** distributed sweeper (B16 RayPool territory), supervisord/asyncio rewrite, cgroup integration, cross-host coordination beyond what `acquire_lock` mandates, sweeper-internal lock substrate, ledger-schema growth beyond the synthetic `sweeper:<host>` instance_id mandated by D2.

## 3. Decisions locked at brainstorm

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Daemon-launch shape | **Foreground supervisor.** `kinoforge sweeper start` blocks. Operator wraps under systemd / nohup / docker PID 1 / tmux. | Simplest code path; composes with existing process supervisors; failure recovery delegated to the supervisor — no PID-file race, no double-fork ceremony. |
| D2 | Daemon-side liveness | **Ledger sweeper-tick entry.** Reserved instance_id `sweeper:<host>`; provider field set to literal `"_sweeper"`. Each tick calls `ledger.touch("sweeper:<host>", heartbeat_thread_tick=clock.now(), **stats)`. | Uniform with the substrate (mirrors Layer U `HeartbeatLoop._tick_once`); `status` reuses Layer U sentinel-window math; no special-case status path; cross-machine status when cfg.store is cloud-backed (Layer T precedent). One synthetic instance_id namespace — the only new ledger key shape. |
| D3 | Signal handling | **SIGTERM** → `loop.stop()` drains in-flight `act_on_verdict` then exit 0; **SIGHUP** → re-read cfg from `--config PATH`, call `loop.reload(...)` (no thread restart); **SIGUSR1** → `logger.info(stats.snapshot_for_log())`. | Unix-daemon idiom. Operators expect these three. |
| D4 | Default cadence | **`sweeper.interval_s = 60`** (YAML default). | Gentle on RunPod GraphQL — B5a live smoke measured P50=460ms, P99=583ms at 5s cadence with no 429 (`docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` §9), so 60s leaves ~100x headroom. Halves daemon overhead vs `lifecycle.heartbeat_interval_s`. Cost-leak window is one extra sweep period, ~$0.013 on a $0.79/hr RunPod pod — not load-bearing. |
| D5 | Metrics surface | **Sibling `kinoforge_sweeper_*` Prom gauges**, same exposition format as B2 (`kinoforge cost --prom`). Counters: last_sweep_ts, sweeps_total, destroys_total, errors_total. Gauge: interval_s. Counter w/ label: `deferred_total{reason=...}`. | Same prefix → operator's textfile-collector cron concatenates both outputs into one scrape. PromQL groups by `host` label as primary axis (sweeper-instance dimension) — mirrors B2's `provider` label pattern. |
| D6 | Crash-recovery posture | **No extra recovery surface.** Existing `reaper/<id>` 30s TTL (`reaper_actor.py:27`) handles mid-sweep crash; next daemon restart picks up. | Verified against code. The substrate already owns mid-sweep crash recovery; B1 adds no parallel state machine. |
| D7 | Status vs metrics split | **`status` = human + `--json`** (sibling of `kinoforge cost --json` schema). **`metrics --prom` = standalone sibling subcommand** (scrape territory). No `--prom` on `status`; no `--json` on `metrics`. | Keeps operator-facing dial (`status`) crisp; keeps scrape target (`metrics`) machine-stable; separates the two cron rhythms (operator-on-demand vs every-30s textfile collector). `kinoforge cost --prom` untouched. |

## 4. Architecture

### 4.1 Module map

```
src/kinoforge/
  core/
    sweeper.py             NEW  ~140 LOC  SweeperLoop, _SweeperStats, _DeferredCounts
    sweeper_metrics.py     NEW  ~70  LOC  Prom + JSON renderers; no I/O
    config.py              EDIT +~30 LOC  SweeperConfig pydantic model + Config.sweeper
    reaper_actor.py        EDIT +1   LOC  one-line filter for `sweeper:*` ids in sweep()
  cli/
    _commands.py           EDIT +~180 LOC _cmd_sweeper_start/_stop/_status/_metrics
    _main.py               EDIT +~15 LOC  sweeper subparser + 4 subcommand bindings
tests/
  core/
    test_sweeper.py                NEW  ~12 tests
    test_sweeper_metrics.py        NEW  ~6  tests
    test_reaper_sweep.py           EDIT +1 test (synthetic-id filter)
    test_config.py                 EDIT +3 tests (sweeper YAML round-trip + defaults + reject)
  cli/
    test_cmd_sweeper.py            NEW  ~8 tests (status / metrics offline)
    test_cmd_sweeper_xprocess.py   NEW  ~3 tests (subprocess start→SIGTERM→stop)
  test_core_invariant.py           EDIT +1 (no I/O imports in core/sweeper.py)
examples/configs/sweeper.yaml      NEW
README.md                          EDIT  + Sweeper daemon section
PROGRESS.md                        EDIT  + B1 closeout strike
warm-reuse-tasks.txt               EDIT  + B1 closeout summary
```

No new substrate. No new ABCs. No new lock keys. One single-line edit to `core/reaper_actor.py`. The core-import-ban invariant (`tests/test_core_invariant.py`) is preserved: `core/sweeper.py` imports from `kinoforge.core.*` only (clock, reaper, reaper_actor, lifecycle); `core/sweeper_metrics.py` imports nothing from `kinoforge.providers.*` / `engines.*` / `sources.*`.

### 4.2 Layered architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  cli/_commands.py  (consumer)                                         │
│     _cmd_sweeper_start  → install signals → SweeperLoop().start()    │
│                         → block on Event until SIGTERM               │
│     _cmd_sweeper_stop   → ledger.read("sweeper:<host>") → SIGTERM    │
│     _cmd_sweeper_status → ledger.read(...) → human / --json render   │
│     _cmd_sweeper_metrics --prom → ledger.read(...) → prom render     │
└──────────────────────────────────────────────────────────────────────┘
                       │ calls
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  core/sweeper.py   IMPURE   (background thread + ledger.touch)        │
│     SweeperLoop.{start, stop, reload}                                │
│     SweeperLoop._run / _tick_once                                    │
│     _SweeperStats (counters + .fold(SweepReport))                    │
└──────────────────────────────────────────────────────────────────────┘
                       │ calls (per tick)
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  core/reaper_actor.py   (Layer V substrate — unchanged contracts)     │
│     sweep(store, ledger, registry_get_provider, thresholds, clock,    │
│           *, policy)                                                  │
│     act_on_verdict(...)  (B7 probe + B5a substrate gate inside)      │
└──────────────────────────────────────────────────────────────────────┘
```

The daemon is essentially `while not stop: sweep(...); fold(report); ledger.touch(...); wait(interval_s)` plus signal handling. Everything destructive happens inside `act_on_verdict`, which the daemon does not re-implement.

### 4.3 `SweeperLoop` shape (mirrors `HeartbeatLoop`)

```python
# src/kinoforge/core/sweeper.py
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import Policy
from kinoforge.core.reaper_actor import SweepReport, sweep
from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger(__name__)


@dataclass
class _DeferredCounts:
    session_claim: int = 0
    heartbeat_unknown_skipped: int = 0
    heartbeat_substrate_missing: int = 0


@dataclass
class _SweeperStats:
    """Cumulative tally across the daemon's lifetime.

    Folded forward each tick; never reset until process exit. The ledger
    sweeper-tick entry carries a snapshot of every counter so `status`
    survives daemon restarts via the cloud-store ledger.
    """

    sweeps_total: int = 0
    destroys_total: int = 0
    errors_total: int = 0
    last_sweep_ts: float = 0.0
    deferred: _DeferredCounts = field(default_factory=_DeferredCounts)

    def fold(self, report: SweepReport, *, now: float) -> None:
        """Walk `report.actions` and tally outcomes."""
        self.sweeps_total += 1
        self.last_sweep_ts = now
        for action in report.actions:
            if action.action == "destroyed_and_forgot":
                self.destroys_total += 1
            elif action.action == "deferred-session-claim":
                self.deferred.session_claim += 1
                _log.info(
                    "sweep deferred for %s — %s",
                    action.instance_id,
                    action.reason or "session-claim",
                )
            elif action.action == "failed":
                self.errors_total += 1
        # Substrate-missing entries surface in the snapshot but not in
        # actions (act_on_verdict returns no_op). Walk the snapshot once
        # to count them for the dashboard.
        for _entry, verdict in report.snapshot.values():
            if verdict.value == "HEARTBEAT_SUBSTRATE_MISSING":
                self.deferred.heartbeat_substrate_missing += 1
            elif verdict.value == "HEARTBEAT_UNKNOWN":
                self.deferred.heartbeat_unknown_skipped += 1

    def snapshot_for_ledger(self) -> dict[str, Any]:
        """Return the ledger.touch **extra kwargs for this tick."""
        return {
            "sweeps_total": self.sweeps_total,
            "destroys_total": self.destroys_total,
            "errors_total": self.errors_total,
            "deferred_session_claim": self.deferred.session_claim,
            "deferred_heartbeat_unknown_skipped": self.deferred.heartbeat_unknown_skipped,
            "deferred_heartbeat_substrate_missing": self.deferred.heartbeat_substrate_missing,
        }


class SweeperLoop:
    """Background thread that periodically calls `sweep` and tallies results.

    Mirrors `HeartbeatLoop` (`core/heartbeat_loop.py:81-175`) byte-for-byte
    on lifecycle (eager first tick, `Event.wait` sleep, daemon thread,
    bounded `join`, broad `try/except` in `_tick_once`).
    """

    def __init__(
        self,
        *,
        store: ArtifactStore,
        ledger: Ledger,
        registry_get_provider: Callable[[str], Callable[[], Any]],
        thresholds: Mapping[str, Any],
        clock: Clock | None = None,
        interval_s: float,
        host: str,
        policy: Policy,
        stats: _SweeperStats | None = None,
        logger_: logging.Logger | None = None,
        join_timeout_s: float = 5.0,
    ) -> None:
        if interval_s <= 0:
            raise ValueError(f"interval_s must be > 0; got {interval_s}")
        self._store = store
        self._ledger = ledger
        self._registry_get_provider = registry_get_provider
        self._thresholds = dict(thresholds)
        self._clock: Clock = clock or RealClock()
        self._interval_s = float(interval_s)
        self._host = host
        self._policy = policy
        self._stats = stats or _SweeperStats()
        self._logger = logger_ or _log
        self._join_timeout_s = join_timeout_s
        self._stop = threading.Event()
        self._reload_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name=f"kinoforge-sweeper-{host}", daemon=True
        )

    def start(self) -> None:
        """Start the background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to stop and join with a bounded timeout.

        Returns even if the thread is wedged inside a blocking
        `sweep` call — the thread is daemon, so process exit is never
        blocked. `join_timeout_s` is 5.0s by default to absorb the
        worst-case `act_on_verdict` round-trip on cloud providers.
        """
        self._stop.set()
        self._thread.join(self._join_timeout_s)

    def reload(
        self,
        *,
        policy: Policy | None = None,
        thresholds: Mapping[str, Any] | None = None,
        interval_s: float | None = None,
    ) -> None:
        """Swap policy / thresholds / interval without restarting the thread.

        SIGHUP target. Acquired under `_reload_lock` so a tick mid-flight
        either sees the old set entirely or the new set entirely; never
        a torn read across two fields.
        """
        with self._reload_lock:
            if policy is not None:
                self._policy = policy
            if thresholds is not None:
                self._thresholds = dict(thresholds)
            if interval_s is not None:
                if interval_s <= 0:
                    raise ValueError(f"interval_s must be > 0; got {interval_s}")
                self._interval_s = float(interval_s)
        # Wake the sleep so the new interval takes effect immediately,
        # not after the old interval elapses.
        self._stop.set()
        self._stop.clear()

    # ------------------------------------------------------------------
    # Internal — thread body
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Loop body: eager first tick, then sleep-and-tick until stopped."""
        while not self._stop.is_set():
            self._tick_once()
            # Event.wait so stop()/reload() can wake the sleep immediately.
            self._stop.wait(self._interval_s)

    def _tick_once(self) -> None:
        """One sweep + persistence cycle, wrapped in broad try/except.

        Any exception from sweep, fold, or ledger.touch is logged via
        `logger.exception` and swallowed. The loop is the only defence
        against silent thread death; future contributors must NOT lift
        this try/except outside the loop body.
        """
        try:
            with self._reload_lock:
                policy = self._policy
                thresholds = dict(self._thresholds)
            report = sweep(
                self._store,
                self._ledger,
                self._registry_get_provider,
                thresholds,
                self._clock,
                policy=policy,
            )
            now = self._clock.now()
            self._stats.fold(report, now=now)
            self._ledger.touch(
                f"sweeper:{self._host}",
                heartbeat_thread_tick=now,
                last_heartbeat=now,
                **self._stats.snapshot_for_ledger(),
            )
        except Exception:  # noqa: BLE001 — single bad tick must not kill the loop
            self._stats.errors_total += 1
            self._logger.exception("sweep tick failed on host=%s", self._host)
```

### 4.4 Synthetic ledger-entry: init + filter

**Init — one-time `Ledger.record` at startup.** `Ledger.touch` is a strict update (`lifecycle.py:569-573`): unknown ids are silent no-ops and `touch` filters the protected set `{"id", "provider", "tags", "created_at", "cost_rate_usd_per_hr"}` so a `**extra` write cannot rewrite `record`-owned fields. The daemon therefore MUST call `Ledger.record` exactly once at startup to materialise the synthetic entry; subsequent ticks call `Ledger.touch`. Implemented in `_cmd_sweeper_start` immediately before `loop.start()`:

```python
from kinoforge.core.interfaces import Instance

synthetic = Instance(
    id=f"sweeper:{host}",
    provider="_sweeper",       # reserved kind; never resolved by registry
    tags={},                   # no kinoforge selfterm / engine tags
    created_at=clock.now(),    # required by Ledger.record
    cost_rate_usd_per_hr=0.0,  # required by Ledger.record; daemon is free
)
# Idempotent on re-start: if a prior daemon's entry survives, record
# appends a duplicate — Ledger.entries() dedup-on-id at Layer S means
# the latest write wins. The pid touch on the next tick refreshes it.
ledger.record(synthetic)
ledger.touch(f"sweeper:{host}", pid=os.getpid())  # pid not in protected set
loop.start()
```

The `provider="_sweeper"` field is purely ledger.json bookkeeping — it is never dispatched against because the §4.4-filter (below) skips the entry before `provider_for` runs.

**Filter — one-line edit at `reaper_actor.py:334`** (top of the per-entry loop in `sweep`):

```python
for entry in entries:
    eid = str(entry["id"])
    if eid.startswith("sweeper:"):
        continue  # synthetic daemon-liveness entry; not a reapable pod
    # ... existing provider_for + classify dispatch ...
```

The prefix is reserved at substrate level — `sweeper:` joins `_lifecycle` (run_id) and `_cost_cache` (B2) as the third reserved kinoforge namespace. Documented in `Ledger.read` docstring extension.

### 4.5 `SweeperConfig` pydantic model

`src/kinoforge/core/config.py`:

```python
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Policy, Verdict


class SweeperConfig(BaseModel):
    interval_s: float = 60.0
    include_orphans: bool = False
    force_forget: bool = False
    host: str | None = None  # None → socket.gethostname()

    @field_validator("interval_s")
    @classmethod
    def _validate_interval(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"sweeper.interval_s must be > 0; got {v}")
        return v


class Config(BaseModel):
    # ... existing fields ...
    sweeper: SweeperConfig = Field(default_factory=SweeperConfig)


# Bridge:
def sweeper_policy_from_cfg(cfg: Config) -> Policy:
    """Build the Policy the daemon should use from cfg.sweeper."""
    act = set(DEFAULT_APPLY_POLICY.act_verdicts)
    if cfg.sweeper.include_orphans:
        act.add(Verdict.ORPHAN_REAP)
    if cfg.sweeper.force_forget:
        act.add(Verdict.UNROUTABLE)
    return Policy(act_verdicts=frozenset(act))
```

YAML surface (additive, backwards-compat — every existing config loads unchanged because `Config.sweeper` defaults via `Field(default_factory=SweeperConfig)`):

```yaml
# examples/configs/sweeper.yaml
compute:
  provider: runpod
  image: runpod/base:ubuntu22.04
  lifecycle:
    idle_timeout_s: 7200
    max_lifetime_s: 28800
    heartbeat_interval_s: 30
    grace_after_session_s: 300

sweeper:
  interval_s: 60          # default; override per-deploy
  include_orphans: false  # opt-in: adds ORPHAN_REAP to act_verdicts
  force_forget: false     # opt-in: adds UNROUTABLE to act_verdicts
  host: null              # default = socket.gethostname()
```

The `policy:` shape composes only the two opt-ins from the brief; future operators wanting a fully custom `act_verdicts` set get a follow-on B8 layer (`--policy policy.yaml` from Layer V §6). YAGNI for B1.

### 4.6 CLI surface

#### 4.6.1 `kinoforge sweeper start`

Blocking foreground supervisor. Flow:

1. Load cfg; build store + ledger + `EnvCredentialProvider` via existing `_build_store` / Layer V dispatch.
2. Resolve `host = cfg.sweeper.host or socket.gethostname()`.
3. Build thresholds dict from `cfg.lifecycle()` (mirrors Layer V `_cmd_reap`).
4. Build `Policy` via `sweeper_policy_from_cfg(cfg)`.
5. Print startup banner (see §4.7).
6. `Ledger.record(synthetic_instance)` (see §4.4 init block) to materialise `sweeper:<host>`; then `Ledger.touch("sweeper:<host>", pid=os.getpid())` to attach the PID.
7. Construct `SweeperLoop(...)`.
8. Install signal handlers on the calling thread (`signal.signal(SIGTERM, ...)` etc.) — handlers MUST run on the main thread (Python `signal` constraint); the daemon thread is the SweeperLoop's own thread.
9. `loop.start()`.
10. Block on `threading.Event().wait()` until SIGTERM sets the event.
11. `loop.stop()` (drains in-flight tick within `join_timeout_s`).
12. Exit code 0.

Signal handler contract:

```python
_exit_event = threading.Event()

def _handle_sigterm(signum, frame):
    _exit_event.set()  # main thread wakes from .wait(), runs loop.stop()

def _handle_sighup(signum, frame):
    new_cfg = load_config(args.config)
    new_policy = sweeper_policy_from_cfg(new_cfg)
    new_thresholds = _thresholds_from_cfg(new_cfg)
    new_interval = new_cfg.sweeper.interval_s
    loop.reload(policy=new_policy, thresholds=new_thresholds,
                interval_s=new_interval)
    _log.info("SIGHUP: cfg reloaded from %s", args.config)

def _handle_sigusr1(signum, frame):
    _log.info("sweeper stats: %s", stats.snapshot_for_log())
```

Flag set:

| Flag | Default | Effect |
|---|---|---|
| `--config PATH` / `-c` | required | cfg load source; also used by SIGHUP re-read. |
| `--interval-s N` | from cfg | override `cfg.sweeper.interval_s` at startup only (SIGHUP re-reads cfg, not the flag). |

`start` is **idempotent on host basis**: it does NOT pre-flight check for a prior daemon. Two daemons on the same host racing on `sweeper:<host>` ledger entry is a no-op race (both write the same shape; last write wins). The race is benign — both daemons would happily sweep, doubling cost. Operator-side responsibility (single systemd unit per host); documented in README.

#### 4.6.2 `kinoforge sweeper stop`

Flow:

1. Load cfg → host.
2. `entry = ledger.read(f"sweeper:{host}")`.
3. If `entry is None` → print `no sweeper running on host=<host>` → exit 1.
4. `pid = entry.get("pid")`; if missing → print `daemon liveness entry has no pid (stale?)` → exit 1.
5. `os.kill(pid, signal.SIGTERM)`.
6. Poll `ledger.read("sweeper:<host>")["heartbeat_thread_tick"]` every 1s until it stops advancing for 2 consecutive polls OR 30s timeout.
7. Exit 0 on success; exit 2 on timeout.

The `pid` field is written by the daemon at startup via a one-time `ledger.touch("sweeper:<host>", pid=os.getpid())` before `loop.start()`.

#### 4.6.3 `kinoforge sweeper status [--json]`

Flow:

1. Load cfg → host.
2. `entry = ledger.read(f"sweeper:{host}")`.
3. Compute `last_sweep_age_s = clock.now() - entry["heartbeat_thread_tick"]` when entry exists.
4. Compute `stale = last_sweep_age_s > 3.0 * cfg.sweeper.interval_s` (mirrors Layer V sentinel-window).
5. Render.

Human render (sibling of `kinoforge status` key=value style):

```
host=hostname.local
running=true
pid=12345
last_sweep_ts=2026-06-12T14:32:01-07:00
last_sweep_age_s=8
interval_s=60
stale=false
sweeps_total=1421
destroys_total=17
deferred_session_claim=3
deferred_heartbeat_unknown_skipped=0
deferred_heartbeat_substrate_missing=0
errors_total=0
```

`--json` (stable schema, future micro-layers add keys, never rename — B2 precedent):

```json
{
  "host": "hostname.local",
  "pid": 12345,
  "running": true,
  "last_sweep_ts": "2026-06-12T14:32:01-07:00",
  "last_sweep_age_s": 8,
  "interval_s": 60,
  "stale": false,
  "sweeps_total": 1421,
  "destroys_total": 17,
  "deferred_total": {
    "session-claim": 3,
    "heartbeat-unknown-skipped": 0,
    "heartbeat-substrate-missing": 0
  },
  "errors_total": 0
}
```

`running` derives from `not stale` AND `pid is not None`. Missing entry → all fields null except `host` and `running: false`.

#### 4.6.4 `kinoforge sweeper metrics --prom`

Flow: same `ledger.read` as status; render Prom text (LF-only line endings per B2 invariant); exit 0.

```
# HELP kinoforge_sweeper_last_sweep_ts Unix timestamp of most recent successful sweep.
# TYPE kinoforge_sweeper_last_sweep_ts gauge
kinoforge_sweeper_last_sweep_ts{host="hostname.local"} 1734036721

# HELP kinoforge_sweeper_sweeps_total Cumulative sweeps since daemon start.
# TYPE kinoforge_sweeper_sweeps_total counter
kinoforge_sweeper_sweeps_total{host="hostname.local"} 1421

# HELP kinoforge_sweeper_destroys_total Cumulative pods destroyed since daemon start.
# TYPE kinoforge_sweeper_destroys_total counter
kinoforge_sweeper_destroys_total{host="hostname.local"} 17

# HELP kinoforge_sweeper_deferred_total Sweeps that skipped a pod for a known reason.
# TYPE kinoforge_sweeper_deferred_total counter
kinoforge_sweeper_deferred_total{host="hostname.local",reason="session-claim"} 3
kinoforge_sweeper_deferred_total{host="hostname.local",reason="heartbeat-unknown-skipped"} 0
kinoforge_sweeper_deferred_total{host="hostname.local",reason="heartbeat-substrate-missing"} 0

# HELP kinoforge_sweeper_errors_total Per-tick exceptions caught by the loop body.
# TYPE kinoforge_sweeper_errors_total counter
kinoforge_sweeper_errors_total{host="hostname.local"} 0

# HELP kinoforge_sweeper_interval_s Configured sweep cadence.
# TYPE kinoforge_sweeper_interval_s gauge
kinoforge_sweeper_interval_s{host="hostname.local"} 60
```

Sweeper not running (no ledger entry) → emit zero counters with the configured host label (Prom convention: keep series alive). `last_sweep_ts` series omitted in that case — no honest value to emit.

Textfile-collector cron (documented in README):

```
*/30 * * * * kinoforge sweeper metrics --prom -c /etc/kinoforge.yaml > /var/lib/node_exporter/textfile/kinoforge_sweeper.prom
```

### 4.7 Banner template (B5a contract surface)

Printed by `start` immediately before `loop.start()`. Exists so daemon operators see the substrate-gate contract in their first log line — addresses the operator-confusion failure mode flagged in the brief:

```
kinoforge sweeper starting host=<host> interval_s=60 policy=[IDLE_REAP,OVERAGE_REAP,STALE_LEDGER]
  include_orphans=false force_forget=false
  pid=12345
B5a heartbeat-substrate gate is ACTIVE:
  providers with no shipped HeartbeatEndpoint satisfier emit
  HEARTBEAT_SUBSTRATE_MISSING and are NEVER reaped. SkyPilot is
  the only such provider today; B5b ships the satisfier when GPU
  quota lands. WARN-once-per-(provider,instance_id) deduped.
B7 cooperative session-claim probe is ACTIVE:
  entries whose orchestrator holds provision:<id> emit
  action="deferred-session-claim" and are skipped this pass;
  the next sweep re-evaluates.
```

Banner emitted via `_log.info(...)` so it lands in journald / docker logs alongside subsequent tick output.

## 5. Failure modes

| # | Mode | Handling |
|---|---|---|
| F1 | `sweep()` raises (provider construction crash, ledger I/O error) | `_tick_once` catch swallows; `stats.errors_total += 1`; `logger.exception` writes full traceback; next tick retries. |
| F2 | `ledger.touch` itself raises (cloud-store outage on sweeper-tick write) | Same as F1 — caught by the outer try/except. Daemon survives; `last_sweep_ts` does not advance until store recovers; `status` reports `stale=true`. |
| F3 | Two daemons on same host | Race on `sweeper:<host>` ledger writes — benign double-write. Both daemons sweep, doubling cost. Operator responsibility; documented in README. |
| F4 | SIGHUP with malformed cfg | `load_config` raises pydantic ValidationError; signal handler catches, logs WARNING with the parse error, leaves the loop running on the OLD config. Loud, never silent; next SIGHUP retries. |
| F5 | SIGTERM during in-flight `act_on_verdict` (e.g. mid-destroy on RunPod) | `loop.stop()` joins with 5s budget; if the destroy is still running, the daemon thread continues (daemon=True) but the main thread exits when the join times out. The pending `destroy_confirmed` either finishes (RunPod GraphQL ~500ms) or is interrupted at process exit. The instance is either destroyed (clean) or the next sweep on a new daemon classifies it correctly (STALE_LEDGER → forgot). No corruption — destroy is idempotent at provider level. |
| F6 | Clock skew on cloud-store ledger entries | Same as Layer U / V — `heartbeat_thread_tick` is wall-clock-relative to the writer's host. `status` and `metrics` compute age relative to the reader's `clock.now()`. Cross-host skew bounded by NTP. Operators on misconfigured NTP get a noisy `stale=true` reading; not catastrophic. |
| F7 | `sweep()` filter omits `sweeper:<host>` but a third-party writes ANOTHER `sweeper:*` entry | Filter is prefix-based; any `sweeper:*` is treated as kinoforge-reserved and skipped. Third-party usage of the prefix is undefined behaviour; documented in the reserved-namespace docstring. |
| F8 | Reload during a tick | `_reload_lock` serialises read-of-policy with write-from-SIGHUP. Tick either sees old set entirely or new set entirely — never torn. |

## 6. Test plan

All offline; zero live spend.

### 6.1 Unit — `tests/core/test_sweeper.py`

RED-first per `CLAUDE.md` TDD workflow.

```python
def test_interval_s_must_be_positive():
    """SweeperLoop(interval_s=0) raises ValueError; same for negative."""

def test_eager_first_tick_fires_before_first_sleep(fake_clock):
    """Mirror HeartbeatLoop AC: first sweep happens at t=0, not t=interval_s."""

def test_stop_set_wakes_event_wait_immediately(fake_clock):
    """stop() returns within join_timeout_s even when interval_s is huge."""

def test_bad_classify_does_not_kill_loop(fake_clock):
    """sweep() raises on tick 1 → stats.errors_total += 1; tick 2 still runs."""

def test_tick_writes_sweeper_ledger_entry(fake_clock):
    """After tick, ledger.read('sweeper:<host>') returns
    heartbeat_thread_tick == fake_clock.now()."""

def test_stats_fold_counts_destroys():
    """Fold a SweepReport with 2 destroyed_and_forgot + 1 failed →
    destroys_total == 2, errors_total == 1."""

def test_stats_fold_counts_deferred_session_claim():
    """Fold a SweepReport with 1 deferred-session-claim →
    deferred.session_claim == 1; INFO log emitted with the reason."""

def test_stats_fold_counts_substrate_missing_from_snapshot():
    """Snapshot has 1 HEARTBEAT_SUBSTRATE_MISSING verdict but no action
    (act_on_verdict returned no_op) → deferred.heartbeat_substrate_missing == 1."""

def test_reload_swaps_policy_under_lock():
    """reload(policy=new) → next tick uses new policy; reload during
    tick does not produce torn read."""

def test_reload_wakes_sleep_immediately(fake_clock):
    """reload(interval_s=5) at t=interval_s/2 → next tick fires at
    t=interval_s/2 + epsilon, not t=interval_s."""

def test_join_timeout_bounds_stop_call():
    """Wedge the sweep call; stop() returns within join_timeout_s + epsilon."""

def test_synthetic_id_not_classified_by_sweep():
    """Existing sweep() with 'sweeper:host' entry in the ledger → snapshot
    does not contain that key. Covers the reaper_actor.py:334 filter."""
```

### 6.2 Metrics + JSON — `tests/core/test_sweeper_metrics.py`

```python
def test_prom_format_emits_all_required_series():
    """Render stats → string; assert every HELP+TYPE present; assert
    all 3 deferred reason labels emit (even zero); UTF-8 + LF endings."""

def test_prom_omits_last_sweep_ts_when_no_entry():
    """No ledger entry → last_sweep_ts series absent; all counter series
    emit zero with the host label."""

def test_json_shape_lock_matches_status_spec():
    """Render stats → dict; assert exact key set + types match §4.6.3."""

def test_json_stale_flag_computed_correctly(fake_clock):
    """last_sweep_age_s > 3 * interval_s → stale=true; under threshold → false."""

def test_human_render_key_value_style():
    """Render stats → string; assert sibling of `kinoforge status` style
    (key=value lines, no JSON, no Prom decoration)."""

def test_running_false_when_pid_missing():
    """Entry exists but pid field absent → running=false."""
```

### 6.3 Filter in `sweep()` — `tests/core/test_reaper_sweep.py` (extension)

```python
def test_sweep_filters_sweeper_prefix_synthetic_ids():
    """Ledger has 2 real pod entries + 1 'sweeper:host' entry → snapshot
    contains 2 keys; 'sweeper:host' absent; no provider lookup attempted."""
```

### 6.4 CLI offline — `tests/cli/test_cmd_sweeper.py`

```python
def test_cmd_sweeper_status_no_entry(tmp_path):
    """No sweeper running → status prints `running=false`, exits 0."""

def test_cmd_sweeper_status_json_shape(tmp_path):
    """--json output parses; shape matches §4.6.3."""

def test_cmd_sweeper_metrics_prom_format(tmp_path):
    """--prom output validates against the §4.6.4 series list."""

def test_cmd_sweeper_stop_no_entry(tmp_path):
    """No sweeper running → exits 1 with `no sweeper running on host=...`."""

def test_cmd_sweeper_status_stale_flag(tmp_path):
    """Inject an old heartbeat_thread_tick; assert stale=true rendered."""

def test_banner_emitted_on_start_dry_run(tmp_path, caplog):
    """--config path; mock SweeperLoop.start to no-op; assert banner
    captured in caplog at INFO level."""

def test_cmd_sweeper_start_records_synthetic_entry(tmp_path):
    """--config path; mock SweeperLoop.start to no-op; assert
    ledger.read('sweeper:<host>') returns a dict with id, provider='_sweeper',
    pid==os.getpid(), created_at, cost_rate_usd_per_hr==0.0."""
```

### 6.5 Subprocess — `tests/cli/test_cmd_sweeper_xprocess.py`

Mirrors `tests/core/test_orchestrator_session_claim_xprocess.py` shape from B7 §5.2.

```python
def test_start_then_sigterm(tmp_path):
    """subprocess: `kinoforge sweeper start -c <fake-config>` with
    LocalProvider; wait until sweeper:<host> ledger entry appears; send
    SIGTERM; assert exit 0 within 10s."""

def test_status_after_clean_stop(tmp_path):
    """After SIGTERM exit, run `kinoforge sweeper status`; assert
    last_sweep_age_s > 0; ledger entry persists (not deleted)."""

def test_sighup_reloads_interval(tmp_path):
    """Start daemon at interval_s=5; send SIGHUP after modifying the
    config file to interval_s=1; assert subsequent ticks land at ~1s
    cadence (sample 3 ticks from ledger heartbeat_thread_tick deltas)."""
```

`FakeProvider` registered in `tests/conftest.py`; subprocess inherits the registration via standard `_adapters.py` import path. No real network.

### 6.6 Config — `tests/core/test_config.py` (extension)

```python
def test_sweeper_config_defaults_load():
    """Config with no `sweeper:` block → cfg.sweeper.interval_s == 60.0,
    include_orphans == False, force_forget == False, host is None."""

def test_sweeper_interval_negative_rejected():
    """interval_s=-1 → pydantic ValidationError."""

def test_sweeper_policy_bridge_composes_correctly():
    """sweeper_policy_from_cfg with include_orphans=True returns Policy
    whose act_verdicts contains ORPHAN_REAP plus DEFAULT_APPLY_POLICY set."""
```

### 6.7 Invariant scan — `tests/test_core_invariant.py` (extension)

`core/sweeper.py` and `core/sweeper_metrics.py` must not import from `kinoforge.providers.*`, `kinoforge.engines.*`, `kinoforge.sources.*`. Same scan shape as existing `reaper.py` purity check.

## 7. Acceptance criteria

| # | Criterion | Verified by |
|---|---|---|
| AC1 | `SweeperLoop._tick_once` calls `sweep()` then `ledger.touch("sweeper:<host>", ...)` with monotonic `heartbeat_thread_tick`. Synthetic entry materialised via `Ledger.record` at startup (§4.4 init); `touch`-only ticks thereafter. | `test_tick_writes_sweeper_ledger_entry`, `test_cmd_sweeper_start_records_synthetic_entry` |
| AC2 | Eager first tick fires before the first sleep (mirrors Layer U HeartbeatLoop). | `test_eager_first_tick_fires_before_first_sleep` |
| AC3 | `_tick_once` exception → `errors_total += 1` and loop continues. | `test_bad_classify_does_not_kill_loop` |
| AC4 | `stop()` interrupts `Event.wait` immediately and joins within `join_timeout_s`. | `test_stop_set_wakes_event_wait_immediately`, `test_join_timeout_bounds_stop_call` |
| AC5 | `_SweeperStats.fold` correctly tallies `destroyed_and_forgot`, `failed`, `deferred-session-claim`; INFO-logs deferred-session-claim with the reason. | `test_stats_fold_*` |
| AC6 | `_SweeperStats.fold` counts `HEARTBEAT_SUBSTRATE_MISSING` from `report.snapshot` (not from actions). | `test_stats_fold_counts_substrate_missing_from_snapshot` |
| AC7 | `reload(...)` swaps policy/thresholds/interval atomically under `_reload_lock`. | `test_reload_swaps_policy_under_lock` |
| AC8 | `reload(interval_s=...)` wakes the in-flight sleep immediately. | `test_reload_wakes_sleep_immediately` |
| AC9 | `sweep()` filters `sweeper:*` ids from snapshot. | `test_sweep_filters_sweeper_prefix_synthetic_ids` |
| AC10 | `Config.sweeper` defaults via `Field(default_factory=SweeperConfig)`; YAML without `sweeper:` block loads unchanged. | `test_sweeper_config_defaults_load` |
| AC11 | `cfg.sweeper.interval_s <= 0` → pydantic `ValidationError` at load time. | `test_sweeper_interval_negative_rejected` |
| AC12 | `sweeper_policy_from_cfg` composes `DEFAULT_APPLY_POLICY ∪ opt-ins` correctly. | `test_sweeper_policy_bridge_composes_correctly` |
| AC13 | `kinoforge sweeper status --json` emits the §4.6.3 schema. | `test_cmd_sweeper_status_json_shape` |
| AC14 | `kinoforge sweeper metrics --prom` emits the §4.6.4 series with UTF-8 + LF line endings; all three `deferred_total{reason=...}` labels emit even at zero. | `test_cmd_sweeper_metrics_prom_format` |
| AC15 | `kinoforge sweeper status` reports `stale=true` when `last_sweep_age_s > 3 * interval_s`. | `test_json_stale_flag_computed_correctly`, `test_cmd_sweeper_status_stale_flag` |
| AC16 | `kinoforge sweeper start` emits the §4.7 banner before starting the loop. | `test_banner_emitted_on_start_dry_run` |
| AC17 | Subprocess: `kinoforge sweeper start` then SIGTERM → exit 0 within 10s; ledger entry persists for `status`. | `test_start_then_sigterm`, `test_status_after_clean_stop` |
| AC18 | SIGHUP re-reads cfg from `--config PATH`; subsequent ticks honour new `interval_s`. | `test_sighup_reloads_interval` |
| AC19 | `core/sweeper.py` and `core/sweeper_metrics.py` import no provider / engine / source modules. | invariant scan extension |
| AC20 | Full test suite green; ruff / ruff-format / mypy clean; `pixi run pre-commit run --all-files` passes. | CI |

## 8. Risk register

| # | Risk | Mitigation |
|---|---|---|
| 1 | SIGHUP cfg parse failure leaves daemon on stale cfg | Caught in signal handler; WARNING logged; loop continues on the OLD config; next SIGHUP retries. Operator sees the parse error in journald. |
| 2 | `sweeper:<host>` collision when two operators run daemons on different machines that share a cloud-store-backed ledger | Each daemon writes its own host-keyed entry — no collision because the key includes hostname. The `metrics --prom` and `status` paths read the entry keyed by the LOCAL hostname; cross-host status is out of scope (one cluster-wide dashboard would aggregate multiple sweepers — B16 territory). |
| 3 | `socket.gethostname()` returns a non-unique value (containers default to a stable but shared hostname) | Operator overrides via `sweeper.host` YAML field. Documented in `examples/configs/sweeper.yaml`. |
| 4 | Daemon thread continues running after main thread exits via SIGTERM-during-destroy F5 | `daemon=True` ensures the OS reaps the thread at process exit. The pending RunPod destroy either completes (idempotent) or is retried on the next daemon start via STALE_LEDGER reclassification. |
| 5 | Two daemons on same host (manual restart without stop) | Benign double-write to `sweeper:<host>` ledger entry; both daemons sweep; doubles cost. Operator-side responsibility; readme documents systemd `Type=simple` + `Restart=on-failure` as the canonical posture. |
| 6 | `kinoforge sweeper stop` race against daemon SIGTERM handler | SIGTERM handler sets `_exit_event`; main thread calls `loop.stop()`; daemon thread joins or times out at `join_timeout_s`. `stop` CLI polls the ledger entry; if `heartbeat_thread_tick` continues advancing past the SIGTERM, polls time out at 30s and exit 2 — operator sees the loud failure. |

## 9. Forward-compat hooks

- **B3 (in-session warm-reuse retrofit).** Hot path stays free of any sweeper imports. B3 inherits B7's `provision:<id>` lock semantics; the daemon's `deferred-session-claim` action surfaces in its own counter without touching B3 code.
- **B8 (`--policy policy.yaml`).** Today's `sweeper.include_orphans` + `sweeper.force_forget` is a closed two-knob shape. When B8 lands, `sweeper.policy_path: str | None` joins the YAML block; `sweeper_policy_from_cfg` grows a load branch. No backwards-compat break — existing fields stay.
- **B5b SkyPilot satisfier landing.** `provider_heartbeat_supported("skypilot")` flips True in `core/heartbeat_endpoints.py`. The daemon's `deferred.heartbeat_substrate_missing` counter drops to zero; no sweeper code change. WARN-once dedup state at `reaper_actor.py:34` clears at process restart.
- **B16 (distributed sweeper, RayPool neighborhood).** Cross-host coordination beyond `acquire_lock`. Today's `sweeper:<host>` entry is host-scoped; B16 grows a cluster-wide rollup. The synthetic-id namespace stays compatible.
- **Future Grafana dashboard.** Reads `kinoforge sweeper metrics --prom` via the textfile-collector cron pattern documented in README. Stable labels: `host` on every series; `reason` on `deferred_total`. Future micro-layers add labels, never rename.

## 10. Task split (~7 tasks)

Order: a → b → c → d → e → f → g. Each atomic commit per `CLAUDE.md` durability rules.

| # | Task | Files | RED-first |
|---|---|---|---|
| a | `_SweeperStats` + `_DeferredCounts` + `SweeperLoop` (incl. `reload`) + offline unit tests | `core/sweeper.py` (new), `tests/core/test_sweeper.py` (new) | yes |
| b | One-line `sweep()` filter for `sweeper:*` ids + test extension | `core/reaper_actor.py` (+~3 LOC incl. comment), `tests/core/test_reaper_sweep.py` (+1 test) | yes |
| c | `SweeperConfig` pydantic model + `Config.sweeper` field + `sweeper_policy_from_cfg` bridge | `core/config.py` (+~30 LOC), `tests/core/test_config.py` (+3 tests) | yes |
| d | `sweeper_metrics.py` (Prom + JSON + human renderers) + offline tests | `core/sweeper_metrics.py` (new), `tests/core/test_sweeper_metrics.py` (new) | yes |
| e | `_cmd_sweeper_start/_stop/_status/_metrics` + `sweeper` subparser + signal handlers + synthetic `Instance` materialised via `Ledger.record` (§4.4 init) + offline CLI tests | `cli/_commands.py` (+~180 LOC), `cli/_main.py` (+~15 LOC), `tests/cli/test_cmd_sweeper.py` (new) | yes |
| f | Subprocess xprocess tests (start→SIGTERM→stop, SIGHUP reload) | `tests/cli/test_cmd_sweeper_xprocess.py` (new) | yes |
| g | `examples/configs/sweeper.yaml` + README Sweeper-daemon section + invariant scan extension + PROGRESS / warm-reuse-tasks.txt closeout strike with commit sha (after merge) | `examples/configs/sweeper.yaml` (new), `README.md`, `tests/test_core_invariant.py`, `PROGRESS.md`, `warm-reuse-tasks.txt` | partial |

Live spend: **$0**. `FakeProvider` + `LocalProvider` + subprocess-isolated start/stop tests cover the contract end-to-end.

## 11. Out of scope (re-confirming brief)

- Distributed sweeper (B16 RayPool territory).
- supervisord / asyncio rewrite.
- cgroup integration.
- Cross-host coordination beyond what `acquire_lock` already mandates.
- Sweeper-internal lock substrate.
- Ledger schema growth beyond the synthetic `sweeper:<host>` instance_id mandated by D2.
- `--policy policy.yaml` declarative policy file (B8 candidate).
- Operator-tunable `act_verdicts` set beyond `include_orphans` + `force_forget` opt-ins.
- B5b SkyPilot heartbeat satisfier (gated on A3 / A4 GPU quota; no B1 code change required when it lands).
- Hosted-engine spend folding into sweeper stats (B10 lights this path).

## 12. Sanity-checks against repo (verified 2026-06-13)

- `core/heartbeat_loop.py:151-175` — eager first tick, `Event.wait` sleep, broad try/except per iter, `daemon=True`, bounded `join` ✓ (B1 mirrors).
- `core/reaper_actor.py:27` — `_LOCK_TTL_S = 30.0` ✓ (D6 crash-recovery rests on this).
- `core/reaper_actor.py:214-227` — `_probe_session_claim_holder` + `action="deferred-session-claim"` with holder-pid reason ✓ (B1 surfaces in `stats.fold`).
- `core/reaper_actor.py:239-253` — `HEARTBEAT_SUBSTRATE_MISSING` arm with WARN-once dedup ✓ (B1 banner advertises the gate; no code duplication).
- `core/reaper_actor.py:334` — top of per-entry loop in `sweep()`; one-line filter inserts cleanly ✓.
- `core/lifecycle.py:525` — `Ledger.read(instance_id) -> dict | None` ✓ (B7-added; reused by `status` / `metrics`).
- `core/lifecycle.py:562-592` — `Ledger.touch` strict-update; unknown id is silent no-op; protected-set filter strips `{"id", "provider", "tags", "created_at", "cost_rate_usd_per_hr"}` ✓ — mandates §4.4 init via `Ledger.record(synthetic_instance)` at startup.
- `core/lifecycle.py:472-478` — `Ledger.record(instance, *, idle_timeout_s=None, max_age_s=None)` takes a typed `Instance` ✓ — daemon fabricates synthetic Instance from `kinoforge.core.interfaces`.
- `core/lifecycle.py:386` — `run_id: str = "_lifecycle"` reserved ✓ (existing reserved namespace; `sweeper:` joins it + `_cost_cache`).
- `core/heartbeat_endpoints.py:provider_heartbeat_supported` ✓ (consumed by `act_on_verdict`; B1 banner cites it).
- `core/reaper.py:DEFAULT_APPLY_POLICY` + `Policy` frozen dataclass ✓ (B1's `sweeper_policy_from_cfg` builds on top).

## 13. PROGRESS / warm-reuse-tasks.txt updates on B1 close

- `PROGRESS.md §B (B1 entry)` — strike with `~~B1. Layer W — ...~~ — CLOSED by commit <sha>`. Mirror the closeout block under §B (commit sha + spec/plan paths + one-line summary of surface area).
- `warm-reuse-tasks.txt` lines 523–555 — replace the open-task block with a closeout summary (commit sha + status: CLOSED).
- `successful-generations.md` — NOT applicable (no video; no new generation axis).
- `Layer V spec §6` — strike the "Layer W — `kinoforge sweeper` daemon" candidate; cite the B1 spec path.
