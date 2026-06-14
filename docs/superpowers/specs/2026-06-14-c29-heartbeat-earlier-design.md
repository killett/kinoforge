# C29 — Heartbeat starts BEFORE wait_for_ready (boot-phase protection)

## Status

Brainstormed 2026-06-14. Ready for plan-phase (`writing-plans`).

## Context

kinoforge's `HeartbeatLoop` currently starts at `src/kinoforge/core/orchestrator.py:1033` —
inside `deploy_session`, AFTER `_provision_instance_and_build_backend` returns. That helper
blocks on `engine.provision()` → `engine.wait_for_ready()` which polls the ComfyUI proxy
until `/system_stats` returns 200. During the entire boot phase (RunPod pod allocation +
bash provision script + ComfyUI clone + pip + custom-node clones + model download +
ComfyUI launch), the heartbeat never ticks. The C26 STALL_REAP + C27 RESTART_LOOP_REAP
predicates only protect the steady-state phase. During boot, only `boot_timeout` (default
30 min) protects.

### Evidence

- C28 Phase A v2 pod `6f1kau8g9kjm4q`: Wan 14B HF download capped at ~2 MB/s, GPU=0,
  CPU=1 for 25 min. `cfg.compute.lifecycle.stall_window_s=1200` (20 min) should have
  fired but the ledger showed no heartbeat fields — `hb_loop.start()` had not been
  called yet because `wait_for_ready` was still polling a 502 proxy. Pod kept burning
  $0.16/hr.
- C28 Phase A v4/v5: chronic restart loop ran undetected for the entire boot window
  because heartbeat couldn't see it. S3 diag snapshot at
  `s3://kinoforge-pod-diagnostics/boot-logs/c28-phase-a-20260613T235704-a1/`.
- `kinoforge status --id <pod>` shows zero liveness info during boot.

### Non-goals

- Changing C26 / C27 reap predicates — they stay byte-identical; C29 only changes WHEN
  the loop starts emitting ticks.
- Diagnosing the C30 root cause (why containers restart every ~30 s). C29 makes C30
  easier to investigate but does not solve it.
- Fixing C31 (`_destroy_safely` verify-and-retry). Separate.
- Introducing a new operator-facing cfg knob. C29 ships on by default.

## Goal

Heartbeat tick — including STALL_REAP + RESTART_LOOP_REAP predicates — starts the moment
the pod's container is RUNNING per the provider's view, not after ComfyUI's HTTP proxy
returns ready. `boot_timeout` demotes from "sole boot-phase protection" to "outer
backstop".

## Design

### 1. Loop start point

Inside `_provision_instance_and_build_backend`, **after** the RunPod-status poll loop on
`orchestrator.py:596` succeeds (`instance.status == "ready"`) and **before**
`resolved_engine.attach_get_instance` / `_provision_compute_once` runs at lines 602–605.

Rationale: at line 596 the container exists, provision_script is about to run, the
util endpoint returns real `uptime_seconds` and GPU data. This catches every observed
C28 boot-phase failure mode (HF download stall, chronic restart loop, slow custom-node
clone). Earlier placement (right after `create_instance` returns) would tick against a
pod still in RunPod's scheduling queue where the util endpoint returns nulls and the
counter accumulates noisy "low util" ticks against scheduling latency that
`boot_timeout` already bounds.

### 2. Construction owner — closure pattern

`deploy_session` builds a `start_heartbeat` closure that captures everything except
`instance.id`:

```
def _build_start_heartbeat_closure(
    *,
    ledger: Ledger,
    provider: ComputeProvider,
    interval: float,
    util_endpoint: UtilSnapshotEndpoint | None,
    cancel_token: CancelToken | None,
    provider_kind: str | None,
    stall_window_s: float | None,
    stall_gpu_threshold: float,
    stall_cpu_threshold: float,
    restart_loop_window_s: float | None,
    restart_loop_uptime_threshold_s: float,
    factory: Callable[..., HeartbeatLoopProtocol],
) -> Callable[[Instance], HeartbeatLoopProtocol]:
    def start_heartbeat(inst: Instance) -> HeartbeatLoopProtocol:
        loop = factory(
            ledger=ledger,
            provider=provider,
            instance_id=inst.id,
            interval_s=interval,
            util_endpoint=util_endpoint,
            cancel_token=cancel_token,
            provider_kind=provider_kind,
            stall_window_s=stall_window_s,
            stall_gpu_threshold=stall_gpu_threshold,
            stall_cpu_threshold=stall_cpu_threshold,
            restart_loop_window_s=restart_loop_window_s,
            restart_loop_uptime_threshold_s=restart_loop_uptime_threshold_s,
        )
        loop.start()
        return loop
    return start_heartbeat
```

