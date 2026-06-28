# Sweeper-side ephemeral reap — design spec

**Date:** 2026-06-28
**Status:** Approved (brainstormed 2026-06-28)
**Supersedes:** none (additive to
`2026-06-27-ephemeral-warm-reuse-discovery-design.md`)
**Owner:** kinoforge

## 1. Problem

`SweeperLoop` and `kinoforge reap` enumerate pods via `ledger.entries()`
(`core/reaper_actor.py:345`). Ephemeral pods never reach the disk
ledger: under `--ephemeral`, `STRICT_POLICY` sets `ledger_record=False`
which diverts writes to `EphemeralSession.in_memory_ledger`
(`core/lifecycle.py:528-531`); the dict dies with the process. The
sweeper daemon runs in its own non-ephemeral process and reads the
disk ledger — which has no ephemeral entries — so the sweeper is blind
to ephemeral pods.

Net effect: a wedged ephemeral pod whose in-pod selfterm watchdog has
crashed (worker hang, OOM, runaway loop) bills indefinitely. Sweeper
cannot help. Only safety nets are (a) in-pod selfterm, (b) explicit
`kinoforge destroy --id`, (c) provider-side spot interrupt, (d)
`BudgetTracker` mid-run kill.

### 1.1 Reproduction

1. `python -m kinoforge --ephemeral generate --config runpod-comfyui-wan-t2v.yaml ...`
2. SSH into pod; `pkill -9 -f selfterm`
3. `kinoforge sweeper start` (separate terminal) — daemon sees empty
   disk ledger; no IDLE/OVERAGE/STALL verdict ever fires.
4. Pod bills at $0.40/hr until manually destroyed.

### 1.2 Spec intent gap

`2026-06-27-ephemeral-warm-reuse-discovery-design.md` §4.2 promises:

> | 1 — sweeper reap | `kinoforge sweeper` destroys idle pod | One-line
> `ephemeral_index.remove(pod_id)` after successful
> `provider.destroy_instance` in `core/sweeper.py` |

The cleanup is wired (via `destroy_confirmed` chokepoint). The
discovery is missing: sweeper never selects an ephemeral pod for
destroy because ephemeral pods are absent from `ledger.entries()`.

### 1.3 Non-goals

- Cross-machine pod sharing (single-workspace assumption inherited
  from ephemeral-workspaces spec).
- Restart-loop detection for ephemeral pods (parallel to C27; deferred
  until first observed failure mode).
- SkyPilot ephemeral probe substrate (deferred until SkyPilot
  ephemeral usage stabilises).
- Cost-cache integration via `RuntimeProbe.cost_per_hr` (separate
  workstream).

## 2. Solution overview

Inside `sweep()`, union `ledger.entries()` with
`EphemeralIndex.rows()`. For each ephemeral-only row, call a new
`ComputeProvider.probe_runtime(pod_id)` method for live GPU/CPU
utilisation and container uptime. Synthesise a ledger-shape entry
flagged `kinoforge_ephemeral=True`. Route through a new
`_classify_ephemeral` branch that emits only the verdicts the sparse
substrate supports.

The ephemeral-index schema does NOT change. Discovery stays cold;
liveness is fetched on demand at sweep time. Existing cross-process
locks (`provision:<id>` from session_claim, `reaper/<id>` from
`act_on_verdict`) cover the race against concurrent warm-attach. No
new lock surface.

### 2.1 Verdict surface for ephemeral entries

| Verdict | Applies? | Source signal |
|---|---|---|
| `OVERAGE_REAP` | ✓ | `created_at_local` from index + `Lifecycle.max_lifetime_s` |
| `STALL_REAP` | ✓ | Probe: GPU+CPU low across N consecutive sweep ticks |
| `GC_404` (new) | ✓ | Probe returns `found=False` → remove index row, no destroy |
| `SKIP_NO_PROBE` (new) | ✓ | Provider returns `None` (substrate missing) → WARN-once dedup, no action |
| `PROBE_FAILED` (new) | ✓ | Provider raised on probe call (transient network/auth) → WARN, no action, retry next tick |
| `IDLE_REAP` | ✗ | No heartbeat substrate; selfterm covers graceful idle (false-positive risk during model load) |
| `HEARTBEAT_UNKNOWN` | ✗ | Replaced by `PROBE_FAILED` |
| `STALE_LEDGER` | ✗ | No ledger entry to be stale |

