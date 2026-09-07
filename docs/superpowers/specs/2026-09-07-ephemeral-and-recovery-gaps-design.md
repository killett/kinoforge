# Closing the ephemeral and recovery gaps — design (2026-09-07)

## Purpose

The 2026-09-06 money-leak branch (merged at `d5dc0433`) made an `--ephemeral` run leave a durable
record *before* it books a GPU, so a crashed run leaves a pod that can still be named and
destroyed. That fix reached `generate`, `upscale` and `interpolate`. It did not reach `batch` or
`grid`, the one-shot recovery command cannot see any of them, and two recovery paths cannot act on
the records that now exist.

This closes those five, filed as U11, U17, U18, U21 and U23 in the
`## URGENT ACTION ITEMS — Modal command matrix (opened 2026-09-06)` section of `PROGRESS.md`.

| Item | Symptom | Cost shape |
|------|---------|------------|
| U23 | `--ephemeral batch` leaves no durable record at all | unbounded — the widest window in the CLI |
| U11 | `grid --ephemeral` accepted and silently dropped | privacy, not dollars: run id, timestamp and workload shape published |
| U18 | one-shot `reap` prints "nothing to do" over a billing pod | the documented backstop is unreachable |
| U17 | `destroy --id` cannot reap a pod killed mid-deploy | narrow in dollars, but it is the recovery U7 existed to end |
| U21 | `provision` never tears down after a post-create failure | pod visible but cleanup manual; readiness loop unbounded |

## Themes, not five patches

**A — the durable record reaches the commands it missed (U23, U11).** `batch` and `grid` are the
two commands the merged branch did not touch. Both launch several pods per invocation.

**B — recovery paths can act on the records that now exist (U18, U17).** A record nothing can use
is not a recovery. Both defects are the same shape as U16: the identifier written down is not the
identifier the consumer accepts.

**C — `provision` cleans up after itself (U21).** U7 closed "nothing can see the pod". This is
"nothing tears the pod down", which U7 never claimed.

## Scope

### A1 — `batch` reserves a launch row before it books

`_cmd_batch` cold-creates through `batch_generate` → `deploy_session` and calls neither
`_ephemeral_launch_row_reserve` nor `_ephemeral_index_add`; nothing in `core/batch.py` or the
orchestrator writes an index row either. Under STRICT_POLICY the ledger write is suppressed and
lives only in `session.in_memory_ledger`, so for the whole batch the pod exists in no state file.

A batch is the longest-running command there is — a manifest of N entries on one pod — so it has
the widest crash window in the CLI.

Reuse the merged branch's helpers (`_ephemeral_launch_row_reserve`, `_ephemeral_strict_session`,
`_settle_unused_launch_row`, `EphemeralSession.resource_name`). Do not build a parallel path.

### A2 — `grid` passes `--ephemeral` through to each cell

The parser advertises the flag as "pass-through to each underlying generate". `_cmd_grid` does
`del ctx`, never reads `args.ephemeral`, and the string `ephemeral` appears nowhere under
`src/kinoforge/core/grid/`. Cells are published under names carrying the run id, the local
timestamp and the workload shape — exactly what the flag exists to suppress.

The flag must reach each cell's subprocess argv (`core/grid/executor.py`, `_build_generate_cmd`).
**Establish and report** whether opaque naming then follows automatically from STRICT_POLICY in the
child process, or whether the grid path needs more.

### B1 — one-shot `reap` gates on the union, not the ledger

`_cmd_reap` short-circuits on `if not ledger.entries():` and returns before calling `sweep()`. An
ephemeral run writes no ledger row by design, so its orphan lives only in the `EphemeralIndex`,
which is unioned in *inside* `sweep()` — on the far side of the guard. Its own comment claims the
one-shot reap "gets the same age+idle backstop the daemon does". It does not, for exactly the case
that needs it, and it prints "nothing to do" over a running pod.

Gate on the union of `ledger.entries()` and `EphemeralIndex(store).rows()`, and word the message
for what was actually searched.

### B2 — `destroy` resolves the identifier the provider accepts

Modal registers an app's NAME only when the deploy completes. An app whose deploying client died
is addressable solely by its `app_id`. `ModalProvider.destroy_instance` builds `kinoforge-<run_id>`
and hands it to `default_stop`, which shells `modal app stop <name> --yes` under `check=True`.

Two failures compound: the wrong identifier, and `check=True` turning a diagnosable provider error
into an unhandled traceback.

Look the app up in `modal app list --json` (matching on `description`), stop it by `app_id`, fall
back to the name, and replace the bare `check=True` with a handled error that prints the app id it
found. Verified recovery from the live proof: `modal app stop ap-UieraQfT1GhxX3v4etyrEA --yes` → rc
0, app `stopped`.

### C1 — `provision` guards its tail and bounds its readiness loop

Everything after the provisional-row collapse runs unguarded: a readiness loop calling
`provider.get_instance` and then `provision(...)`. Any raise propagates out with the pod alive and
billing. The loop also has **no timeout and no iteration cap** — a pod that never reaches `ready`
spins forever at two seconds a turn, which is the same leak with no exception at all.

Wrap the tail, destroy on failure, and bound the loop with a deadline that surfaces a clear error.
Follow `deploy`'s existing destroy-on-error handling rather than inventing a second shape.

## Non-goals

- U16 (the RunPod half of the launch-row handle) stays filed: it needs probe-and-destroy by name,
  a provider change with its own live proof.
- U1, U2, U3, U5, U6, U13, U19, U22 stay filed and are out of scope.
- No RunPod or SkyPilot live re-proof. These paths are shared and expected to benefit, but proving
  that is its own budget line.

## Verification

Offline throughout — TDD, failing test first — then **one** live task at the end on the cheap A10
(`examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml`, ~90 s boot, $1.10/hr). Budget ~$0.50,
hard ceiling $1.50.

| Proof | Shape |
|-------|-------|
| A1 | launch `--ephemeral batch`, read the index **while it runs**, kill the controller, recover the pod from a fresh process |
| A2 | run `grid --ephemeral`, confirm the provider's app list shows opaque `eph-` names and no run id or timestamp |
| B1 | with an ephemeral orphan live and an empty ledger, one-shot `reap` must find it rather than printing "nothing to do" |

B2 and C1 are proven offline; a live re-proof of the mid-deploy kill is a follow-up with its own
budget, because reproducing it reliably means racing a 1-second window.

**A deliberate mid-run kill is the point of A1, not an accident to avoid.** These fixes exist so a
crashed run leaves a nameable pod, and only a real kill proves it.

## Risks

- **A filed mechanism may be wrong.** Five defects in this project have had wrong stated mechanisms
  and one diagnosis was retracted. Every claim here must be verified against the code before it is
  fixed; if a claim does not survive inspection, fix what is actually there and say so.
- **A2 may exceed a flag pass-through.** If threading the flag means restructuring the grid
  executor, stop and file rather than forcing it.
- **C1 changes what `provision` does on failure** — from leaving a pod up to destroying it. That is
  the intent, but it is a behaviour change on a money path and needs to be stated in the record.
- **B2 shells out to the provider CLI.** Parsing `modal app list --json` couples kinoforge to that
  output shape; keep the parse defensive and fail with a message naming what it could not find.