`HeartbeatLoop` API is unchanged.

### 3. `_provision_instance_and_build_backend` — signature + return shape

New kwarg:

```
start_heartbeat: Callable[[Instance], HeartbeatLoopProtocol] | None = None
cancel_token: CancelToken | None = None
```

New NamedTuple return:

```
class ProvisionResult(NamedTuple):
    instance: Instance
    backend: GenerationBackend
    hb_loop: HeartbeatLoopProtocol | None
```

`hb_loop` is `None` when `start_heartbeat` is not supplied (hosted-engine paths,
`heartbeat_interval_s ≤ 0`, or caller-supplied-instance paths that never call this
helper).

Internal flow:

```
create_instance -> instance.id known
on_instance_created -> _record_then_install   # B3/B7 — ledger record + claim
poll loop -> instance.status == "ready"        # line 596
if start_heartbeat is not None:                # NEW
    try:
        hb_loop = start_heartbeat(instance)
    except Exception as exc:
        log.exception("start_heartbeat closure failed; falling through to late-start")
        hb_loop = None
else:
    hb_loop = None
attach_get_instance
try:
    _provision_compute_once(..., cancel_token=cancel_token)
except (ProvisionFailed, ProvisionTimeout, CapabilityMismatch, ValidationError):
    if hb_loop is not None:
        hb_loop.stop()
    destroy_instance(instance.id)
    raise
except Cancelled:                              # NEW — reap path
    destroy_instance(instance.id)              # idempotent on RunPod
    raise
backend = engine.backend(instance, cfg_dict)
return ProvisionResult(instance, backend, hb_loop)
```

### 4. `deploy_session` — wiring

Today's hb-construction block at `orchestrator.py:972-1033` becomes:

- Build `util_endpoint` (existing logic, hoisted up).
- Build `start_heartbeat` closure if `interval > 0` and `requires_compute` and a provider
  is resolved; else leave closure as `None`.
- Pass closure + cancel_token to both `_provision_instance_and_build_backend` call sites
  (cache-miss branch at lines 889–901, cache-hit branch at 911–934).
- Receive `result.hb_loop` from each call; store on the outer scope (replacing today's
  `hb_loop = factory(...); hb_loop.start()` block).
- Caller-supplied branches (`_caller_supplied_instance=True` at 883–887, 917–920) keep
  inline construction at the OLD position with byte-identical behaviour — that path has
  no boot phase to protect.
- `try: yield session; finally: if hb_loop: hb_loop.stop()` — unchanged.

### 5. Engine ABI — cancel_token in wait_for_ready

`GenerationEngine.wait_for_ready` Protocol gains:

```
cancel_token: CancelToken | None = None
```

`ComfyUIEngine.wait_for_ready` (`src/kinoforge/engines/comfyui/__init__.py:1430`) +
`DiffusersEngine.wait_for_ready` (`src/kinoforge/engines/diffusers/__init__.py:525`) +
`FakeEngine.wait_for_ready` (`src/kinoforge/engines/fake/__init__.py:300`) implementations
add at the top of the poll loop:

```
if cancel_token is not None:
    cancel_token.raise_if_set()
```

`engine.provision` callers of `self.wait_for_ready(...)` at
`engines/comfyui/__init__.py:1160` + `engines/diffusers/__init__.py:468` thread the
token through. `engine.provision` Protocol gains the same kwarg.
`_provision_compute_once` in `orchestrator.py` forwards the token into
`engine.provision`.

Default-None preserves every existing caller's behaviour without changes.

### 6. Cancelled handling

When a heartbeat reap fires mid-boot, `_maybe_fire_reap` (`heartbeat_loop.py:383`)
already calls `cancel_token.set()`. The new `cancel_token.raise_if_set()` at the top of
`wait_for_ready`'s poll loop raises `Cancelled` on the next iteration. `Cancelled`
propagates up through `engine.provision` → `_provision_compute_once` →
`_provision_instance_and_build_backend`.

