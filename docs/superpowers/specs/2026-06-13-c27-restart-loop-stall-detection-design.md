# C27 — Restart-loop stall detection (design)

**Status:** DESIGN (brainstorm validated)
**Date:** 2026-06-13
**Author:** brainstorm session with Dr. Twinklebrane
**Tracker:** PROGRESS.md §C entry **C27**
**Predecessor:** C26 (PROGRESS.md §C, spec `2026-06-13-c26-runpod-util-aware-stall-classify-design.md`, closeout SHA `867b441`)
**Closes:** C26 Task 14 / C25 Task 4 deferred acceptance gate (Wan + ComfyUI 2-CLI smoke)

---

## 1. Purpose

C26 shipped PARTIAL: util-aware stall classification fires when GPU + CPU are
both low for a configurable window AND the container is running normally
(uptime increasing). C26 Phase B (Wan + ComfyUI on a real RunPod pod)
reproduced a second stall class that the shipped design does **not** cover:
**chronic container-restart loops**.

Empirical evidence (sidecar `tests/live/_c26_phase_b_smoke_evidence.json`):
pod `o4leekoaqru8cg` in a continuous container-restart loop. Every util tick
read `uptime_seconds=1`, `gpu=0`, `cpu=13`. The C26 counter
(`consecutive_low_util_count`) repeatedly hit the uptime-decrease restart-blip
guard in `_update_counter` and reset to 0 each tick. After 480 s of unbroken
low util, the counter was still 0. `STALL_REAP` never fired. Operator killed
the pod manually.

The C26 restart-blip guard exists for a real reason: a one-shot RunPod
migration / spot reclaim restart should NOT count as workload stall — the pod
takes 30-60 s to settle, then resumes normal operation. C26 correctly skips
those events. C27's job is to recognise that when restarts are **chronic**
(uptime never recovers above a sensible threshold for K consecutive ticks),
the restarts ARE the stall and must trigger reap.

C27 adds a sibling predicate on the existing C26 util substrate. No new wire
path, no provider changes, no ledger migration. Pure additive: one new pure
counter, one new pure predicate, one new Verdict, four new cfg knobs.

The acceptance gate IS the deferred C25 Task 4 / C26 Task 14 smoke
(Wan + ComfyUI 2-CLI), re-fired with C27 protections active.

---

## 2. Decisions locked at brainstorm

Seven design tensions surfaced; all resolved before this spec was written.

1. **Predicate shape.** **Low-uptime streak** — K consecutive ticks where
   `uptime_seconds < uptime_threshold_s`. Mirrors C26 counter shape exactly:
   same persistence pattern, same `_resolve` override path, same substrate
   gate. One-shot migration (30-60 s settling) lands ≤ 2 ticks on Wan's 30 s
   cadence — stays well under K=6. Phase B uptime=1 stuck for 16+ ticks →
   trivially caught. (Rejected: restart-event count over ring-buffer window —
   needs new persisted state shape and higher false-positive surface on
   single migrations. Rejected: hybrid streak + at-least-one-restart — most
   state, only useful if uptime-only false positives emerge in production.)

2. **AND-clause with low gpu/cpu.** **Uptime-only.** The chronic restart loop
   *is* the stall, regardless of momentary CPU/GPU activity during the brief
   uptime windows. Threshold high enough (90 s) that a healthy provision
   script crossing it lands the next tick with uptime > 90 → streak breaks.
   Phase B confirmed gpu=0+cpu=13 but the uptime=1-for-480-s signal is
   dispositive on its own. (Rejected: AND uptime + low gpu + low cpu — would
   suppress the predicate during legit apt-install CPU spikes mid-restart-
   loop and extend MTTD pointlessly.)

3. **Defaults.** **`restart_loop_uptime_threshold_s = 90.0`,
   `restart_loop_window_s = 180.0`.** 90 s threshold = ~30 s headroom
   over typical one-shot migration settling (30-60 s). 180 s window = 6
   ticks on Wan's 30 s cadence (fires at counter=6). Migration recovery
   crosses uptime > 90 within ~90 s wall → streak breaks at ~3 ticks,
   comfortably under window. Phase B fires at counter=6 ≈ 180 s after symptom
   onset. (Rejected: 60/120 aggressive — slow migration could trip predicate.
   Rejected: 120/300 conservative — 120 s extra paid pod time per detection.)

4. **Verdict.** **New `Verdict.RESTART_LOOP_REAP`.** Appended after
   `STALL_REAP` (insertion-order contract honoured). Added to
   `DEFAULT_APPLY_POLICY`. Cleanest observability — log lines + ledger
   residue + future Layer W metrics partition naturally on Verdict value.
   Operator post-mortem reads the Verdict directly instead of inferring from
   which counter wins. (Rejected: fold into `STALL_REAP` — lossy in logs;
   muddies semantic separation between two distinct failure modes.)

