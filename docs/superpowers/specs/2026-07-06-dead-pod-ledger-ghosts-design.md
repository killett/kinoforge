# Design — Kill phantom dead-pod `est_spend` ghosts

**Date:** 2026-07-06
**Status:** Validated (brainstorm approved)
**Spec (what):** this document
**Plan (how):** `docs/superpowers/plans/2026-07-06-dead-pod-ledger-ghosts.md` (to be written)

## Problem

`_print_instance_overview` (`src/kinoforge/cli/_main.py:908`) runs at the top of
**every** command (upscale, interpolate, generate, …) and prints one line per
ledger entry:

```
  <id>  age=<h>h  est_spend=$<age_h × cost_rate>
```

It reads **raw** ledger rows with **no liveness check**. When a pod dies
out-of-process and its ledger row is never forgotten, the row survives and its
`est_spend = age_h × cost_rate` inflates forever — a purely wall-clock figure
presented as a confident dollar amount.

Observed 2026-07-06: two pods `fhwesee6ttxwxk` / `kunltd9f4xahgz`, age ~189 h,
showed `est_spend≈$225` each at the top of an `upscale --config` run. Both
confirmed **GONE** provider-side (`get_instance` → `KeyError`). The `$225` was
never billed — `189 h × ~$1.19/hr` fiction. The user's balance was never
touched.

### Why the existing fix does not cover this

The reconcile fix `a6e5ec2` (`_reconcile_dead_ledger_entries`,
`src/kinoforge/cli/_commands.py:1133`) forgets confirmed-gone RunPod rows — but
it is called **only from `_cmd_list`** (`_commands.py:1188`). Every other
command's startup overview never reconciles, so `list` shows a clean ledger while
`upscale` still prints the ghosts. Different code paths; only one reconciles.

### How the orphan is born (root cause)

`destroy_confirmed` (`src/kinoforge/core/lifecycle.py:743`) treats "already gone"
as **success** (it polls `list_instances()` and returns when the id is absent),
then the `--no-reuse` teardown `finally` (`orchestrator.py:1346-1351`) forgets
the row. That path is sound. It keeps the row only on `TeardownError` = pod
**still visible** after 3 retries, where keeping it is correct (may still be
billing).

The real orphan births are **out-of-process**:

1. **Host-reclaim then operator kill.** RunPod reclaims a pod mid-run (a known
   failure mode — see `CLAUDE.md` gotchas). The driver hangs on the dead pod; the
   operator SIGKILLs it before the teardown `finally` runs. The forget never
   happens. (This is the June-28 case.)
2. **Warm-reuse pod dies host-side later** and the user never runs `list`.

`_tick_once` in the heartbeat loop (`src/kinoforge/core/heartbeat_loop.py:220`)
wraps every provider probe in a broad `except Exception: log + swallow`, so a
host-reclaimed pod raises, is swallowed, and the row is **never** forgotten
mid-run.

## Principle

**Ledger = cache. Provider = source of truth for liveness.** A cache with no
reconciliation contract is the technical debt. The fix is defense-in-depth:
prevent births at the source where possible, and converge the cache against the
provider on read.

## Design

### Part 1 — Read-side self-heal (primary)

`_print_instance_overview` reconciles **before** printing.

- For each entry, compute `age = now − created_at`. Probe the provider **only**
  when `age > max_lifetime_s` — the per-entry value already persisted in the
  ledger (`lifecycle.py:583-584`, stored as `idle_timeout_s`). A row still
  present past its own reap deadline means the sweeper never ran = a genuine
  orphan candidate. When the entry lacks the field (older rows), fall back to a
  module-level default (`_OVERVIEW_STALE_AFTER_S`).
- Suspect rows are passed to the **existing** `_reconcile_dead_ledger_entries`.
  Confirmed-gone rows are forgotten and dropped from the printed output.
- Young / live rows are **never** probed → the warm-reuse hot path stays
  zero-network. The common empty-ledger case is unchanged.
- Best-effort: any provider/network failure is caught and the row is **kept**
  (see Part 3 for how it is then displayed). The overview never raises — it
  preserves its current degrade-gracefully contract.