The hb-reap path already destroyed the pod inside `_maybe_fire_reap`, but for symmetry
with operator-Ctrl-C-during-boot (which also sets the token without destroying the pod),
the outer `except Cancelled` in `_provision_instance_and_build_backend` re-destroys.
RunPod's `destroy_instance` is idempotent (404 on already-destroyed is logged + swallowed
by the provider) — log noise only.

## Test surface

### Modified (defaults keep most callers source-compatible)

| File | Reason | Update kind |
|---|---|---|
| `tests/core/test_orchestrator_heartbeat.py` | Asserts hb_loop construction timing + factory invocation | Modify — verify factory invoked from inside `_provision_*`, after RunPod-status poll, not from deploy_session post-provision |
| `tests/core/test_orchestrator_creds_default.py` | Spies on `_provision_instance_and_build_backend`, expects 2-tuple | Modify — spy returns `ProvisionResult(instance, backend, None)` |
| `tests/core/test_batch_creds_default.py` | Same | Modify — same |
| `tests/core/test_orchestrator_compute.py` | Likely exercises deploy_session shape | Audit + likely-modify tuple-destructure sites |
| `tests/core/test_orchestrator_no_reuse.py` | Touches `_provision_*` path | Audit |
| `tests/core/test_orchestrator_session_claim.py` | `_LazyClaim` + `on_instance_created` chain | Audit; claim seam unchanged |
| `tests/core/test_orchestrator_session_fields.py` | DeploySession field shape | Audit |
| `tests/engines/test_comfyui_wait_for_ready.py` | 4 tests passing kwargs explicitly | Modify — accept `cancel_token=None` default |
| `tests/engines/test_diffusers_wait_for_ready.py` | Same | Modify |
| `tests/engines/test_comfyui_provision_branch.py` | 3 fake `wait_for_ready` impls | Modify — add `cancel_token=None` kwarg to fake signatures |
| `tests/engines/test_diffusers_provision_branch.py` | 2 fakes | Modify |

### Unchanged

- `tests/core/test_heartbeat_loop.py` + `test_heartbeat_loop_util.py` — HeartbeatLoop API unchanged.
- `tests/core/test_ledger_read.py` + `test_ledger_touch.py` — ledger surface unchanged.
- `tests/core/test_heartbeat_endpoints.py` — util_endpoint construction unchanged.
- `tests/core/test_sweeper.py` — independent of boot timing.
- `tests/providers/test_heartbeat_parity.py` — provider.heartbeat parity unchanged.
- `tests/live/test_c26_phase_a_stall_detection_live.py`,
  `tests/live/test_c27_phase_a1_uptime_streak_live.py`,
  `tests/live/test_c27_phase_a2_alpine_restart_loop_live.py` — boot-phase reap is a
  superset of steady-state reap; predicates byte-identical.

### New unit tests

1. `tests/core/test_orchestrator_c29_start_heartbeat.py`
   - `test_start_heartbeat_invoked_after_runpod_poll_succeeds`
   - `test_start_heartbeat_not_invoked_for_caller_supplied_instance`
   - `test_start_heartbeat_not_invoked_when_hb_interval_is_none`
   - `test_start_heartbeat_closure_failure_falls_through_to_late_start`
   - `test_provision_result_namedtuple_shape`

2. `tests/core/test_orchestrator_c29_cancel_during_boot.py`
   - `test_cancel_token_set_mid_wait_for_ready_destroys_pod`
   - `test_engine_provision_failure_stops_hb_loop_before_destroy`
   - `test_hb_reap_during_boot_propagates_cancelled`

3. `tests/engines/test_comfyui_wait_for_ready_cancel.py`
   - `test_wait_for_ready_raises_cancelled_when_token_set_before_poll`
   - `test_wait_for_ready_raises_cancelled_when_token_set_mid_poll`
   - `test_wait_for_ready_no_cancel_token_preserves_today_behavior`

4. `tests/engines/test_diffusers_wait_for_ready_cancel.py` — mirror of (3).

5. `tests/engines/test_fake_wait_for_ready_cancel.py` — minimal Protocol parity.

Approximate scale: ~13 new + ~10 modified out of 1535 + 208 existing tests.

## Live smokes

