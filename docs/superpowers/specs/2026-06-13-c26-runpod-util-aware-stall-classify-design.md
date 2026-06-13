# C26 — RunPod util-aware stall classify (design)

**Status:** DESIGN (brainstorm validated)
**Date:** 2026-06-13
**Author:** brainstorm session with Dr. Twinklebrane
**Tracker:** PROGRESS.md §C entry **C26**
**Predecessor:** C25 (PROGRESS.md §C, spec `2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md`, closeout SHA `7f901be`)
**Closes:** C25 Task 4 deferred acceptance gate (Wan + ComfyUI 2-CLI cold-skip ratio < 0.7)

---

## 1. Purpose

Today's `classify()` (`src/kinoforge/core/reaper.py`) returns `LIVE` whenever the
heartbeat-substrate sentinel (B5a) is fresh and the engine's `last_heartbeat` is within
`idle_timeout_s`. A pod that booted cleanly but whose in-pod workload has stalled
(model download hung, ComfyUI in restart loop, Python deadlock) is still classified
`LIVE` because heartbeats tick normally from the orchestrator side — heartbeats prove
that **the orchestrator is alive**, not that **the pod is making progress**.

C25 Task 4 surfaced the gap empirically: a real Wan + ComfyUI workload stalled at
~22 min wall on pod `uokf7x7cbfcunk` (sidecar `tests/live/_c25_smoke_evidence.json`)
with RAM / GPU / VRAM / disk near zero on the RunPod console, `uptimeInSeconds=5` at
kill (container had recently restarted), CPU 14%. Operator had to kill the pod
manually. C25's wire fix is orthogonal to the stall; what's missing is **observability
of the in-pod workload**.

C26 adds a util-snapshot read alongside the existing heartbeat write per tick, persists
the snapshot + a consecutive-low-util counter to the ledger, and extends `classify()`
with a new `STALL_REAP` verdict that intercepts the existing row-3 `LIVE` return when
GPU + CPU util have both been below configurable thresholds for a configurable window.
The holding orchestrator self-runs `classify()` per tick so the stall is caught
in-session (no cross-process race with B7); cross-process consumers (CLI `reap`, Layer
W sweeper, B3 attach-gate) consume the same persisted state via the same `classify()`.

The acceptance gate IS the deferred C25 Task 4 smoke (cold-skip ratio < 0.7), re-fired
on a new pod with C26 protections in place.

---

## 2. Decisions locked at brainstorm

Eight design tensions surfaced; all resolved before this spec was written.

1. **Detection site.** **Reaper-coordinated; holding orchestrator self-classifies
   every tick.** Stall detection lives in `classify()`; HeartbeatLoop runs
   `classify()` against its own ledger entry per tick. Cross-process sweepers
   (CLI reap, B1 sweeper, B3 attach-gate) run the same `classify()` against the
   persisted ledger state. Single decision tree, three consumers. B7's
   `provision:<id>` cooperative lock is unaffected — the holder self-detects from
   inside the lock; cross-process reapers never fight it. (Rejected: pure
   self-bail in HeartbeatLoop — locks the project into a refactor next layer.)

2. **Detection signal.** **Multi-axis util as primary, container uptime as
   discriminator.** Each tick captures `{gpu_util, cpu_percent, memory_percent,
   disk_percent, uptime_seconds}`. STALL_REAP fires when GPU AND CPU both below
   threshold for N consecutive ticks. Container uptime decreasing tick-over-tick
   resets the consecutive-low counter — handles legitimate container restart
   (RunPod migration, ComfyUI crash + restart) without false-positive. Memory and
   disk captured for observability / future Layer X dashboards but excluded from
   the threshold AND-clause (a hung Python process keeps memory high — memory-low
   is not a stall signal). (Rejected: uptime-reset as primary signal — misses
   hung-not-crashed cases.)