## 3. Module surface

### 3.1 New module — `src/kinoforge/core/runtime_probe.py`

```python
@dataclass(frozen=True)
class RuntimeProbe:
    pod_id: str
    found: bool                       # False → provider returned 404
    container_uptime_s: float | None  # None if found=False
    gpu_util_pct: float | None        # None if probe partial
    cpu_pct: float | None
    cost_per_hr: float | None         # optional; for future cost cache
    probed_at_local: str              # ISO timestamp, local TZ per project rule
    error: str | None = None          # WARN payload when found=True but partial
```

### 3.2 `ComputeProvider` ABC extension

`ComputeProvider` is an ABC at `src/kinoforge/core/interfaces.py:194`,
not a `Protocol`. Add a method with a `return None` default so
existing providers compile without modification:

```python
class ComputeProvider(ABC):
    # … existing methods unchanged …
    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:
        """Live runtime probe. Default: substrate missing (sweeper SKIPs)."""
        return None
```

Returning `None` signals "this provider lacks runtime-probe
substrate." Sweeper treats `None` as B5b parallel: WARN-once skip
keyed on `(provider_kind, pod_id)`. RunPod overrides; SkyPilot and
Local keep the default.

### 3.3 Per-provider implementation

| Provider | File | Behavior |
|---|---|---|
| `RunPodProvider` | `src/kinoforge/providers/runpod/__init__.py` (class at line 270) | Overrides `probe_runtime` — thin wrapper around existing GraphQL substrate used by C26 (`runtime.gpus[].gpuUtilPercent`, `runtime.container.cpuPercent`, `runtime.container.uptime`, `costPerHr`). 404 from GraphQL → `RuntimeProbe(found=False, ...)`. Network/auth error → raises (caller catches via `_probe_with_cache`). |
| `SkyPilotProvider` | `src/kinoforge/providers/skypilot/__init__.py` (class at line 429) | No override — inherits ABC default `return None`. Covers Lambda + Vast paths. |
| `LocalProvider` | `src/kinoforge/providers/local/__init__.py` | No override — inherits ABC default `return None`. Ephemeral on local is degenerate. |

### 3.4 `sweep()` changes — `core/reaper_actor.py`

```python
entries = list(ledger.entries())
ledger_ids = {str(e["id"]) for e in entries}

ephemeral_index = EphemeralIndex(store=store)
probe_cache: dict[tuple[str, str], RuntimeProbe | None] = {}

for row in ephemeral_index.rows():
    if row.id in ledger_ids:
        continue                                 # ledger wins on overlap
    probe = _probe_with_cache(
        provider_for_kind(row.provider),
        row.id,
        probe_cache,
    )
    synthetic = _synthesize_ephemeral_entry(row, probe)
    entries.append(synthetic)
```

`_probe_with_cache` wraps `provider.probe_runtime` and additionally
catches network/auth exceptions, converting them to a sentinel
`PROBE_FAILED` marker (distinct from `None` "substrate missing" and
distinct from `RuntimeProbe(found=False)` "provider 404"). Three-state
result: `RuntimeProbe` / `None` / `_PROBE_FAILED`.

`_synthesize_ephemeral_entry(row, probe_result)` returns a dict
matching the ledger entry shape but flagged with the
`kinoforge_ephemeral=True` sentinel. Populated fields:

- `id`, `provider`, `provider_kind` from row
- `created_at` from `row.created_at_local`
- `kinoforge_ephemeral = True` (sentinel for `classify` dispatch)
- `probe_state` — one of `"ok"`, `"not_found"`, `"no_substrate"`,
  `"failed"` (encodes the three-state plus the `found=False` case)