All three RED-scaffolded + committed before any live spend per project durability rules.
Each smoke uses `pixi run preflight` precheck.

### Smoke A — STALL_REAP during boot

- Cfg: `heartbeat_interval_s=10`, `stall_window_s=60`, `stall_reap_enabled=true`,
  `restart_loop_reap_enabled=false`, `boot_timeout=600`.
- Provision script: `sleep 600` (no ComfyUI launch, no GPU work).
- Image: `kinoforge/wan-comfyui:latest` (prebake).
- Provider: RunPod, lowest-tier GPU.
- Expected: status=ready → start_heartbeat fires → 6 ticks of GPU=0, CPU=low →
  STALL_REAP at ~tick 7 → pod destroyed at ~150 s total. Cost ≤ $0.10.
- Pass: ledger `consecutive_low_util_count ≥ 6`; logs show `STALL_REAP fired`;
  provider returns terminated.

### Smoke B — RESTART_LOOP_REAP during boot

- Cfg: `heartbeat_interval_s=10`, `restart_loop_window_s=60`,
  `restart_loop_reap_enabled=true`, `stall_reap_enabled=false`,
  `restart_loop_uptime_threshold_s=30`.
- Provision script: `exit 1`. RunPod `restart_policy=always` → container restarts every
  ~10 s → `uptime_seconds < 30` each tick.
- Expected: counter increments → RESTART_LOOP_REAP at ~tick 7. Cost ≤ $0.10.
- Pass: `consecutive_low_uptime_count ≥ 6`; `RESTART_LOOP_REAP fired`; pod destroyed.

### Smoke C — boot-phase liveness in `kinoforge status`

- Cfg: `heartbeat_interval_s=10`, all reap disabled, `provision_script=sleep 120`.
- Action: start deploy, wait 30 s, run `kinoforge status --id <id>` from sibling shell.
- Expected: status shows `heartbeat_thread_tick`, `util_thread_tick`,
  `last_gpu_util_percent`, `last_uptime_seconds` populated within 2 * interval (20 s)
  of pod-ready.
- Pass: all 4 ledger fields non-null before the 30 s mark; explicit destroy after.

Total budget: ~$0.30 across all three smokes.

## Backward-compat assessment

C29 ships on by default with no new cfg knob.

| Surface | Today | C29 |
|---|---|---|
| `stall_reap_enabled=true` | Fires post-wait_for_ready | Fires post-line-596 (RunPod ready); covers `engine.provision` + `wait_for_ready` + steady-state |
| `restart_loop_reap_enabled=true` | Same | Same coverage extension |
| `kinoforge status --id <pod>` during boot | Empty heartbeat fields | Populated within 2 * interval of status=ready |
| `boot_timeout` | Sole boot-phase backstop | Outer backstop; reap predicates fire first when configured |
| Caller-supplied-instance branch | Late-start hb | Byte-identical (late-start preserved) |
| Hosted-engine path | hb_loop = None | Byte-identical |
| `cfg.heartbeat_interval_s ≤ 0` or `None` | No hb_loop | Byte-identical (closure not built) |

### Operator runbook addendum

> If a pod previously survived a 25-minute HF download and now reaps mid-download, raise
> `stall_window_s` above your worst-case provision-time stall, or set
> `stall_reap_enabled=false` to opt out of boot-phase STALL protection.

### Boot-vs-steady discriminator

The canonical "is this pod still booting" signal is the absence of `session_start` on
the ledger, NOT the absence of `heartbeat_thread_tick`. Operator tooling that keyed on
the latter as a boot proxy must migrate to the former.

## Internal-API deltas

| API | Today | C29 |
|---|---|---|
| `_provision_instance_and_build_backend` return | `(Instance, Backend)` | `ProvisionResult(instance, backend, hb_loop)` |
| `_provision_instance_and_build_backend` kwargs | `... on_instance_created` | `... on_instance_created, start_heartbeat, cancel_token` |
| `GenerationEngine.wait_for_ready` | `(instance, *, http_get, sleep, get_instance, timeout_s)` | `(..., cancel_token: CancelToken \| None = None)` |
| `GenerationEngine.provision` | varies | accept + thread `cancel_token` |
| `_provision_compute_once` | no cancel | accept + forward `cancel_token` |

Default-None kwargs keep every external caller source-compatible.