**Shared implementation.** `_reconcile_dead_ledger_entries` currently lives in
`cli/_commands.py`; the overview lives in `cli/_main.py`. Move the function (and
its `_RECONCILABLE_PROVIDERS` / `_ForgetLedger` helpers) to a shared module
(e.g. `cli/_reconcile.py` or `core/lifecycle.py`) and import it from both call
sites. **One** implementation, two callers — no duplicated logic.

### Part 2 — Write-side source fix

Add a `POD_GONE` verdict to the heartbeat loop.

- In `_tick_once`, when the provider probe confirms the pod no longer exists
  (`KeyError` from a status probe, or the id absent from `list_instances`),
  classify `POD_GONE` and route it through the existing `_maybe_fire_reap`
  machinery: forget the ledger entry + signal the cancel token + stop the thread.
- This fires at the **next tick** after a host-reclaim — before the operator has
  to kill a hung driver — so the June-28 birth is prevented at the source.
- Distinguish "pod confirmed gone" (forget) from a transient transport/auth error
  (keep, current swallow behavior). Only a definitive not-found triggers
  `POD_GONE`.

Residual not catchable in-process: a SIGKILL that lands *before any tick observes
the death*. That residual is exactly what Part 1 mops up on the next command.

### Part 3 — Display honesty

The number must never again read as a confident real charge.

- Relabel `est_spend` as an explicit upper-bound estimate. Example line shape:
  `est≤$225.6105 (age×rate; $0 if pod already dead)`, or a one-line header caveat
  above the entries. Exact wording chosen during planning; requirement: it is
  unambiguously an estimate, not a bill.
- A suspect row (`age > max_lifetime_s`) that **survives an offline overview**
  (Part 1 could not reach the provider to confirm) is marked
  `⚠ unverified — run 'kinoforge list'`, so a ghost is visibly flagged even with
  no network.

## Testing

- **Overview age-gate:** entry just under `max_lifetime_s` → provider **not**
  probed (assert the injected provider factory is never called); entry just over
  → probed.
- **Forgotten drop:** a suspect row the provider confirms gone is forgotten and
  absent from printed output.
- **Offline degrade:** provider probe raises → row kept, printed with
  `⚠ unverified`, overview returns normally (no raise, no crash).
- **Shared reconcile move:** existing `tests/cli/test_reconcile_ledger.py` stays
  green after the function moves (import-path update only).
- **Heartbeat `POD_GONE`:** provider probe raises `KeyError` (pod gone) mid-tick →
  forget + cancel + stop invoked; a live pod → `POD_GONE` never fires; a transient
  transport error → row kept, no forget.
- **Label formatting:** unit test asserting the estimate/upper-bound wording.

Follow the `test-design` skill: each test states the behavior under test and a
concrete bug it would catch. No happy-path-only coverage; assert the negative
(provider-not-called) paths explicitly.

## Non-goals (YAGNI)

- **No real per-pod billing integration.** The estimate stays an estimate,
  honestly labeled. Wiring a RunPod cost API is out of scope.
- **No new global config knob.** Reuse the per-entry `max_lifetime_s` already in
  the ledger; a module-level default covers legacy rows.
- **No change to the `--no-reuse` teardown `finally`.** It is already correct
  (`destroy_confirmed` treats already-gone as success and forgets; keeps the row
  only when the pod is genuinely still visible).
- **No reconcile for non-RunPod providers.** `_RECONCILABLE_PROVIDERS` stays
  `{"runpod"}`; `local`'s instance table is in-process and would false-positive.

## Files touched (anticipated)

- `src/kinoforge/cli/_main.py` — `_print_instance_overview`: age-gate + reconcile
  call + honest label + `⚠ unverified` marker.
- `src/kinoforge/cli/_commands.py` — move `_reconcile_dead_ledger_entries` out;
  import from the shared module; `_cmd_list` uses the shared one.
- New shared module (e.g. `src/kinoforge/cli/_reconcile.py`) — the reconcile
  primitive + helpers.
- `src/kinoforge/core/heartbeat_loop.py` — `POD_GONE` verdict in `_tick_once` /
  `_maybe_fire_reap`.
- `src/kinoforge/core/reaper.py` — `POD_GONE` added to the `Verdict` enum (if
  verdicts live there).
- Tests alongside each.