- `container_uptime_s`, `gpu_util_pct`, `cpu_pct` from probe when
  `probe_state == "ok"`; absent otherwise
- `last_heartbeat`, `heartbeat_thread_tick`, `session_claim`,
  `restart_count` → deliberately absent

### 3.5 `classify()` change — `core/reaper.py`

```python
def classify(entry, thresholds, clock, live_pod_ids, ...):
    if entry.get("kinoforge_ephemeral"):
        return _classify_ephemeral(entry, thresholds, clock, stall_history)
    # … existing ledger-pod branch unchanged …
```

New `Verdict` enum members: `GC_404`, `SKIP_NO_PROBE`,
`PROBE_FAILED`.

`_classify_ephemeral` decision tree (dispatches on `probe_state`):

```
entry["probe_state"] == "not_found"
  → GC_404                              # remove index row, no destroy

entry["probe_state"] == "no_substrate"
  → SKIP_NO_PROBE                       # WARN-once dedup, no action

entry["probe_state"] == "failed"
  → PROBE_FAILED                        # log WARN with dedup, retry next tick

# probe_state == "ok" — full util data available:

now - created_at > max_lifetime_s
  → OVERAGE_REAP

stall_history is None                   # one-shot CLI mode — see §3.10
  → LIVE                                # skip STALL_REAP (no history)

(stall window satisfied: N >= ceil(stall_window_s / sweeper_interval_s)
 consecutive samples each with gpu < stall_gpu_threshold AND cpu < stall_cpu_threshold)
  → STALL_REAP

else
  → LIVE
```

### 3.6 `act_on_verdict` change — `core/reaper_actor.py`

```python
elif v2 == Verdict.GC_404:
    ephemeral_index.remove(instance_id)
    action = "gc_404_removed"
elif v2 == Verdict.SKIP_NO_PROBE:
    _warn_once_no_probe(provider_kind, instance_id)
    action = "no_op"
elif v2 == Verdict.PROBE_FAILED:
    _warn_probe_failed(provider_kind, instance_id)   # dedup keyed on (provider_kind, pod_id, error_class)
    action = "probe_failed"
```

Existing `IDLE_REAP/OVERAGE_REAP/STALL_REAP` paths already route
through `destroy_confirmed` which calls `ephemeral_index.remove`. No
duplication needed.

### 3.7 `SweeperLoop` stall history — `core/sweeper.py`

The loop owns `dict[pod_id, collections.deque[float]]` (gpu_util
samples) and `dict[pod_id, collections.deque[float]]` (cpu samples).
Both bounded at `ceil(stall_window_s / interval_s) + 1`.

- Loop passes the dicts to `_sweep_fn` each tick.
- After each tick, the loop evicts entries whose `pod_id` is no longer
  in `ledger.entries() ∪ ephemeral_index.rows()` (avoids leak for
  pods that come and go).
- Restart resets history (in-memory only). One-window grace after
  restart is acceptable per §6.5.

`_classify_ephemeral` reads from the supplied history dict — does not
mutate it. `sweep()` appends the latest probe sample before calling
`classify`. Single writer, no contention.

### 3.8 `_SweeperStats` extension — `core/sweeper_metrics.py`

New counters: `gc_404_total`, `probe_failed_total`,
`skip_no_probe_total`. Surfaced in `snapshot_for_ledger()` and
SIGUSR1 dump.

### 3.9 CLI emit changes — `cli/_commands.py`

`_emit_reap_human` adds rows for `gc_404_removed`, `probe_failed`,
`no_op (SKIP_NO_PROBE)`. `_emit_reap_jsonl` adds matching literals.
No breaking change to existing action literals.

### 3.10 Behavior under `kinoforge reap` one-shot

A single CLI invocation gets one probe per pod — cannot satisfy
N-of-K stall window. One-shot mode:

- `OVERAGE_REAP` works (wall-clock comparison, no history needed)
- `GC_404` works (single probe is sufficient)
- `STALL_REAP` is **skipped** in one-shot mode; falls through to LIVE
- Operator gets STALL detection by running `kinoforge sweeper start`
  in continuous mode