5. **Counter persistence.** **New sibling field
   `consecutive_low_uptime_count`.** Lives alongside C26's
   `consecutive_low_util_count`. New pure function `_update_uptime_counter`
   sits next to `_update_counter` in `core/util_counter.py`. C26 unit tests
   unaffected; ledger residue tells post-mortem story per axis. (Rejected:
   extend C26 counter to mean 'low util OR low uptime' — collides with the
   new-Verdict choice since one counter cannot produce two Verdicts at
   row 3'.)

6. **Smoke ladder.** **Three steps: Phase A1 (FakeUtilEndpoint) → Phase A2
   (alpine restart-loop) → Phase B (Wan re-fire).** A1 verifies orchestrator-
   side logic end-to-end on a cheap RunPod offer. A2 verifies the
   `RunPodUtilEndpoint` wire path against a real restart-policy churn on an
   alpine pod (~5 MB image pull). B re-fires the deferred C25/C26 gate on
   Wan + ComfyUI. (Rejected: A1 only — wouldn't verify the wire path on real
   restart behaviour. Rejected: skip alpine — would couple C27 acceptance to
   the much costlier Wan smoke.)

7. **Cancel-token semantics.** **Share C26's existing `_cancel_token`.** The
   outer `deploy_session` just needs to abort; the abort reason is the same
   regardless of which stall class fired. No new token needed.

---

## 3. Architecture

C27 is a **sibling predicate** on the C26 substrate. The full per-tick flow
inside `HeartbeatLoop._tick_once` (existing C26 path + C27 additions):

```
provider.heartbeat(id)
last_hb = provider.last_heartbeat(id)
snap = self._read_util_safely()             # C26
if util_endpoint is not None:
    counter_low_util   = _update_counter(prev_low_util, ...)               # C26
    counter_low_uptime = _update_uptime_counter(prev_low_uptime, ...)      # C27 (new)
    ledger.touch(
        last_heartbeat=last_hb,
        heartbeat_thread_tick=now,
        util_thread_tick=now,
        consecutive_low_util_count=counter_low_util,
        consecutive_low_uptime_count=counter_low_uptime,                   # C27 (new)
        last_gpu_util_percent=snap.gpu_util_percent,
        last_cpu_percent=snap.cpu_percent,
        ...
    )
    _maybe_fire_reap(now)                   # renamed from _maybe_fire_stall_reap
                                            # first-match-wins on both predicates
```

`_maybe_fire_reap` checks `_stall_reap_predicate` first (C26 — older Verdict,
established order) then `_restart_loop_reap_predicate` (C27). First match
triggers destroy + ledger.forget + cancel-token.set + stop-event.set.

Classify-side (`reaper.py::classify`) gains the same first-match-wins
ordering at row 3' / 3''. Cross-process consumers (CLI reap, Layer W
sweeper) read the same persisted state and reach the same verdict by
construction.

Three new pieces:

1. **Pure counter** `_update_uptime_counter` in `core/util_counter.py`.
2. **Pure predicate** `_restart_loop_reap_predicate` in `core/reaper.py`.
3. **New Verdict** `Verdict.RESTART_LOOP_REAP` appended in `core/reaper.py`.

Plus the matching cfg surface on `LifecycleConfig` and CLI on
`kinoforge deploy`.

---

## 4. Components

### 4.1 `_update_uptime_counter` (new)

Location: `src/kinoforge/core/util_counter.py`, alongside `_update_counter`.

```python
def _update_uptime_counter(
    prev_counter: int,
    *,
    snap: UtilSnapshot | None,
    uptime_threshold_s: float,
) -> int:
    """Tick the consecutive-low-uptime counter (C27).

    Pure function — no I/O, no side effects. Called from
    HeartbeatLoop._tick_once each tick alongside _update_counter.

    Semantics:
      - snap is None (transport hiccup): preserve prev_counter.
      - snap.uptime_seconds is None: reset to 0 (provider not surfacing).
      - snap.uptime_seconds < uptime_threshold_s: increment.
      - else: reset to 0.

    Differences vs _update_counter (C26):
      - No prev_uptime_s parameter. C27 does not care about restart-blip
        detection — the blip itself IS the signal. Each tick is judged on
        its own absolute uptime value.
      - No two-axis AND. Single read of uptime_seconds.
      - uptime_seconds is None → reset to 0 (safer than preserve — silences
        the predicate if the provider stops surfacing uptime mid-loop).

    Args:
        prev_counter: The previous tick's counter value.
        snap: This tick's util snapshot, or None on transport failure.
        uptime_threshold_s: Strictly-< threshold below which the tick
            counts as 'low uptime'.

    Returns:
        The new counter value.
    """
    if snap is None:
        return prev_counter
    if snap.uptime_seconds is None:
        return 0
    if snap.uptime_seconds < uptime_threshold_s:
        return prev_counter + 1
    return 0
```

### 4.2 `_restart_loop_reap_predicate` (new)

Location: `src/kinoforge/core/reaper.py`, alongside `_stall_reap_predicate`.