## Success criteria

1. A `cfg.compute.lifecycle.stall_window_s` stall during the boot phase fires
   STALL_REAP at the expected tick (`counter * interval ≥ window`) — same predicate
   semantics as steady-state today.
2. A chronic container restart loop during boot fires RESTART_LOOP_REAP at the
   expected tick.
3. `kinoforge status --id <pod>` returns non-empty liveness metrics
   (`heartbeat_thread_tick`, `util_thread_tick`, plus the 4 util fields when
   util_endpoint is supported) within `2 * heartbeat_interval_s` of pod creation +
   status=ready.
4. Warm-reuse (`_caller_supplied_instance=True`) produces byte-identical heartbeat
   behaviour to today.
5. A failed first tick (e.g. RunPod GraphQL pod-not-found while pod is still
   provisioning) does NOT crash the loop or set `cancel_token`. `_tick_once`'s broad
   `try/except` + `_read_util_safely`'s TransportError handling carries this property
   from steady-state to boot-phase unchanged.
6. All 3 live smokes pass within their per-smoke cost budget.
7. The 1535 unit + 208 C27 live tests pass after the ~10 modifications enumerated in
   the Test surface section.

## Risks + mitigations

- **R1: A test missed in the audit list breaks the green sweep.** Mitigation: plan-phase
  task 1 is a `pixi run test` baseline run; task 2 is the `rg` audit (re-grep for
  `wait_for_ready`, `_provision_instance_and_build_backend`, `HeartbeatLoop`,
  `hb_loop`); task 3 is a fail-fast `pixi run test` after the first edit batch to
  surface unanticipated callsites.

- **R2: `start_heartbeat` closure captures stale state when invoked from inside
  provision.** Mitigation: closure captures immutable references (cfg-derived
  thresholds, ledger handle, provider handle, util_endpoint, cancel_token instance).
  No mutation between deploy_session construction and provision invocation.

- **R3: Operator-Ctrl-C during boot leaks pod when `Cancelled` propagates uncaught.**
  Mitigation: `except Cancelled:` in `_provision_instance_and_build_backend` re-destroys
  the pod before re-raising. Idempotent under hb-reap (pod already destroyed) +
  load-bearing under Ctrl-C.

- **R4: `_record_then_install` (the `on_instance_created` callback) and
  `start_heartbeat` race on the ledger.** Both touch the ledger for the same
  instance_id. `_record_then_install` runs at create_instance return; `start_heartbeat`
  runs after the status-ready poll. Strict temporal ordering — no race.

- **R5: A boot reap fires while the `_LazyClaim` lock is held; the claim release
  expects `hold_until_first_tick` to see the first sentinel.** Mitigation: by the
  time start_heartbeat runs, `_record_then_install` has installed the claim and the
  hb_loop's eager first tick writes the sentinel before the `_LazyClaim.__exit__`
  poll wakes. Same temporal ordering as today's path.

## Plan-phase entry contract

The plan-phase agent should produce tasks in this order:

1. RED-scaffold the new unit test files in `tests/core/` + `tests/engines/` with
   xfail markers. Commit.
2. Thread `cancel_token` through `GenerationEngine.wait_for_ready` (Protocol + 3 impls);
   thread through `GenerationEngine.provision` + `_provision_compute_once`. Run
   unit tests; expect the new wait_for_ready_cancel tests to flip from xfail to pass.
3. Add `ProvisionResult` NamedTuple + `start_heartbeat` kwarg + extract
   `_build_start_heartbeat_closure` helper. Wire deploy_session's two call sites.
   Run unit tests; expect new orchestrator_c29_* tests to flip from xfail to pass.
4. Rewrite outer except block in `_provision_instance_and_build_backend` to handle
   `Cancelled` + stop hb_loop on existing exception types before destroy.
5. Update modified-tests list (creds_default spies, wait_for_ready impl test
   signatures, heartbeat construction-timing assertions).
6. Full `pixi run test` green sweep. Commit.
7. RED-scaffold 3 live smokes (A, B, C) + commit BEFORE live spend (durability rule).
8. `pixi run preflight` + run live Smoke A. Verify pass. Commit evidence.
9. Repeat for Smoke B + Smoke C.
10. PROGRESS.md + successful-generations.md update; close C29 in §C.