Sweep gains a new `stall_history` kwarg (defaults to `None`).
`SweeperLoop` passes its in-memory deque dict; `_cmd_reap` leaves it
`None`. `_classify_ephemeral` checks `stall_history is None` and
short-circuits STALL_REAP to LIVE. Distinct from the existing
`policy` kwarg (which controls read-only vs apply mode).

## 4. Race safety

Existing locks already cover the new code paths; this section
documents which fires where so future readers know the invariants
hold.

| Lock | Owner | Coverage |
|---|---|---|
| `provision:<pod_id>` | `session_claim.hold_until_first_tick` via `orchestrator` (wraps cold-create AND warm-attach) | Sweeper's `_probe_session_claim_holder` (`reaper_actor.py:126`) demotes verdict to `SESSION_CLAIM` when held |
| `reaper/<pod_id>` | `act_on_verdict` for full `destroy_confirmed` round (`reaper_actor.py:210`) | Serialises concurrent sweep ticks on same pod |
| `ephemeral-index/_lifecycle` | `EphemeralIndex.add`/`remove` RMW lock (existing) | Reads (`rows()`) are lock-free — sweeper hot path |

### 4.1 Race resolution table

| Race | Resolution |
|---|---|
| Sweep probes during cold-create boot | First tick records 0%-util sample; stall window (N>1) not yet satisfied → LIVE. Boot completes before window fills. |
| STALL_REAP selected, attach starts before destroy | `act_on_verdict` acquires `reaper/<id>`, re-probes `provision:<id>` (held by attacher) → demote to `SESSION_CLAIM`, skip. |
| Two sweeper instances probe same pod | Both serialise on `reaper/<id>`. Second finds index row already removed → no-op. Idempotent. |
| Concurrent `kinoforge destroy --id` + sweep STALL_REAP | Both go through `destroy_confirmed`; first wins, second gets provider 404 → existing 404-swallow handles it. Idempotent. |
| Probe returns 404 while attacher mid-flight | `provision:<id>` lock blocks `GC_404`. Sweeper defers; next tick re-probes. Eventually consistent. |

### 4.2 Probe failure handling

`probe_runtime` raises network/auth error → `_probe_with_cache`
catches and converts to `probe_state="failed"`. Sweeper classifies as
`Verdict.PROBE_FAILED`, logs WARN with `(provider_kind, pod_id,
error_class)` dedup, takes no action. Next tick retries. Two dedup
sets, both same pattern as existing `_WARNED_SUBSTRATE_MISSING`:

- `_WARNED_PROBE_MISSING` — keyed on `(provider_kind, pod_id)`, used
  for `SKIP_NO_PROBE`.
- `_WARNED_PROBE_FAILED` — keyed on `(provider_kind, pod_id,
  error_class)`, used for `PROBE_FAILED`. Including `error_class` in
  the key lets a transient DNS failure log once, then a separate auth
  failure log once, instead of either masking the other.

## 5. Tests

### 5.1 Unit — `tests/core/test_runtime_probe.py`

| Test | Bug it catches |
|---|---|
| `RuntimeProbe` is frozen dataclass | Mutation across sweep ticks corrupts stall history |
| `RunPodProvider.probe_runtime(known_id)` returns populated probe | GraphQL field rename breaks parsing silently |
| `RunPodProvider.probe_runtime(404_id)` returns `RuntimeProbe(found=False, ...)` | 404 mis-handled as exception → sweep tick aborts |
| `RunPodProvider.probe_runtime` raises on network error (NOT swallowed) | Provider swallows → sweeper sees synthetic LIVE → wedged pod survives |
| `SkyPilotProvider.probe_runtime` returns `None` | Provider returns fake probe → reap fires on SkyPilot pod (wrong) |
| `LocalProvider.probe_runtime` returns `None` | Same |

### 5.2 Unit — `tests/core/test_classify_ephemeral.py`