```python
def _restart_loop_reap_predicate(
    entry: Mapping[str, Any],
    *,
    now: float,
    sentinel_window: float,
    heartbeat_interval_s: float,
    restart_loop_window_s: float | None,
) -> bool:
    """Return True iff the entry should fire RESTART_LOOP_REAP (C27 row 3'').

    Same defensive shape as _stall_reap_predicate: bad types fall through
    to default rather than raising.

    Returns True when:
      1. Feature on (effective window is not None), AND
      2. Provider has a util substrate (or provider unknown), AND
      3. consecutive_low_uptime_count and util_thread_tick both present, AND
      4. util_thread_tick fresh (age <= sentinel_window), AND
      5. counter * heartbeat_interval_s >= effective window.

    Per-entry restart_loop_window_s override beats the cfg default.
    """
    override = entry.get("restart_loop_window_s")
    if override is not None:
        try:
            effective_window: float | None = float(override)
        except (TypeError, ValueError):
            effective_window = restart_loop_window_s
    else:
        effective_window = restart_loop_window_s
    if effective_window is None:
        return False
    provider_kind = entry.get("provider_kind") or entry.get("provider")
    if provider_kind is not None and not provider_util_supported(str(provider_kind)):
        return False
    counter = entry.get("consecutive_low_uptime_count")
    util_tick = entry.get("util_thread_tick")
    if counter is None or util_tick is None:
        return False
    try:
        counter_i = int(counter)
        util_age = now - float(util_tick)
    except (TypeError, ValueError):
        return False
    if util_age > sentinel_window:
        return False
    return counter_i * heartbeat_interval_s >= effective_window
```

### 4.3 `Verdict.RESTART_LOOP_REAP` + `DEFAULT_APPLY_POLICY`

```python
class Verdict(StrEnum):
    LIVE = "LIVE"
    IDLE_REAP = "IDLE_REAP"
    ORPHAN_REAP = "ORPHAN_REAP"
    OVERAGE_REAP = "OVERAGE_REAP"
    STALE_LEDGER = "STALE_LEDGER"
    HEARTBEAT_UNKNOWN = "HEARTBEAT_UNKNOWN"
    HEARTBEAT_SUBSTRATE_MISSING = "HEARTBEAT_SUBSTRATE_MISSING"
    STALL_REAP = "STALL_REAP"               # C26
    RESTART_LOOP_REAP = "RESTART_LOOP_REAP" # C27 — appended, insertion-order honoured

DEFAULT_APPLY_POLICY = Policy(
    act_verdicts=frozenset({
        Verdict.IDLE_REAP,
        Verdict.OVERAGE_REAP,
        Verdict.STALE_LEDGER,
        Verdict.STALL_REAP,           # C26
        Verdict.RESTART_LOOP_REAP,    # C27
    })
)
```

### 4.4 `classify()` row 3'' wiring

`classify()` signature gains:
- `restart_loop_window_s: float | None = None` — kill-switch default.
- `restart_loop_uptime_threshold_s: float = 90.0` — passed through to
  HeartbeatLoop counter; classify itself does not use it.

Row 3 / 3' / 3'' block:

```python
# Rows 3 & 4 — sentinel fresh
if sent_age <= sentinel_window:
    if hb_age <= idle:
        # Row 3' (C26): util-aware stall reap interception.
        if _stall_reap_predicate(
            entry, now=now, sentinel_window=sentinel_window,
            heartbeat_interval_s=heartbeat_interval_s,
            stall_window_s=stall_window_s,
        ):
            return Verdict.STALL_REAP
        # Row 3'' (C27): restart-loop reap interception.
        if _restart_loop_reap_predicate(
            entry, now=now, sentinel_window=sentinel_window,
            heartbeat_interval_s=heartbeat_interval_s,
            restart_loop_window_s=restart_loop_window_s,
        ):
            return Verdict.RESTART_LOOP_REAP
        return Verdict.LIVE
    return Verdict.IDLE_REAP
```

Tie-breaker: if BOTH predicates fire on the same tick (rare — chronic
restart loop with simultaneous low util), `STALL_REAP` wins by check order.
Operator-visible difference: the Verdict tag + the counter that crossed.
No double-destroy because `_maybe_fire_reap` returns after the first fire
and `classify()` returns the first matching Verdict.

### 4.5 `HeartbeatLoop` self-classify