3. **Substrate boundary.** **New sibling `UtilSnapshotEndpoint` Protocol at
   `src/kinoforge/core/util_endpoints.py`,** parallel to `HeartbeatEndpoint`.
   `UtilSnapshot` is a frozen dataclass with all-Optional fields; satisfiers
   surface different subsets. `provider_util_supported(kind) -> bool` mirrors
   B5a's heartbeat capability gate. RunPod implements both endpoints; SkyPilot
   (later) can implement Heartbeat without Util; Bedrock implements neither.
   (Rejected: fattening `HeartbeatEndpoint` — mixes 'is pod cooperating with
   lease' with 'is pod doing work'.)

4. **Verdict shape + default policy.** **New `STALL_REAP` Verdict, appended at
   end of the StrEnum (public-contract additive), included in
   `DEFAULT_APPLY_POLICY`, cfg-gated by `stall_reap_enabled` (default True).**
   `kinoforge reap --apply` destroys stalled pods automatically. Legacy entries
   lacking util fields hit the 'data absent → LIVE' fall-through — zero behavior
   change for non-RunPod / pre-C26 entries. Cfg flag is the kill-switch for
   workloads that legitimately idle GPU. (Rejected: reusing IDLE_REAP with a
   reason field — conflates reusable-idle with stalled-must-destroy.)

5. **Threshold knobs.** **GPU + CPU AND-clause, consecutive-tick window,
   cfg-driven knobs.** New fields on `LifecycleConfig`:
   `stall_reap_enabled: bool = True`, `stall_window_s: float = 600.0`,
   `stall_gpu_threshold: float = 5.0`, `stall_cpu_threshold: float = 20.0`.
   Per-entry override via ledger field `stall_window_s` (mirrors Layer V
   `idle_timeout_s` per-entry override). 600 s window = ~2.5× typical Wan cold
   boot. (Rejected: per-engine `expected_boot_seconds` ABC — couples engines to
   numerics that drift with model size / NVMe / HF mirror speed.)

6. **Ledger schema.** **Flat fields + counter state.** Seven new fields on
   ledger entries: `last_util_tick`, `last_util_gpu_percent`,
   `last_util_cpu_percent`, `last_util_memory_percent`, `last_util_disk_percent`,
   `last_util_uptime_s`, `consecutive_low_util_count`. HeartbeatLoop owns the
   counter state machine; `Ledger.touch(**util_fields, consecutive_low_util_count=N)`
   persists. classify() is pure on the persisted state. (Rejected: nested dict /
   time-series ring buffer — premature for shipping today's gate.)

7. **Multi-GPU aggregation.** **MAX across devices.**
   `gpu_util_percent = max(g.gpuUtilPercent for g in runtime.gpus, default=None)`.
   Any GPU busy → not stalled. Correct for today's single-GPU Wan workload;
   safe-direction false-negative for multi-GPU workloads (operator tightens via
   `stall_gpu_threshold`). (Rejected: per-device classify — ledger schema bloat;
   premature.)

8. **Test strategy.** **Three layers — pure-replay classify tests + fake-GraphQL
   RunPod tests + 2-CLI live smoke re-firing the deferred C25 Task 4 gate.**
   Phase A of live smoke (FakeEngine with intentional sleep, ~$0.02) proves
   STALL_REAP fires end-to-end without depending on Wan stability. Phase B is
   the actual Wan + ComfyUI cold-skip ratio gate (~$0.50). RED scaffold
   committed before live spend per durability rule. Sidecar at
   `tests/live/_c26_smoke_evidence.json`.

---

## 3. Architecture

Diagrammatic flow (orchestrator-side, per heartbeat tick):

```
HeartbeatLoop._tick
    │
    ├── heartbeat.write(id, ts_local)         ── existing (B5a / C25)
    │
    ├── snap = util_endpoint.read_util(id)    ── NEW (C26)
    │       │  on TransportError → snap = None
    │       │
    │       └── RunPodGraphQLUtilEndpoint
    │             1 GraphQL query →
    │               runtime { uptimeInSeconds,
    │                         gpus { gpuUtilPercent memoryUtilPercent },
    │                         container { cpuPercent memoryPercent } }
    │             MAX across gpus for gpu_util_percent
    │             returns UtilSnapshot or None (pod gone)
    │
    ├── counter = _update_counter(prev_counter, prev_uptime_s, snap, thresholds)
    │       │  uptime decrease  → counter = 0
    │       │  snap is None     → counter unchanged
    │       │  all-axis-low     → counter += 1
    │       │  any-axis-high    → counter = 0
    │
    ├── ledger.touch(id,
    │       last_heartbeat=ts_local.timestamp(),
    │       heartbeat_thread_tick=now,
    │       last_util_tick=now,
    │       last_util_gpu_percent=snap.gpu_util_percent if snap else None,
    │       last_util_cpu_percent=snap.cpu_percent if snap else None,
    │       last_util_memory_percent=snap.memory_percent if snap else None,
    │       last_util_disk_percent=snap.disk_percent if snap else None,
    │       last_util_uptime_s=snap.uptime_seconds if snap else None,
    │       consecutive_low_util_count=counter,
    │   )
    │
    └── verdict = classify(entry, live_ids, now, **thresholds, stall_window_s=...)
            │
            if verdict == Verdict.STALL_REAP:
                ├── log.warning("STALL_REAP self-classified ...")
                ├── reaper_actor.act_on_verdict(id, verdict)  → destroys pod
                └── cancel_token.set()                         → in-flight engine aborts
```

Single source of truth: `classify()`. Three consumers:

| Consumer                            | Calls classify against                  |
|-------------------------------------|------------------------------------------|
| HeartbeatLoop (holding orchestrator) | Own ledger entry, every tick             |
| CLI `kinoforge reap --apply`         | All ledger entries, on operator request  |
| B1 Layer W sweeper daemon (future)  | All ledger entries, periodic             |
| B3 attach-gate (`_resolve_warm_instance`) | Candidate ledger entry, attach time    |

All four consume the same persisted state. No drift possible.

---

## 4. Components

### 4.1 New module — `src/kinoforge/core/util_endpoints.py`

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["UtilSnapshot", "UtilSnapshotEndpoint", "provider_util_supported"]


@dataclass(frozen=True)
class UtilSnapshot:
    """Per-tick provider-side resource metrics.

    All fields Optional — providers surface different subsets. Multi-axis
    classify() AND-clause (currently GPU + CPU) treats None as 'data
    unavailable' (does not contribute to stall verdict).
    """
    gpu_util_percent: float | None
    cpu_percent: float | None
    memory_percent: float | None
    disk_percent: float | None
    uptime_seconds: int | None


@runtime_checkable
class UtilSnapshotEndpoint(Protocol):
    """Provider-agnostic substrate for orchestrator-side util sampling.

    Contract invariants (every satisfier honors):

    - read_util(id) returns None when the instance is gone, the storage
      slot was never written, or all upstream fields are unavailable.
    - Transport failures (HTTP non-2xx, GraphQL rate-limit, SSH refused)
      propagate as TransportError. Consumers tolerate.
    - read_util is idempotent and side-effect-free (no provider-side
      mutation, no ledger writes).
    """

    def read_util(self, instance_id: str) -> UtilSnapshot | None: ...


_UTIL_SUPPORTED: frozenset[str] = frozenset({"local", "runpod"})


def provider_util_supported(provider_kind: str) -> bool:
    """Whether a wire-level UtilSnapshotEndpoint satisfier ships for kind.

    Mirrors B5a's provider_heartbeat_supported. Used by adapters /
    classify to gate util-aware behavior on providers whose substrate is
    not yet shipped.
    """
    return provider_kind in _UTIL_SUPPORTED
```

Core-import-ban invariant: this module imports nothing from
`kinoforge.providers.*`. Satisfiers self-register on import via `_adapters.py`.

### 4.2 New module — `src/kinoforge/providers/runpod/util.py`

```python
class RunPodGraphQLUtilEndpoint:
    """RunPod GraphQL runtime{} satisfier.

    Single query per tick:

        query GetRuntime($podId: String!) {
          pod(input: {podId: $podId}) {
            id
            runtime {
              uptimeInSeconds
              gpus { id gpuUtilPercent memoryUtilPercent }
              container { cpuPercent memoryPercent }
              # disk field name TBD by probe task — see §16 wire-discovery
            }
          }
        }

    gpu_util_percent = MAX across runtime.gpus (None when array empty).
    memory_percent = container.memoryPercent (pod-level, not per-GPU).
    Transport failures → TransportError. Pod gone (data.pod=null) → None.
    """

    def __init__(
        self,
        *,
        api_key: str,
        graphql_url: str = "https://api.runpod.io/graphql",
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None: ...

    def read_util(self, instance_id: str) -> UtilSnapshot | None: ...
```

Bearer-auth HTTP closure pattern matches B5a / C25 satisfier (shared
`_default_http_post` extracted in this layer if not already shared).

### 4.3 New module — `src/kinoforge/providers/local/util.py`

```python
class LocalUtilEndpoint:
    """Test seam. Returns snapshots from a programmable script.

    Self-registers via providers/local/__init__.py. Used by
    HeartbeatLoop integration tests and by the local-provider lifecycle
    path so that the 'local' provider passes provider_util_supported().
    """
    def __init__(self, *, script: list[UtilSnapshot | None] | None = None) -> None: ...
    def read_util(self, instance_id: str) -> UtilSnapshot | None: ...
```

### 4.4 Extended — `src/kinoforge/core/lifecycle.py`

`Ledger.touch(**extra)` already passes-through arbitrary kwargs into ledger
JSON. No method-signature change. Seven new keys are persisted by callers
using the existing seam:

- `last_util_tick: float` — wall-clock seconds, last successful util read
- `last_util_gpu_percent: float | None`
- `last_util_cpu_percent: float | None`
- `last_util_memory_percent: float | None`
- `last_util_disk_percent: float | None`
- `last_util_uptime_s: int | None`
- `consecutive_low_util_count: int` — counter state machine value

`Ledger.touch` docstring updated to enumerate these as sentinel-gate
contract members (mirrors the B5a `heartbeat_thread_tick` enumeration).

### 4.5 Extended — `src/kinoforge/core/reaper.py`

```python
class Verdict(StrEnum):
    LIVE = "LIVE"
    IDLE_REAP = "IDLE_REAP"
    ORPHAN_REAP = "ORPHAN_REAP"
    OVERAGE_REAP = "OVERAGE_REAP"
    STALE_LEDGER = "STALE_LEDGER"
    HEARTBEAT_UNKNOWN = "HEARTBEAT_UNKNOWN"
    HEARTBEAT_SUBSTRATE_MISSING = "HEARTBEAT_SUBSTRATE_MISSING"
    UNROUTABLE = "UNROUTABLE"
    STALL_REAP = "STALL_REAP"  # NEW (C26) — append-only public contract


DEFAULT_APPLY_POLICY = Policy(
    act_verdicts=frozenset({
        Verdict.IDLE_REAP,
        Verdict.OVERAGE_REAP,
        Verdict.STALE_LEDGER,
        Verdict.STALL_REAP,  # NEW
    })
)


def classify(
    entry: Mapping[str, Any],
    live_pod_ids: frozenset[str] | set[str],
    now: float,
    *,
    idle_timeout_s: float,
    max_lifetime_s: float,
    heartbeat_interval_s: float | None,
    grace_after_session_s: float,
    stall_window_s: float | None,           # NEW (None disables STALL_REAP)
    stall_gpu_threshold: float = 5.0,        # NEW
    stall_cpu_threshold: float = 20.0,       # NEW
) -> Verdict:
    # ... existing row 1 (STALE_LEDGER), row 2 (OVERAGE_REAP),
    #     row 7 (HB_UNKNOWN / HB_SUBSTRATE_MISSING) unchanged ...

    sentinel_window = 3.0 * heartbeat_interval_s

    if sent_age <= sentinel_window:
        if hb_age <= idle:
            # NEW — STALL check intercepts LIVE
            if stall_window_s is not None:
                util_tick = entry.get("last_util_tick")
                counter = int(entry.get("consecutive_low_util_count", 0) or 0)
                window = _resolve(entry, "stall_window_s", stall_window_s)
                if (
                    util_tick is not None
                    and (now - float(util_tick)) <= sentinel_window
                    and heartbeat_interval_s is not None
                    and counter * heartbeat_interval_s >= window
                ):
                    return Verdict.STALL_REAP
            return Verdict.LIVE
        return Verdict.IDLE_REAP

    # ... existing rows 5 & 6 unchanged ...
```

Per-entry `stall_window_s` override via `_resolve` matches Layer V pattern.
Counter is integer-typed in ledger; defensive `int(... or 0)` guards
against ledger-corruption per existing `_resolve` posture.

### 4.6 Extended — `src/kinoforge/core/config.py`

```python
class LifecycleConfig(BaseModel):
    # ... existing fields ...
    stall_reap_enabled: bool = True
    stall_window_s: float = Field(default=600.0, ge=0.0)
    stall_gpu_threshold: float = Field(default=5.0, ge=0.0, le=100.0)
    stall_cpu_threshold: float = Field(default=20.0, ge=0.0, le=100.0)
```

YAML schema:

```yaml
compute:
  lifecycle:
    stall_reap_enabled: true
    stall_window_s: 600.0
    stall_gpu_threshold: 5.0
    stall_cpu_threshold: 20.0
```

When `stall_reap_enabled: false`:

1. `_adapters.build_util_endpoint_for(cfg)` returns None → HeartbeatLoop
   skips util read entirely → ledger util fields never written.
2. **Callers of `classify()` pass `stall_window_s=None` when
   `cfg.compute.lifecycle.stall_reap_enabled is False`** — same pattern
   the existing classify already uses for `heartbeat_interval_s=None`
   when the heartbeat feature is disabled.
3. classify() falls through to LIVE on the row 3' guard. Backward-
   compatible.

Caller pattern (CLI `_cmd_reap`, B3 `_resolve_warm_instance`,
HeartbeatLoop self-classify):

```python
stall_window = (
    cfg.compute.lifecycle.stall_window_s
    if cfg.compute.lifecycle.stall_reap_enabled
    else None
)
verdict = classify(
    entry, live_ids, now,
    ...,
    stall_window_s=stall_window,
    stall_gpu_threshold=cfg.compute.lifecycle.stall_gpu_threshold,
    stall_cpu_threshold=cfg.compute.lifecycle.stall_cpu_threshold,
)
```

Clean kill-switch — operator with workload that legitimately idles GPU
for >stall_window_s sets `stall_reap_enabled: false` once, no further
changes needed.

### 4.7 Extended — `src/kinoforge/core/heartbeat_loop.py`

`HeartbeatLoop.__init__` gains optional `util_endpoint:
UtilSnapshotEndpoint | None = None` and `reaper_actor: ReaperActor | None
= None` and `cancel_token: CancelToken | None = None`. `_tick` extended
per §3 flow diagram.

Counter state machine encapsulated in a pure helper `_update_counter(
prev_counter: int, prev_uptime_s: int | None, snap: UtilSnapshot | None,
gpu_threshold: float, cpu_threshold: float) -> int` — testable in
isolation.

### 4.8 Extended — `src/kinoforge/core/_adapters.py`

```python
def build_util_endpoint_for(cfg) -> UtilSnapshotEndpoint | None:
    """Construct the per-provider util endpoint, or None when disabled.

    Returns None when:
      - cfg.compute.lifecycle.stall_reap_enabled is False, OR
      - provider_util_supported(cfg.compute.provider) is False.

    Mirrors build_heartbeat_endpoint_for. Provider-specific construction
    (RunPod API key from EnvCredentialProvider; Local no-arg) dispatched
    by provider kind.
    """
```

---

## 5. classify() decision-tree extension

Current decision tree (`reaper.py` rows from B5a docstring):

| Row | Condition                                              | Verdict                        |
|-----|--------------------------------------------------------|--------------------------------|
| 1   | pod not in live_pod_ids                                | STALE_LEDGER                   |
| 2   | pod_age > max_lifetime_s                               | OVERAGE_REAP                   |
| 7   | heartbeat data unavailable, provider has substrate     | HEARTBEAT_UNKNOWN              |
| 7'  | heartbeat data unavailable, provider lacks substrate   | HEARTBEAT_SUBSTRATE_MISSING    |
| 3   | sentinel fresh, hb_age ≤ idle                          | LIVE                           |
| 4   | sentinel fresh, hb_age > idle                          | IDLE_REAP                      |
| 5   | sentinel stale, pod_age > grace                        | ORPHAN_REAP                    |
| 6   | sentinel stale, pod_age ≤ grace                        | LIVE                           |

C26 inserts row 3' BEFORE row 3 returns LIVE:

| Row | Condition                                                              | Verdict     |
|-----|------------------------------------------------------------------------|-------------|
| 3'  | sentinel fresh, hb_age ≤ idle, util_tick fresh, counter × interval ≥ window | STALL_REAP  |
| 3   | sentinel fresh, hb_age ≤ idle, else                                    | LIVE        |

Fall-throughs to LIVE preserved when: `stall_window_s is None`, util_tick
absent (legacy entry), util_tick stale (>3× heartbeat_interval), or
counter × interval < window.

---

## 6. Per-tick state machine (`_update_counter`)

Pure function, fully testable:

```python
def _update_counter(
    prev_counter: int,
    prev_uptime_s: int | None,
    snap: UtilSnapshot | None,
    *,
    gpu_threshold: float,
    cpu_threshold: float,
) -> int:
    """Update consecutive-low-util counter.

    Container restart (uptime decrease) RESETS counter — handles
    legitimate restart blip per smoke evidence sidecar
    (uptimeInSeconds=5 at kill).

    Util read failure (snap=None) PRESERVES counter — transport hiccup
    should not reset progress toward stall verdict.

    Required axes (GPU + CPU) below threshold INCREMENTS counter; any
    above threshold RESETS to zero. None values in required axes treat
    as 'not low' (False in AND-clause) — partial data does not trigger
    STALL_REAP.
    """
    if snap is None:
        return prev_counter

    if (snap.uptime_seconds is not None
        and prev_uptime_s is not None
        and snap.uptime_seconds < prev_uptime_s):
        return 0

    gpu_low = (
        snap.gpu_util_percent is not None
        and snap.gpu_util_percent < gpu_threshold
    )
    cpu_low = (
        snap.cpu_percent is not None
        and snap.cpu_percent < cpu_threshold
    )
    if gpu_low and cpu_low:
        return prev_counter + 1
    return 0
```

`prev_uptime_s` is read from the ledger entry's `last_util_uptime_s`
field set on the previous tick. First tick after pod create has
`prev_uptime_s = None` → no comparison → counter starts at 0 or 1 per
that tick's `_all_axis_low` evaluation.

---

## 7. Cross-process consumer integration

### 7.1 CLI `kinoforge reap`

`_cmd_reap` already calls `classify()` per ledger entry; gains the three
new kwargs sourced from `cfg.compute.lifecycle`. STALL_REAP joins the
default-apply policy → `--apply` destroys stalled pods. Dry-run path
prints STALL_REAP entries with the same shape as IDLE_REAP. No CLI flag
churn.

### 7.2 B3 attach-gate (`cli/_commands.py:_resolve_warm_instance`)

Already calls a classify-equivalent cheap-first on candidate pods. Gains
the three new kwargs the same way. STALL_REAP → refuse attach + force
cold create (same code path as IDLE_REAP refuse).

### 7.3 B1 Layer W sweeper

`sweep()` consumes classify outputs and acts via `act_on_verdict()`. No
code change — adding STALL_REAP to the actionable verdict set is purely
a cfg + classify update. Sweeper inherits stall detection on next run.

### 7.4 HeartbeatLoop self-classify

The orchestrator that holds the pod runs `classify()` on its own ledger
entry at the end of every `_tick`. If `STALL_REAP`, it dispatches the
destroy path AND sets the `cancel_token`. The in-flight engine call
(via `pool.map` / `backend.result` / etc.) honors `cancel_token` per
the C18 / C19 cancel-hardening contract.

### 7.5 B7 cooperative-lock interaction

B7's `provision:<id>` lock holds through "first heartbeat tick lands"
in the holding orchestrator's `deploy_session.__enter__`. The
HeartbeatLoop runs inside the same process. Self-classify fires inside
the lock holder → reaper-actor destroy + cancel_token → engine.provision
raises → `deploy_session` exits via `finally` → lock released. Cross-
process reapers never see the lock held during a self-classified
STALL_REAP. No lock-overriding logic needed.

---

## 8. Wire payload — RunPod GraphQL `runtime{}`

Probed 2026-06-13 against test pod (probe sidecar to be captured in Task
1 of implementation plan):

```graphql
query GetRuntime($podId: String!) {
  pod(input: {podId: $podId}) {
    id
    runtime {
      uptimeInSeconds
      gpus {
        id
        gpuUtilPercent
        memoryUtilPercent
      }
      container {
        cpuPercent
        memoryPercent
      }
    }
  }
}
```

**Confirmed fields**: `uptimeInSeconds`, `gpus[].gpuUtilPercent`,
`gpus[].memoryUtilPercent`, `container.cpuPercent`,
`container.memoryPercent`.

**Disk field — TBD by probe task.** RunPod introspection blocked
(`__type` disabled). Probe in Task 1 — trial selection sets against a
small test pod. Candidates to try in priority order:

1. `container.diskInfo { utilPercent }`
2. `runtime.disk { utilPercent percentUsed }`
3. `container.storage { used total }` (then `disk_percent = used / total`)

Probe spend ~$0.001 on RTX A2000 (mirrors C25 Task a methodology).
Sidecar: `tests/live/_runpod_util_disk_probe.json`. If no field exists,
ship with `disk_percent = None` permanently; not in classify's threshold
AND-clause regardless.

---

## 9. Cfg surface

`compute.lifecycle.*`:

| Field                  | Type     | Default   | Effect                                              |
|------------------------|----------|-----------|-----------------------------------------------------|
| `stall_reap_enabled`   | bool     | `True`    | False → no util endpoint built, classify skips check |
| `stall_window_s`       | float    | `600.0`   | Threshold for `counter × interval ≥ window`         |
| `stall_gpu_threshold`  | float    | `5.0`     | GPU util percent below = "low"                      |
| `stall_cpu_threshold`  | float    | `20.0`    | CPU percent below = "low"                           |

Per-entry override via ledger field `stall_window_s` (matches Layer V
per-entry `idle_timeout_s` override pattern). Useful for one-off long-
boot workloads without changing global cfg. Operator workflow:

```bash
# Deploy with extended boot grace for a known-slow workload
kinoforge deploy cfg.yaml --stall-window-override 1800
```

`--stall-window-override` is a new CLI flag that writes the override
into the ledger entry at deploy time.

---

## 10. Test strategy

### 10.1 Pure-replay classify tests — `tests/core/test_reaper_stall.py`

| Test                                                              | Branch covered                          |
|-------------------------------------------------------------------|-----------------------------------------|
| `test_stall_reap_fires_when_consecutive_low_exceeds_window`       | Row 3' true → STALL_REAP                |
| `test_stall_reap_suppressed_when_counter_below_window`            | Row 3' false → LIVE                     |
| `test_stall_reap_suppressed_when_util_tick_stale`                 | Row 3' staleness guard                  |
| `test_stall_reap_suppressed_when_stall_window_s_none`             | Feature flag off                        |
| `test_stall_reap_suppressed_on_legacy_entry_missing_util_fields`  | Backward compat                         |
| `test_stall_reap_per_entry_override_via_ledger_field`             | Per-entry `stall_window_s` override     |
| `test_stall_reap_appended_at_end_of_verdict_enum`                 | Public-contract guard                   |
| `test_default_apply_policy_includes_stall_reap`                   | Policy default                          |
| `test_stall_reap_suppressed_on_provider_without_util_substrate`   | `provider_util_supported` gate          |

### 10.2 Provider-side fake-GraphQL tests — `tests/providers/test_runpod_util.py`

`FakeRunPodGraphQL` fixture in `tests/providers/conftest.py` (extends
C25's existing fake or shares a sibling helper). Programmable JSON
responses.

| Test                                                          | Covers                                          |
|---------------------------------------------------------------|-------------------------------------------------|
| `test_read_util_returns_max_gpu_across_devices`               | MAX aggregation                                 |
| `test_read_util_returns_none_when_pod_gone`                   | `data.pod = null` → None                        |
| `test_read_util_returns_partial_when_container_null`          | Partial fields → some-None UtilSnapshot         |
| `test_read_util_raises_transport_error_on_graphql_errors`     | Transport contract                              |
| `test_read_util_handles_empty_gpus_array`                     | `gpus=[]` → gpu_util_percent=None               |
| `test_read_util_passes_bearer_auth_header`                    | Wire-shape sanity (matches C25 pattern)         |

### 10.3 HeartbeatLoop integration tests — `tests/core/test_heartbeat_loop_util.py`

`FakeUtilEndpoint` programmable per-tick + `FakeReaperActor` + `FakeCancelToken`.

| Test                                                                  | Covers                                       |
|-----------------------------------------------------------------------|----------------------------------------------|
| `test_counter_increments_when_all_axis_low`                           | Counter state machine increment              |
| `test_counter_resets_on_uptime_decrease`                              | Container restart blip → reset to 0          |
| `test_counter_resets_when_gpu_recovers`                               | Any-axis-high → reset                        |
| `test_counter_preserved_when_util_read_fails`                         | Transport error tolerated                    |
| `test_self_classify_fires_destroy_and_cancel_token`                   | STALL_REAP self-classify → destroy + cancel  |
| `test_legacy_no_util_endpoint_path_omits_util_fields`                 | `util_endpoint=None` → ledger unaffected     |
| `test_partial_snapshot_with_none_gpu_does_not_increment`              | None-axis treated as not-low                 |

### 10.4 Counter helper tests — `tests/core/test_heartbeat_loop_counter.py`

Direct table-driven tests on `_update_counter` covering all 8 truth-table
combinations of `(uptime_decrease, snap_present, gpu_low, cpu_low)`.

### 10.5 Live smoke — `tests/live/test_c26_wan_warm_reuse_live.py`

**Two-phase smoke. RED scaffold committed BEFORE live spend per
durability rule.**

**Phase A — cheap STALL_REAP wire validation (~$0.02, RTX A2000):**

Deploy FakeEngine on RunPod with `provision_script` injecting an
intentional `python -c "import time; time.sleep({stall_window_s + 60})"`
loop. Assert:

1. STALL_REAP fires within `stall_window_s + heartbeat_interval_s × 2`.
2. Ledger entry shows `consecutive_low_util_count ≥ stall_window_s / heartbeat_interval_s`.
3. `act_on_verdict` destroyed the pod (provider.get_instance raises after
   destroy).
4. `cancel_token.is_set() == True`.
5. Engine.provision raised (the in-flight call aborted).

Sidecar: `tests/live/_c26_phase_a_smoke_evidence.json`.

**Phase B — Wan + ComfyUI cold-skip ratio (~$0.50, RTX A5000):**

Re-fires C25 Task 4 acceptance gate with C26 protections live. Two-CLI
warm-reuse smoke per B3 pattern.

Two acceptable smoke outcomes (both count as Phase B PASS for C26
acceptance criterion 8):

1. **CLEAN-PASS**: Wan + ComfyUI completes both gens; cold-skip
   benefit > 30% (gen 2 elapsed wall × (1 / 0.7) ≤ gen 1 elapsed wall).
   Closes C25 Task 4 deferred gate. `successful-generations.md` gets a
   new entry per the durability rule.
2. **PROVEN-PROTECTION**: Wan + ComfyUI regresses to the C25 Task 4
   stall symptom; STALL_REAP self-classify fires within the cfg window;
   pod destroyed; gen 2 forced to cold create. Phase B records outcome
   as PROVEN-PROTECTION — C26 succeeded at catching the stall the
   operator had to catch manually in C25.

Either outcome is a Phase B PASS. The smoke test classifies via the
ledger snapshot (consecutive_low_util_count + verdict trail) and the
two-gen elapsed wall ratio. FAIL only if STALL_REAP fails to fire
during a real stall (false-negative) OR fires during a clean run
(false-positive).

Sidecar: `tests/live/_c26_phase_b_smoke_evidence.json`.

**Total live spend budget**: ≤ $0.55. Preflight gate. Standard prompt
from `prompt-field-realistic.txt`.

### 10.6 Core-import-ban invariant — `tests/test_core_invariant.py`

Add `kinoforge.core.util_endpoints` to allowed core modules.
Vendor-SDK confinement scan: RunPod util satisfier confined to
`kinoforge.providers.runpod.util`. Same allowlist extension as B5a
heartbeat substrate.

---

## 11. Task split (preview for plan phase)

Anticipated task breakdown (writing-plans will refine into the exact
shape):

1. **Task 1 — Probe disk-util GraphQL field.** Live (~$0.001). Sidecar
   `tests/live/_runpod_util_disk_probe.json`. Decides disk_percent
   wire path. **First — informs Task 3 schema.**
2. **Task 2 — `UtilSnapshotEndpoint` Protocol + `UtilSnapshot` dataclass
   + capability gate.** Pure module. RED + GREEN.
3. **Task 3 — `RunPodGraphQLUtilEndpoint` satisfier.** Fake-GraphQL
   tests; field set finalized using Task 1 probe outcome. RED + GREEN.
4. **Task 4 — `LocalUtilEndpoint` test seam + self-registration on both
   providers.** Wires `_adapters.build_util_endpoint_for`.
5. **Task 5 — Ledger schema fields + `Ledger.touch` docstring update.**
6. **Task 6 — `_update_counter` helper + counter state machine
   integration in `HeartbeatLoop._tick`.** Table-driven tests.
7. **Task 7 — `LifecycleConfig` cfg knobs + YAML schema.**
8. **Task 8 — `classify()` STALL_REAP row 3' + Verdict enum extension
   + `DEFAULT_APPLY_POLICY` update.** Pure-replay tests.
9. **Task 9 — CLI flag `--stall-window-override` on `deploy` + ledger
   persistence.** Per-entry override wire-up.
10. **Task 10 — HeartbeatLoop self-classify wire-up + cancel_token +
    reaper-actor dispatch.** Integration tests.
11. **Task 11 — B3 attach-gate threshold-kwarg pass-through.** Single
    file change; pre-existing _resolve_warm_instance gains the three new
    kwargs.
12. **Task 12 — Core-import-ban allowlist + vendor-SDK confinement
    test update.**
13. **Task 13 — RED live smoke scaffold for Phase A and Phase B.**
    Committed BEFORE live spend per durability rule.
14. **Task 14 — Live smoke Phase A — FakeEngine intentional stall.**
    Sidecar capture. ~$0.02 spend.
15. **Task 15 — Live smoke Phase B — Wan + ComfyUI cold-skip ratio.**
    Re-fires C25 Task 4 acceptance gate. ~$0.50 spend.
16. **Task 16 — PROGRESS / spec / closeout / merge.**

Plan phase finalizes task ordering, granularity, and the writing-plans
HEREDOC code blocks.

---

## 12. Risk register

| #  | Risk                                                  | Likelihood | Mitigation                                          |
|----|-------------------------------------------------------|------------|-----------------------------------------------------|
| R1 | Disk-util field name unknowable (no GraphQL exposure) | Medium     | Ship with `disk_percent=None`. Not in AND-clause.   |
| R2 | RunPod GraphQL rate-limit at 30s × N pods             | Low        | B5a smoke saw P99=583ms at 5s × 1; 30s margin huge. |
| R3 | False-positive on multi-GPU pod with single dispatcher| Medium     | MAX aggregation; operator tunes via threshold cfg.  |
| R4 | Cancel-token race (engine returns just before destroy)| Low        | Cleanup idempotent (existing contract).             |
| R5 | Container restart blip false-fires STALL              | Medium     | Counter resets on uptime decrease (Q2 discriminator)|
| R6 | Legacy ledger entries trip new code paths             | Low        | `entry.get(...) is None` → fall-through to LIVE.    |
| R7 | Wan smoke regression of C25 Task 4 symptom            | High       | Phase A proves wire fix without depending on Wan; Phase B is best-effort acceptance gate, not block. |
| R8 | `consecutive_low_util_count` overflow on long pods    | Negligible | Int64; ledger.touch persists as JSON int.           |
| R9 | Util-query latency adds to per-tick overhead          | Low        | 1 extra GraphQL call per 30s (~50ms); negligible.   |

---

## 13. Acceptance criteria

C26 is closed when:

1. **Wire substrate**: `UtilSnapshotEndpoint` Protocol shipped at
   `core/util_endpoints.py`; RunPod + Local satisfiers self-register;
   `provider_util_supported` returns True for both; vendor-SDK
   confinement test green.
2. **classify() decision tree**: STALL_REAP row 3' implemented; pure-
   replay test suite green (all 9 §10.1 tests); Verdict enum order
   preserved.
3. **HeartbeatLoop integration**: counter state machine + self-classify
   + cancel_token wire-up shipped; all §10.3 integration tests green.
4. **Cfg surface**: four new knobs on `LifecycleConfig`; YAML schema
   round-trip green; kill-switch path (`stall_reap_enabled=False`)
   verified.
5. **CLI**: `--stall-window-override` persisted to ledger; per-entry
   override read by classify in §10.1 test.
6. **Cross-process consumers**: CLI `reap`, B3 attach-gate threaded
   through new kwargs; no behavior change when feature disabled.
7. **Live smoke Phase A**: STALL_REAP fired on FakeEngine intentional
   stall; pod destroyed; sidecar captured; spend ≤ $0.05.
8. **Live smoke Phase B**: EITHER Wan + ComfyUI cold-skip ratio < 0.7
   (closes C25 Task 4 deferred gate), OR STALL_REAP fired on Wan
   stall (proves protection on the regression case). Sidecar captured;
   spend ≤ $0.55.
9. **PROGRESS.md**: C26 entry struck-through; `## Single next action`
   updated; smoke evidence sidecar paths recorded.
10. **`successful-generations.md`**: amended if Phase B produced a
    video on the new tuple (`runpod`, `comfyui`, `wan`, `i2v`); skipped
    if Phase B STALL-protected without producing video.

---

## 14. Out of scope (carry-forwards to future layers)

- **SkyPilot util satisfier** (`providers/skypilot/util.py`). ssh-execs
  `nvidia-smi`, parses output. Plug-in satisfier; no substrate churn
  needed. Gated on A3 / A4 GPU quota landing AND a real stall on a
  SkyPilot deployment to motivate the spend. Spec hook here.
- **Bedrock util** — semantically inapplicable (serverless; no pod).
  `provider_util_supported("bedrock")` returns False forever.
- **Per-device classify** (one counter per GPU). Multi-GPU workloads
  where dispatcher idle + workers busy is a stall. Premature for today's
  single-GPU Wan smoke. Promote when a real multi-GPU workload trips a
  false-negative.
- **Adaptive baseline learned per (engine, model)** persisted in profile
  cache. Replaces hardcoded `stall_window_s` with learned value. More
  state, more complexity; ship after >1 cfg knob causes false-positives.
- **Layer X dashboard plotting `consecutive_low_util_count` time series.**
  Layer X reads ledger; plotting comes free once Layer X is built.
- **In-pod stage-progress beacon.** Selfterm script writes progress
  markers ("downloading models", "ComfyUI bound", "node install") to a
  side channel. Catches stalls during boot phases where util is
  legitimately low. Different layer; orthogonal to C26.
- **Time-series util_samples ring buffer** in ledger. Layer X dashboard
  candidate; not required by classify.

---

## 15. PROGRESS.md updates on C26 close

When C26 closes (PARTIAL or FULL):

- §C C26 entry: strike-through with `~~C26.~~ — CLOSED by ...` pattern.
- §C C25 entry: closeout amended — "Task 4 deferred gate closed by C26
  (closeout SHA `<sha>`)" appended to the existing CLOSED-PARTIAL line.
- §"Active workstream": cleared back to None (or next in queue).
- `## Single next action`: updated to next item in B-series or live-spend
  Tracks A/B.
- `successful-generations.md`: amended only if Phase B produced a video.

---

## 16. Wire-discovery notes

To capture during implementation:

- Disk-util GraphQL field name (probe Task 1).
- Empirical P50 / P99 latency of the new util query at 30s cadence
  (Phase A smoke captures).
- Real RunPod multi-GPU response shape: confirm `gpus` array structure
  matches the spec query (single-GPU pods today; multi-GPU not exercised
  until a real workload requests >1 GPU).
- Whether `runtime` is `null` during early boot (first ~5s after pod
  create); if yes, `read_util` returns UtilSnapshot with all-None fields
  → counter starts at 0 → no premature STALL.

Update this section + §8 when probes resolve.