| Test | Bug |
|---|---|
| Sentinel `kinoforge_ephemeral=True` routes to ephemeral branch | Heartbeat branch silently fires on ephemeral entry; missing `last_heartbeat` → HEARTBEAT_UNKNOWN forever |
| Sentinel absent → heartbeat branch unchanged (regression guard) | Ephemeral branch leaks into ledger-pod path |
| `created_at + max_lifetime_s < now` → `OVERAGE_REAP` | Wall-clock cap never enforced |
| `probe_found=False` → `GC_404` | Stale row never reaped → matcher keeps finding ghost |
| `probe_found=None` (substrate missing) → `SKIP_NO_PROBE` | SkyPilot pod silently classified LIVE → never reaped |
| Stall window: `(N-1)` zero-util samples → LIVE | Off-by-one fires STALL_REAP one tick early |
| Stall window: `N` zero-util samples → `STALL_REAP` | Window never satisfied → wedged pod survives |
| Stall window resets when one tick > threshold | Single recovery tick should clear suspicion |
| `IDLE_REAP` thresholds never consulted in ephemeral branch | Refactor copies idle code into ephemeral path → false positives during model load |
| Stall history `None` (one-shot CLI mode) → STALL_REAP skipped | One-shot reap kills pod on first 0%-util probe |

### 5.3 Unit — `tests/core/test_sweep_ephemeral_union.py`

| Test | Bug |
|---|---|
| `sweep()` with empty ledger + 1 ephemeral row → 1 entry classified | Union missing → ephemeral row invisible (regression of THIS spec) |
| `sweep()` with overlap (same id in ledger + index) → ledger entry used, not synthetic | Synthetic clobbers richer ledger entry → loses heartbeat data |
| `sweep()` caches `probe_runtime` per `(provider, pod_id)` within single tick | Same pod probed N times per tick → quota exhaustion |
| `sweep()` failure of one provider's probe does not abort sweep | One bad provider kills sweep for all others |
| `sweep()` policy=None (read-only) does NOT call `ephemeral_index.remove` | Read-only sweep mutates state |

### 5.4 Unit — `tests/core/test_sweeper_loop_stall_history.py`

| Test | Bug |
|---|---|
| `SweeperLoop` maintains per-pod probe deque; bounded length | Unbounded growth → memory leak |
| Loop restart resets history (in-memory only) | Stall window persists incorrectly across restart |
| Pod removed from index → deque entry evicted | Memory leak for pods that come and go |

### 5.5 Integration — `tests/integration/test_sweeper_reaps_ephemeral_stall.py`

Fake provider emitting scripted `RuntimeProbe` sequence:
- Tick 1: util=50% → LIVE
- Ticks 2..K (K = stall window size) : util=0% → STALL_REAP on tick K
- Assert `provider.destroy_instance(pod_id)` called once
- Assert `ephemeral_index.rows()` no longer contains `pod_id`
- Assert ledger never mutated (no `forget` on never-recorded entry)

### 5.6 Integration — `tests/integration/test_sweeper_skips_on_session_claim.py`

Hold `provision:<pod_id>` lock in test thread; run sweep tick; assert
verdict demoted to `SESSION_CLAIM`, `destroy_instance` NOT called.
Closes the attach-vs-reap race.

### 5.7 Integration — `tests/integration/test_reap_cli_ephemeral.py`

- `kinoforge reap` (no `--apply`) shows ephemeral pods in report
- `kinoforge reap --apply` GC_404 path removes stale row
- `kinoforge reap` one-shot does NOT emit STALL_REAP (per §3.10)

### 5.8 Visibility — `tests/integration/test_sweep_skypilot_ephemeral_warn_once.py`

SkyPilotProvider stub with `probe_runtime → None`. Two sweep ticks.
Assert exactly one WARN log line per `(provider_kind, pod_id)` dedup
key. Mirrors `_WARNED_SUBSTRATE_MISSING`.

### 5.9 AST invariant — `tests/test_classify_ephemeral_no_heartbeat_keys.py`

Walk `src/kinoforge/core/reaper.py`, find `_classify_ephemeral`
function body, assert it never reads keys `last_heartbeat`,
`heartbeat_thread_tick`, `session_claim`, `restart_count`. Prevents
future refactor from re-coupling to heartbeat substrate that doesn't
exist for ephemeral.