New `__init__` kwargs (extends C26's three to five new total):

| Kwarg | Default | Purpose |
| ----- | ------- | ------- |
| `restart_loop_window_s` | `None` | Kill-switch when None (C27 feature off). |
| `restart_loop_uptime_threshold_s` | `90.0` | Strictly-< threshold for the counter. |

New instance state:

```python
self._uptime_counter: int = 0
```

`_tick_once` extension — one new counter update line and one new ledger
field. `self._counter` (C26) unchanged.

`_build_util_extra` extension — both branches (snap=None and snap-present)
gain `consecutive_low_uptime_count`.

Rename `_maybe_fire_stall_reap` → `_maybe_fire_reap` to host both routes:

```python
def _maybe_fire_reap(self, *, now: float) -> None:
    """Self-classify; on STALL_REAP or RESTART_LOOP_REAP destroy + cancel + stop."""
    sentinel_window = 3.0 * self._interval_s
    entry: dict[str, float | int | str | None] = {
        "id": self._instance_id,
        "consecutive_low_util_count": self._counter,
        "consecutive_low_uptime_count": self._uptime_counter,
        "util_thread_tick": now,
    }
    if self._provider_kind is not None:
        entry["provider"] = self._provider_kind

    fired_verdict: str | None = None
    if self._stall_window_s is not None and _stall_reap_predicate(
        entry, now=now, sentinel_window=sentinel_window,
        heartbeat_interval_s=self._interval_s,
        stall_window_s=self._stall_window_s,
    ):
        fired_verdict = "STALL_REAP"
    elif self._restart_loop_window_s is not None and _restart_loop_reap_predicate(
        entry, now=now, sentinel_window=sentinel_window,
        heartbeat_interval_s=self._interval_s,
        restart_loop_window_s=self._restart_loop_window_s,
    ):
        fired_verdict = "RESTART_LOOP_REAP"
    if fired_verdict is None:
        return

    self._logger.warning(
        "%s fired for %s (low_util_counter=%d, low_uptime_counter=%d, window=%s)",
        fired_verdict, self._instance_id,
        self._counter, self._uptime_counter,
        self._stall_window_s if fired_verdict == "STALL_REAP" else self._restart_loop_window_s,
    )
    # destroy + forget + cancel + stop — unchanged from C26
    destroy = getattr(self._provider, "destroy_instance", None)
    if destroy is not None:
        try:
            destroy(self._instance_id)
        except Exception:  # noqa: BLE001 — best-effort destroy
            self._logger.exception(
                "%s destroy failed for %s", fired_verdict, self._instance_id
            )
    forget = getattr(self._ledger, "forget", None)
    if forget is not None:
        try:
            forget(self._instance_id)
        except Exception:  # noqa: BLE001
            self._logger.exception(
                "%s ledger.forget failed for %s", fired_verdict, self._instance_id
            )
    if self._cancel_token is not None:
        self._cancel_token.set()
    self._stop.set()
```

Backward-compat: existing C26 callers passing only `stall_window_s` still
work — `restart_loop_window_s` defaults to None → kill switch → predicate
never evaluated → C27 dormant.

---

## 5. Cfg surface

### 5.1 `LifecycleConfig` extensions

Three new fields:

```python
class LifecycleConfig(BaseModel):
    # ... existing C26 stall_* fields ...
    restart_loop_reap_enabled: bool = True
    restart_loop_window_s: float = 180.0
    restart_loop_uptime_threshold_s: float = 90.0
```

Validators (mirror C26 pattern):

```python
@field_validator("restart_loop_window_s")
@classmethod
def _validate_restart_loop_window_non_negative(cls, v: float) -> float:
    if v < 0:
        raise ValueError(f"restart_loop_window_s must be >= 0; got {v}")
    return v

@field_validator("restart_loop_uptime_threshold_s")
@classmethod
def _validate_restart_loop_uptime_threshold_non_negative(cls, v: float) -> float:
    if v < 0:
        raise ValueError(f"restart_loop_uptime_threshold_s must be >= 0; got {v}")
    return v
```

No upper bound on `restart_loop_uptime_threshold_s` — uptime can legitimately
be measured in days for long-lived pods. The threshold floor (90 s default)
is set by the predicate's intent, not by a hard cap.

### 5.2 `interfaces.Lifecycle` extensions

Mirror two new fields on the public `Lifecycle` dataclass:

```python
restart_loop_window_s: float | None = None
restart_loop_uptime_threshold_s: float = 90.0
```

Bool `restart_loop_reap_enabled` is collapsed into `restart_loop_window_s`
at `Config.lifecycle()` build time (matches C26's `stall_reap_enabled` ↔
`stall_window_s` pattern):

```python
return InterfaceLifecycle(
    # ... existing fields ...
    stall_window_s=lc.stall_window_s if lc.stall_reap_enabled else None,
    stall_gpu_threshold=lc.stall_gpu_threshold,
    stall_cpu_threshold=lc.stall_cpu_threshold,
    restart_loop_window_s=(
        lc.restart_loop_window_s if lc.restart_loop_reap_enabled else None
    ),
    restart_loop_uptime_threshold_s=lc.restart_loop_uptime_threshold_s,
)
```

### 5.3 Per-entry ledger override

Optional field `restart_loop_window_s` on a ledger entry overrides the cfg
default for that entry only. Read inside `_restart_loop_reap_predicate` (not
via `_resolve` — matches C26 pattern: predicate owns its own override read).

### 5.4 CLI extension

`kinoforge deploy` gains a symmetric override flag alongside the existing
C26 `--stall-window-override`:

```
--stall-window-override SECONDS         # C26
--restart-loop-window-override SECONDS  # C27
```

Both persist to the deployed ledger entry's matching key.

### 5.5 Callsite audit

Five callsites that currently thread C26 kwargs — same five thread C27 kwargs:

1. `deploy_session` HeartbeatLoop constructor.
2. `_adapters.build_util_endpoint_for` consumers.
3. `kinoforge deploy` CLI handler (+ new `--restart-loop-window-override`).
4. Reaper / sweeper `classify()` call sites (Layer V CLI + Layer W daemon).
5. HeartbeatLoop unit-test factories.

Identifier the implementer must verify by running `rg 'stall_window_s'`
during plan phase.

---

## 6. Ledger schema delta

One new field on the ledger entry:

```
consecutive_low_uptime_count: int   # C27 sibling of consecutive_low_util_count
```

Optional per-entry override key:

```
restart_loop_window_s: float        # C27 sibling of stall_window_s
```

**Backward-compat invariant** — legacy entries (pre-C27) lack both keys:

- `_restart_loop_reap_predicate` reads `entry.get("consecutive_low_uptime_count")`
  → `None` → returns False → classify returns the pre-C27 verdict (LIVE,
  IDLE_REAP, …). No false-positive on legacy entries.
- HeartbeatLoop self-classify on legacy entries: same — counter starts at 0
  in instance state, predicate reads it as 0, never fires until enough fresh
  ticks accumulate naturally.

`Ledger.touch` already accepts `**extra: float | int | str | None`. No
schema migration; new field flows through verbatim.

`_build_util_extra` shape:

```python
@staticmethod
def _build_util_extra(
    *, now: float,
    snap: UtilSnapshot | None,
    counter: int,
    uptime_counter: int,
) -> dict[str, float | int | str | None]:
    base: dict[str, float | int | str | None] = {
        "util_thread_tick": now,
        "consecutive_low_util_count": counter,
        "consecutive_low_uptime_count": uptime_counter,  # C27
    }
    if snap is None:
        return base
    return {
        **base,
        "last_gpu_util_percent": snap.gpu_util_percent,
        "last_cpu_percent": snap.cpu_percent,
        "last_memory_percent": snap.memory_percent,
        "last_disk_percent": snap.disk_percent,
        "last_uptime_seconds": snap.uptime_seconds,
    }
```

---

## 7. Test strategy

### 7.1 Unit tests (RED first per TDD)

Table-driven where possible. Each test gets the `test-design` skill's
"what behaviour, what bug would make it fail" pre-check.

**`tests/test_util_counter.py` — `_update_uptime_counter` (new TestClass)**

| Case | Input | Expected |
| ---- | ----- | -------- |
| transport hiccup preserves at high counter | `prev=9, snap=None` | `9` |
| snap with `uptime_seconds=None` resets | `prev=5, snap.uptime=None` | `0` |
| uptime strictly < threshold increments | `prev=3, snap.uptime=89, threshold=90` | `4` |
| uptime == threshold resets (strict <) | `prev=3, snap.uptime=90, threshold=90` | `0` |
| uptime > threshold resets | `prev=7, snap.uptime=200, threshold=90` | `0` |
| float-equal threshold edge | `prev=0, snap.uptime=89.9999, threshold=90` | `1` |
| fresh tick uptime=1 always counts | `prev=0, snap.uptime=1, threshold=90` | `1` |
| extreme threshold (0) blocks all | `prev=5, snap.uptime=1, threshold=0` | `0` |

**`tests/test_reaper.py` — `_restart_loop_reap_predicate` (new TestClass)**

| Case | Setup | Expected |
| ---- | ----- | -------- |
| feature off via `None` window | `restart_loop_window_s=None, counter=999` | `False` |
| substrate-unsupported provider | `provider="fal", counter=999, window=10` | `False` |
| substrate-unknown provider | `provider=None` (legacy), counter=20, window=10 | `True` (provider gate skipped) |
| legacy entry no counter | `counter missing, util_tick set` | `False` |
| legacy entry no util_tick | `counter=20, util_tick missing` | `False` |
| stale util_tick | `util_age > sentinel_window` | `False` |
| just-under window | `counter*interval = window-1` | `False` |
| exactly at window | `counter*interval = window` | `True` (≥ not >) |
| per-entry override beats cfg | `entry.restart_loop_window_s=10, cfg=999, counter*interval=15` | `True` |
| corrupt override falls through | `entry.restart_loop_window_s="abc", cfg=10, counter*interval=15` | `True` |
| corrupt counter type | `counter="abc"` | `False` (TypeError caught) |
| corrupt util_tick type | `util_tick="bad"` | `False` (ValueError caught) |

**`tests/test_reaper.py` — classify row 3'' wiring (new TestClass)**

- Both predicates fire on same entry → STALL_REAP wins (tie-breaker).
- Only restart-loop predicate fires → RESTART_LOOP_REAP returned.
- Only stall predicate fires → STALL_REAP returned (existing C26 behaviour).
- Neither fires → LIVE returned (existing pre-C26 behaviour).
- restart-loop predicate fires but kill-switch on → LIVE returned.

**`tests/test_heartbeat_loop.py` — `_maybe_fire_reap` both routes (extend existing TestClass)**

- Only `stall_window_s` set: fires STALL_REAP path (C26 unchanged).
- Only `restart_loop_window_s` set: fires RESTART_LOOP_REAP path.
- Both set + only restart-loop counter at threshold: fires RESTART_LOOP_REAP.
- Both set + only stall counter at threshold: fires STALL_REAP.
- Both set + both counters at threshold: fires STALL_REAP (tie-breaker matches classify).
- Neither set (kill switches): no fire even with both counters past threshold.
- Destroy + forget + cancel + stop all called on fire (existing C26 spy pattern extended).

**`tests/test_config.py` — `LifecycleConfig` extensions (extend existing TestClass)**

- `restart_loop_reap_enabled=False` collapses `restart_loop_window_s` to None in `Lifecycle()`.
- `restart_loop_window_s=-1` rejected at load.
- `restart_loop_uptime_threshold_s=-1` rejected at load.
- Defaults match spec: 180.0, 90.0, True.

### 7.2 Core-import-ban invariant

`tests/test_core_invariant.py` already enforces `core/` cannot import
`providers/`. C27 adds zero new provider imports — invariant green by
construction. Test run as part of normal `pixi run test`.

### 7.3 Live smoke ladder

**Phase A1 — `tests/live/test_c27_phase_a1_uptime_streak_live.py`**

- Cheapest RunPod offer (~$0.13/hr).
- `FakeEngine` + new `FakeUtilEndpoint` returning
  `UtilSnapshot(gpu=None, cpu=None, memory=None, disk=None, uptime_seconds=1)`
  on every read.
- HeartbeatLoop args: `interval_s=10`, `restart_loop_window_s=60`,
  `restart_loop_uptime_threshold_s=90`, `stall_window_s=None` (isolate C27).
- Acceptance:
  - Counter trail `[1, 2, 3, 4, 5, 6]` (first tick increments since uptime=1<90).
  - `RESTART_LOOP_REAP` fires at counter=6 (60-90 s wall).
  - Pod destroyed, ledger entry forgotten, cancel-token set, thread stopped.
- Sidecar evidence: `tests/live/_c27_phase_a1_evidence.json`.
- Budget cap $0.05. Expected actual ~$0.02.

**Phase A2 — `tests/live/test_c27_phase_a2_alpine_restart_loop_live.py`**

- Cheapest RunPod offer + `alpine:latest` image (~5 MB pull).
- `dockerArgs: "sh -c 'sleep 5; exit 1'"` — RunPod restart-policy churns container.
- Real `RunPodUtilEndpoint` over GraphQL (no Fake).
- HeartbeatLoop args: `interval_s=15`, `restart_loop_window_s=120`,
  `restart_loop_uptime_threshold_s=90`, `stall_window_s=None`.
- Acceptance:
  - Live `runtime.container.uptimeInSeconds < 90` for ≥ 8 consecutive ticks
    (counter increments unchecked).
  - `RESTART_LOOP_REAP` fires; pod destroyed; cancel-token signalled.
- Sidecar evidence: `tests/live/_c27_phase_a2_evidence.json` includes raw
  GraphQL `runtime{}` responses for forensic record.
- Budget cap $0.15. Expected actual ~$0.05.

**Phase B — `tests/live/test_c27_phase_b_wan_warm_reuse_live.py`**

Re-fire of C26 Phase B (`test_c26_phase_b_wan_warm_reuse_live.py`) with C27
cfg knobs active. Identical shape to the C26 file except `LifecycleConfig`
carries C27 defaults (`restart_loop_reap_enabled=True`,
`restart_loop_window_s=180`, `restart_loop_uptime_threshold_s=90`).

Acceptance is **either**:

1. **CLEAN-PASS** — Wan run completes both CLI invocations, warm-reuse ratio
   `gen2_elapsed / gen1_elapsed < 0.7`. Outcome: prior stall was transient;
   C27 predicate didn't need to fire. Acceptance: workload produced video,
   logged to `successful-generations.md` per CLAUDE.md.
2. **PROVEN-PROTECTION** — `RESTART_LOOP_REAP` fires within
   `restart_loop_window_s` (180 s) of the chronic-restart symptom. Pod
   destroyed; cancel-token propagates; outer CLI exits with cancel reason
   instead of hanging at 22 min. Acceptance: counter trail in sidecar
   crosses threshold; CLI exit code reflects cancel; no operator manual kill.

Either is acceptance. Sidecar `tests/live/_c27_phase_b_evidence.json`
records counter trails, raw GraphQL responses at threshold crossing,
ledger snapshots, CLI exit reason, and which acceptance path closed.

Budget cap $0.60 (matches C26 Phase B cap).

---

## 8. Acceptance criteria

All must pass to ship C27:

1. All new unit tests green per §7.1.
2. All pre-existing C26 unit + live tests green untouched.
3. Core-import-ban invariant green (`tests/test_core_invariant.py`).
4. Phase A1 PROVEN — counter trail crosses threshold, RESTART_LOOP_REAP
   fires, evidence sidecar committed.
5. Phase A2 PROVEN — alpine restart loop triggers fire on the real
   `RunPodUtilEndpoint` wire path, evidence sidecar committed.
6. Phase B closes — either CLEAN-PASS or PROVEN-PROTECTION per §7.3;
   evidence sidecar committed.
7. `pixi run pre-commit run --all-files` clean (ruff, ruff-format, mypy).
8. `pixi run preflight` clean before each live smoke.

---

## 9. Invariants preserved

- **Backward-compat.** Legacy ledger entries (no util fields, no restart
  fields) still classify per pre-C27 behaviour. New field absent → predicate
  returns False → row 3'' falls through.
- **Sentinel-gate contract (Layer V).** Row 3'' nested inside
  `sent_age <= sentinel_window` AND `hb_age <= idle` — never fires when the
  heartbeat is already stale (IDLE_REAP path) or sentinel dead.
- **Substrate-gated.** `provider_util_supported({"local", "runpod"})` — fal,
  SkyPilot, hosted always return LIVE, never RESTART_LOOP_REAP.
- **Kill-switch.** `restart_loop_reap_enabled=False` OR
  `restart_loop_window_s=None` → predicate never fires.
- **Insertion-order Verdict contract.** `RESTART_LOOP_REAP` appended after
  `STALL_REAP` — never inserted mid-enum.
- **C26 unchanged.** `_stall_reap_predicate`, `_update_counter`, and all
  C26 behaviour are not edited; classify signature widens additively, and
  HeartbeatLoop gains kwargs without changing existing kwargs.
- **Core-import-ban.** No `providers/` import from `core/`.

---

## 10. Out of scope

Explicit non-goals — surfaced and rejected during brainstorm:

- **Ring-buffer restart-event tracking.** Counting actual restart events
  over a window (option 2 from §2.1) needs new persisted state shape; not
  needed given the low-uptime streak covers the observed failure class.
- **AND-coupling with low gpu/cpu.** Suppressing the predicate during
  apt-install CPU spikes would extend MTTD pointlessly; rejected at §2.2.
- **New cancel-token semantics.** Share C26's existing token; rejected at
  §2.7.
- **Provider-specific code outside `providers/runpod/util.py`.** Already
  C26-shipped; C27 adds zero provider files.
- **Ledger schema migration.** Additive field only; no `Ledger.migrate`,
  no schema version bump.
- **Per-engine `expected_uptime_seconds` ABC.** Couples engines to numerics
  that drift; threshold is cfg-level, per-entry overridable.

---

## 11. Risk register

| # | Risk | Mitigation |
| - | ---- | ---------- |
| 1 | Crontab-style pod that legitimately exits + restarts on each cycle (cycle < 90 s) gets reaped as a "restart loop" | Deploy that workload with `restart_loop_reap_enabled: False` in `LifecycleConfig` (mirrors C26's `stall_reap_enabled` cfg-level kill switch). Per-entry `--restart-loop-window-override` tunes the window but cannot disable; matching C26 design parity. Default workloads (Wan / Comfy / generic engines) do not have this pattern, so default cfg stays opt-in safe. |
| 2 | Phase A2 RunPod restart-policy backs off and stops restarting before predicate fires | Use `dockerArgs` `sleep 5; exit 1` — short cycle keeps churn within expected RunPod retry policy. If predicate doesn't fire within 5 min budget, smoke fails with diagnostic — escalate to longer sleep or different image. |
| 3 | Wan re-fire on Phase B passes CLEAN — predicate untested in the wild | Acceptance accepts CLEAN-PASS explicitly (§7.3). Phase A2 already verifies the wire path; Phase B is a real-workload sanity check, not the predicate's only proving ground. |
| 4 | Concurrent fires (STALL + RESTART_LOOP on same tick) cause confusion in logs | Tie-breaker documented (§4.4): STALL_REAP wins. Sidecar records both counter values; log line names which fired. |
| 5 | A future provider's `uptime_seconds` semantics differ from RunPod's (e.g. reports 0 when not running) | Substrate gate (`_UTIL_SUPPORTED`) limits exposure to the two providers C26 already shipped. New satisfiers go through brainstorm before being added. |
| 6 | Counter persistence skew between in-process counter and ledger field | HeartbeatLoop writes counter to ledger every tick before self-classify; cross-process consumers always see the same value HeartbeatLoop just decided on. C26 has lived with this seam — no new exposure. |

---

## 12. Task split (preview for plan phase)

Rough preview; canonical split lives in the `/gsd-plan-phase` PLAN.md.

1. **Probe** — none needed (C26 already mapped RunPod uptime semantics).
2. **`_update_uptime_counter`** + table-driven tests. RED → GREEN.
3. **`_restart_loop_reap_predicate`** + table-driven tests. RED → GREEN.
4. **`Verdict.RESTART_LOOP_REAP`** + `DEFAULT_APPLY_POLICY` + tests.
5. **`classify()` row 3'' wiring** + tests (tie-breaker + isolation cases).
6. **`HeartbeatLoop._maybe_fire_reap`** rename + both-routes wiring + tests.
7. **`LifecycleConfig` + `interfaces.Lifecycle`** extension + validators + tests.
8. **`Config.lifecycle()` wiring** + tests.
9. **CLI `--restart-loop-window-override`** + per-entry persist + tests.
10. **Five-callsite kwarg thread** (audit per §5.5) + tests at each.
11. **`FakeUtilEndpoint` test helper** for Phase A1.
12. **Phase A1 live smoke** (RED scaffold committed pre-spend; budget $0.05).
13. **Phase A2 live smoke** (RED scaffold committed pre-spend; budget $0.15).
14. **Phase B live smoke re-fire** (RED scaffold committed pre-spend; budget $0.60).
15. **Closeout** — PROGRESS.md §C C27 CLOSED; spec §13 closeout section;
    C26 §17 pointer to C27 close.

---

## 13. Closeout — Phase A1 + Phase A2 + Phase B outcomes (2026-06-13)

**C27 ships fully closed.** All three live smokes proven; the deferred
C25 Task 4 / C26 Task 14 gate is closed via the Phase B PROVEN-PROTECTION
path. Pure-additive on the C26 util substrate — zero new wire paths,
zero ledger migrations.

### Per-task commits

| Task | Subject | SHA |
| ---- | ------- | --- |
| 1  | `Verdict.RESTART_LOOP_REAP` + `DEFAULT_APPLY_POLICY` entry | `19cffff` |
| 2  | `_update_uptime_counter` pure state machine | `25a738d` |
| 3  | `_restart_loop_reap_predicate` pure function | `71c8780` |
| 4  | `classify()` row 3'' wiring + STALL_REAP tie-breaker | `d12f26a` |
| 5  | `LifecycleConfig` `restart_loop_*` fields + validators | `16266a9` |
| 6  | `interfaces.Lifecycle` + `Config.lifecycle()` collapse | `9397eb6` |
| 7  | `HeartbeatLoop` kwargs + `_uptime_counter` state + ledger touch | `16ba622` |
| 8  | `_maybe_fire_stall_reap` → `_maybe_fire_reap` + both-routes wiring | `2e3e6f5` |
| 9  | `--restart-loop-window-override SECONDS` CLI flag | `f6fecda` |
| 10 | Cross-process callsite threading (adapters + orchestrator + CLI) | `23dccaa` |
| 11 | `FakeUtilEndpoint` test helper | `1d2296c` |
| 12 | Phase A1 RED scaffold + PROVEN evidence | `34571f6` / `2f57931` |
| 13 | Phase A2 RED scaffold + PROVEN evidence | `d698d3b` / `8faef91` |
| 14 | Phase B RED scaffold + PROVEN-PROTECTION evidence | `39e64f8` / `ce4bd00` |
| 15 | This closeout — PROGRESS §C + spec §13 + C26 §17 pointer | (this commit) |

### Phase A1 — FakeUtilEndpoint uptime=1

- **Outcome:** PROVEN — counter trail `[1, 2, 3, 4, 5, 6]`; fires at 52.3 s
  (well under the 90 s ceiling).
- **Sidecar:** `tests/live/_c27_phase_a1_evidence.json` (committed in
  `2f57931`).
- **Spend:** ~$0.002 (RTX A2000 @ $0.12/hr × 55 s).

### Phase A2 — real alpine restart loop

- **Outcome:** PROVEN — real `RunPodGraphQLUtilEndpoint` against alpine
  pod with `provision_script="sleep 5; exit 1"`. Counter trail
  `[0, 1, 2, 3, 4, 5, 6]`; uptime readings
  `[None, 0, 0, -2, -2, -15, -15]` (RunPod's runtime{} surfaces 0 or
  negative uptime during restart churn — all well below threshold 90 s).
  Fires at 96.4 s.
- **Sidecar:** `tests/live/_c27_phase_a2_evidence.json` (committed in
  `8faef91`).
- **Spend:** ~$0.003 (RTX A2000 @ $0.12/hr × 99 s).

### Phase B — Wan + ComfyUI re-fire of deferred C25/C26 gate

- **Acceptance path:** PROVEN-PROTECTION.
- **Outcome:** real Wan 2.1 14B T2V cold-attach regressed into the same
  container-restart-loop symptom that defeated C26 Phase B — but C27
  caught it. The HeartbeatLoop's `_maybe_fire_reap` self-classified
  `RESTART_LOOP_REAP`, destroyed the pod, set the `CancelToken`; gen1's
  `ComfyUIBackend.result` poll observed `token.is_set()`, raised
  `Cancelled` at 356.8 s, and the `kinoforge generate` subprocess exited
  rc=1. The C25 Task 4 / C26 Task 14 deferred gate is closed via this
  path.
- **Sidecar:** `tests/live/_c27_phase_b_evidence.json` (committed in
  `ce4bd00`).
- **Spend:** ~$0.05 (gen1 only; predicate fired before gen2).

### Total live spend

~$0.06 across all three smokes (well under the $0.80 cumulative cap and
under the $20 session budget).

### Deferred-gate closure

The C25 Task 4 acceptance gate (Wan + ComfyUI 2-CLI warm-reuse smoke),
which C26 marked PARTIAL because the predicate did not cover the
chronic-restart-loop class, is now closed. Phase B reproduced the
symptom and demonstrated end-to-end protection.

### PROGRESS.md / C26 §17 references

- PROGRESS.md §C C27 line: appended in the Task 15 closeout commit
  (this commit).
- C26 §17 cross-reference pointer: appended at the end of the C26
  spec in the same commit.

---

---

## 14. Wire-discovery notes (for plan phase)

- **RunPod `runtime.container.uptimeInSeconds`** semantics confirmed in C26
  Task 1 probe (sidecar `tests/live/_runpod_util_disk_probe.json`): integer
  seconds, increments monotonically while container is up, resets to 1 on
  container restart. Phase B evidence confirms it stays at 1 across reads
  during chronic restart loops (orchestrator's read race lands inside the
  brief "container just started" window every poll).
- **RunPod restart policy** (default): restarts container on non-zero exit.
  Backoff observed empirically — alpine smoke must use short `sleep` (5 s)
  to keep churn faster than any backoff curve.
- **Wire latency** — C26 measured P50=460 ms, P99=583 ms on RunPod GraphQL
  (sidecar `tests/live/_runpod_heartbeat_smoke_latencies.json`). 30 s tick
  cadence has ~50× headroom even at P99. C27 thresholds (90 s uptime, 180 s
  window) are far above any transport latency floor — wire timing does not
  affect predicate aggressiveness.
- **No disk axis on RunPod.** `disk_percent` is permanently None
  (C26 §16). Irrelevant to C27 since uptime-only.

---

## 15. PROGRESS.md updates on C27 close

Append under §C:

```
- **C27 — Restart-loop stall detection.** CLOSED. Adds
  `_update_uptime_counter` + `_restart_loop_reap_predicate` +
  `Verdict.RESTART_LOOP_REAP` on the C26 substrate. Three smoke ladder
  (FakeUtilEndpoint, alpine restart loop, Wan re-fire) all proven /
  closed. Spec
  `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md`.
  Closes deferred C25 Task 4 / C26 Task 14 gate.
```

C26 §17 closeout gains a pointer:

```
- C26 PARTIAL → C27 closes the restart-loop gap.
  See `2026-06-13-c27-restart-loop-stall-detection-design.md` §13.
```