### 5.10 Live smoke — `tests/live/test_runpod_ephemeral_sweeper_smoke.py`

Pre-conditions: `pixi run preflight` green. Cost budget ~$0.30.

1. Start `kinoforge sweeper start` in background with
   `stall_window_s=120`, `max_lifetime_s=600`, `interval_s=30`.
2. Provision ephemeral pod via cold-create CLI path (prompt from
   `examples/configs/prompts/field-realistic.txt` per project rule).
3. SSH into pod; kill in-pod heartbeat process (simulate selfterm
   crash).
4. Wait `stall_window_s`; assert sweeper destroys pod (poll
   `kinoforge list` + RunPod GraphQL).
5. Assert `ephemeral-index.json` no longer contains pod id.
6. Sweeper teardown.

Cleanup paths verified: STALL_REAP + GC of index row. OVERAGE_REAP
covered offline by FakeClock test in §5.5.

### 5.11 RED-scaffold commit policy

Per project durability rule, the live smoke scaffold + new provider
`probe_runtime` methods + `_classify_ephemeral` function MUST be
committed BEFORE live invocation, even if RED (`pytest.skip`,
`xfail`, or scaffold-only impl). Subagent runs verify scaffold
committed before live spend.

## 6. Rejected alternatives

### 6.1 Pod writes heartbeats into ephemeral-index

Defeats the index's design intent (cold discovery channel, written
once at boot). Turns it into a parallel ledger with lock contention
every heartbeat tick. Two write paths for one piece of data — drift
risk.

### 6.2 Sibling per-pod heartbeat sidecar files

Two-file consistency burden (index row + heartbeat sidecar must stay
in sync across destroy). Cleanup paths multiply. Provider probe gives
same signal with one fewer file to maintain.

### 6.3 Refuse `--ephemeral` on providers without probe substrate

Too restrictive; ephemeral warm-reuse discovery is still useful on
SkyPilot/Local even without sweeper-side reap. WARN-once skip is the
operator-recoverable path.

### 6.4 IDLE_REAP via util-only

False positives during model load (Wan 14B weights take 4-8 min to
download into VRAM; GPU util is 0% throughout). In-pod selfterm
already covers graceful idle. Sweeper's job is failed-selfterm +
wedged-worker, both covered by STALL_REAP + OVERAGE_REAP.

### 6.5 On-disk stall history sidecar

Cross-restart history would require new disk-write path. Restart-
across-window grace is acceptable since operator-driven sweeper
restarts are rare.

## 7. Open questions

None — all design decisions locked during 2026-06-28 brainstorm.

## 8. Module list + commit plan

### 8.1 Files created

| Path | Purpose |
|---|---|
| `src/kinoforge/core/runtime_probe.py` | `RuntimeProbe` dataclass |
| `tests/core/test_runtime_probe.py` | §5.1 |
| `tests/core/test_classify_ephemeral.py` | §5.2 |
| `tests/core/test_sweep_ephemeral_union.py` | §5.3 |
| `tests/core/test_sweeper_loop_stall_history.py` | §5.4 |
| `tests/integration/test_sweeper_reaps_ephemeral_stall.py` | §5.5 |
| `tests/integration/test_sweeper_skips_on_session_claim.py` | §5.6 |
| `tests/integration/test_reap_cli_ephemeral.py` | §5.7 |
| `tests/integration/test_sweep_skypilot_ephemeral_warn_once.py` | §5.8 |
| `tests/test_classify_ephemeral_no_heartbeat_keys.py` | §5.9 |
| `tests/live/test_runpod_ephemeral_sweeper_smoke.py` | §5.10 |

### 8.2 Files modified

| Path | Change |
|---|---|
| `src/kinoforge/core/reaper.py` | New `_classify_ephemeral` branch; `classify` dispatches on sentinel; new `Verdict.GC_404`, `Verdict.SKIP_NO_PROBE` |
| `src/kinoforge/core/reaper_actor.py` | `sweep()` unions ephemeral_index rows; per-tick probe cache; `act_on_verdict` handles `GC_404` + `SKIP_NO_PROBE`; `_WARNED_PROBE_MISSING` set |
| `src/kinoforge/core/sweeper.py` | `SweeperLoop` owns bounded stall history; passes to sweep; evicts on pod removal |
| `src/kinoforge/core/sweeper_metrics.py` | `_SweeperStats` gains `gc_404_total`, `probe_failed_total`, `skip_no_probe_total` |
| `src/kinoforge/providers/runpod/__init__.py` | `RunPodProvider.probe_runtime` override wrapping existing GraphQL substrate (C26) |
| `src/kinoforge/core/interfaces.py` | `ComputeProvider` ABC gains `probe_runtime` method (default `return None`) |
| `src/kinoforge/cli/_commands.py` | `_emit_reap_human` + `_emit_reap_jsonl` new action literals |
| `docs/lifecycle.md` | Document ephemeral-aware sweep + verdict matrix |
| `docs/warm-reuse.md` | Cross-reference: sweeper reaps ephemeral pods |

### 8.3 Commit plan (per superpowers + project durability rules)

1. `feat(reaper): RuntimeProbe dataclass + ComputeProvider.probe_runtime interface` — runtime_probe.py + Protocol method + provider stubs returning None; unit tests RED for RunPod, GREEN for SkyPilot/Local
2. `feat(runpod): probe_runtime wraps existing GraphQL substrate` — RunPod impl; §5.1 RunPod tests GREEN
3. `feat(reaper): Verdict.GC_404 + Verdict.SKIP_NO_PROBE + _classify_ephemeral` — classify branch; §5.2 GREEN
4. `feat(reaper): sweep() unions EphemeralIndex with per-tick probe cache` — sweep union + cache; §5.3 GREEN
5. `feat(sweeper): SweeperLoop owns bounded stall history; passes to sweep` — loop change; §5.4 GREEN
6. `feat(reaper): act_on_verdict GC_404 removes index row; SKIP_NO_PROBE WARN-once` — act_on_verdict; §5.5 + §5.8 GREEN
7. `feat(cli): kinoforge reap emits ephemeral pod entries in dry-run + apply` — emit changes; §5.7 GREEN
8. `test(invariant): AST scan _classify_ephemeral consumes no heartbeat keys` — §5.9
9. `test(live): RED scaffold for RunPod sweeper-reaps-ephemeral-stall smoke` — §5.10 RED scaffold COMMITTED before live spend
10. `test(live): GREEN evidence for sweeper-reaps-ephemeral-stall smoke` — after live run
11. `docs(lifecycle,warm-reuse): document ephemeral-aware sweep` — doc updates

## 9. References

- `docs/superpowers/specs/2026-06-27-ephemeral-warm-reuse-discovery-design.md`
  — `EphemeralIndex`, `destroy_confirmed` chokepoint, cleanup paths.
- `docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md`
  — `EphemeralSession`, `STRICT_POLICY`, in-memory ledger diversion.
- `docs/superpowers/specs/2026-06-13-b1-sweeper-daemon-design.md`
  — `SweeperLoop`, `_SweeperStats`, sweeper teardown protocol.
- `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection.md`
  — C26 GraphQL probe surface (reused by `RunPodProvider.probe_runtime`).
- `docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md`
  — `provision:<id>` lock, `hold_until_first_tick`, race semantics.
- `src/kinoforge/core/reaper.py:24` — `Verdict` enum.
- `src/kinoforge/core/reaper_actor.py:210` — `reaper/<id>` lock.
- `src/kinoforge/core/reaper_actor.py:126` — `_probe_session_claim_holder`.
- `src/kinoforge/core/sweeper.py:111` — `SweeperLoop`.
- `src/kinoforge/core/warm_reuse/ephemeral_index.py` — `EphemeralIndex`.
- `src/kinoforge/core/session_claim.py:41` — `hold_until_first_tick`.
- `src/kinoforge/core/orchestrator.py` — `_LazyClaim` wrapper.
