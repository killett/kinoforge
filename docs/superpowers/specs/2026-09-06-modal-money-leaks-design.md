# Closing the four money leaks — design (2026-09-06)

## Purpose

The Modal command-matrix campaign (`docs/modal-command-matrix.md`) filed fifteen defects. The
four most urgent all lose money or block the only escape from losing money. This closes them.

| Item | Symptom | Cost shape |
|------|---------|------------|
| U7 | `provision` books an instance no kinoforge command can see or destroy | unbounded, silent — cost $0.13 live |
| U8 + U9 | an `--ephemeral` run has no durable record while it runs, and nothing automatic reaps it afterwards | unbounded, silent |
| U14 | warm-attach cold-boots a duplicate pod while an idle one with the same key is live | doubles the burn rate, every run |
| U15 | `--attach-pod` refuses a healthy pod whose endpoint the ledger holds | forces a fresh boot per upscale |

U14 and U15 compose: with the matcher missing live pods and the explicit override refusing to
attach, there is currently **no way at all** to run a second upscale on an existing pod.

## Themes, not four patches

These are two themes plus one extension, and the design follows that rather than the filing order.

**Theme A — write the durable row BEFORE the billing resource exists.** `provision` and the
ephemeral path both create a live instance before anything records it. This is the same hole that
F12 and ruling C1 closed for `deploy`'s main-ledger path; it is simply still open on these two.

**Theme B — consult the row that already exists.** The attach path merges the ledger's `tags` onto
the live instance and never its sibling `endpoints`; the matcher rejects live candidates without
the operator ever seeing the reason it already computed.

**Extension — make the documented safety net true.** `CLAUDE.md` tells operators to run
`kinoforge sweeper start &` for unsupervised work. It is blind to exactly the run shape that leaves
no ledger row.

## Scope

### A1 — `provision` writes a row and stops double-booking

`_cmd_provision` (`src/kinoforge/cli/_commands.py:255`) calls `provider.create_instance(spec)` and
writes nothing to the ledger — no provisional row before, no real row after — and never checks
whether an instance for this capability key already exists, so it is an unconditional second
create rather than the re-provision its name implies. On Modal the returned id is empty, so the app
is named with a bare `kinoforge-` prefix and the id needed to reap it never exists.

Two changes:
1. Write the same pre-launch provisional row `deploy` writes (`kf_launch_phase=launching`,
   `src/kinoforge/core/orchestrator.py`), then upgrade it to a real row after create. The
   reconciler's provider-agnostic age-out already handles a launching row whose create died, so
   this inherits that recovery for free.
2. Refuse when a live instance for this capability key already exists, naming it, rather than
   booking a second one.

An empty id returned from create must be treated as a failure, not recorded as an instance —
a row that cannot name its pod is not a record.

### A2 — the ephemeral index row is written before `create_instance`

`_record_cold_instance` calls `_ephemeral_index_add` (`src/kinoforge/cli/_commands.py:583`, and
the sibling at `:936`) only after the orchestrator returns, so the row lands at completion and is
stamped with the completion time. For the whole run there is no durable record anywhere.

Move the write ahead of `create_instance`, stamped with launch time. The row must carry the pod id
and endpoints as soon as they exist, because a monitor cannot poll a pod whose endpoint has not
been written down — the campaign could not sample utilisation for one ephemeral cell for exactly
this reason.

`EphemeralIndexRow` already carries `created_at_local`, documented in
`src/kinoforge/core/warm_reuse/ephemeral_index.py` as a "future sweeper TTL backstop". This makes
that description true.

### B1 — `_resolve_attach_pod` merges endpoints, not only tags

`src/kinoforge/cli/_commands.py:1765-1789` merges the ledger entry's `tags` onto the live instance,
then calls `provider.ensure_endpoints(live)` and refuses when the result is empty. On Modal
`get_instance` builds the instance from `modal app list`, which carries no URL, and
`ModalProvider.endpoints` falls back to a per-process `_deployments` dict that a fresh process
never populated. The ledger entry's `endpoints` field is right there and is never read.

Merge `endpoints` from the ledger entry alongside `tags`, before the `ensure_endpoints` call, so
the provider has something to repair rather than nothing to find. The refusal message must also
stop reporting tag keys as evidence about endpoints — it named the wrong field, which is what made
the filed cause a hypothesis rather than a diagnosis.

**Expected to also improve U3** (`status` and `pod lora ls` report no endpoint on Modal), which is
the same root at a different site. That is a prediction to verify, not a claim to assume: if
U3 remains open after this, it stays filed.

### B2 — surface the skip reason the scan already computes

`_scan_warm_candidates` (`src/kinoforge/cli/_commands.py`) already builds a `skipped` list of
`(instance_id, reason)` pairs — `reaper-held`, `provision-held`, `stage-mismatch`, and
`_rc_to_reason(rc, entry)` verdicts. U14 was filed as "the match site logs no reject reason", and
that filing is **wrong about the mechanism**: the reason exists, it just may never reach the
operator.

So B2 is: establish whether that report is surfaced, and surface it if not. This is a $0 step and
it must run **before** any matcher change.

### B3 — fix whatever B2 names

U14's cause is genuinely unknown. The two configs in the reproducer differ only in `upscale.scale`
(`4x` vs `1080p`), which the capability key does not distinguish, so key equality was not the
discriminator — whatever rejected the candidate sits past the key comparison.

This design deliberately does **not** specify the fix. Specifying it now would mean guessing, which
is what produced U13's retracted suspected site. The plan treats B3 as discovery-then-fix: run the
reproducer with B2's output visible, let the recorded reason name the cause, then decide.

If B2's output shows the candidate was never in the list at all, the cause is upstream in
`ledger.find_pods_by_warm_attach_key` / `rows_by_wak` rather than in the eligibility filter, and
B3 addresses that instead.

### C1 — the sweeper covers ephemeral pods

Two halves, per U9:

- **Enumeration.** `kinoforge sweeper start` exposes only `-c` and `--interval-s`. But
  `cfg.sweeper.include_orphans` already exists (`src/kinoforge/core/config.py:1222`, consumed at
  `:1841`), so the daemon may already be able to see the index via config. Establish which is true
  before adding a CLI flag: if config already works, the defect is discoverability, and the fix is
  a flag plus documentation rather than new plumbing.
- **Classification.** Orphan rows carry no heartbeat and no last-used stamp, so a pod that merely
  answers is `LIVE` and no threshold promotes it. `created_at_local` gives an age. Age alone is a
  blunt instrument, so the classifier uses it together with the util probe the Modal provider
  already exposes: an orphan that is old enough AND idle on GPU is reapable. Age without idleness
  must not reap — a long generation is not a leak.

## Non-goals

- U1 (provider-blind matcher), U2, U6, U11, U13 and the open half of U5 stay filed. U13 in
  particular is excluded on purpose: its suspected site was disproved and it needs its own
  observation pass.
- No re-architecture of the warm-reuse key. Adding a provider field to `WarmAttachKey` would move
  every warm-attach hash and is a separate piece of work.
- No live re-proof of RunPod or SkyPilot. They share this code and are expected to benefit, but
  proving that is its own budget line.

## Verification

Operator decision: **live-proven, roughly $2**. Offline green is not sufficient here — the
duplicate-boot miss was only ever reproduced live, and one filed cause was wrong.

Unit and integration tests throughout, red/green. Then live on Modal:

| Proof | Shape | Hardware |
|-------|-------|----------|
| A1 | run `provision`, kill it mid-create, confirm the row names the pod and `destroy` reaps it | A10 |
| A2 | launch an ephemeral generate, read the index **while it runs**, kill the controller, confirm the pod is nameable and reapable | A10 |
| B1 | attach to a warm pod from a fresh process | A10 |
| B2+B3 | the U14 reproducer with skip reasons visible; a second run must attach, not boot | A100 (one pass) |
| C1 | leave an ephemeral pod idle, confirm the daemon reaps it | A10 |

Every live cell obeys the campaign's standing rules: preflight before spend, utilisation polling
during any in-flight generation, teardown proof from a fresh process after the orchestrator exits,
and no pod left running.

A deliberate mid-run kill is the point of A1 and A2, not an accident to avoid: these fixes exist so
that a crashed run leaves a nameable pod, and only a real kill proves it.

## Risks

- **A2 writes a row for a pod that may never exist** (create raises). That is the correct trade —
  ruling C1 already established that a provisional row is kept and aged out rather than removed on
  a failed create — but the ephemeral index has no reconciler, so C1's classifier must be able to
  age out a row whose pod never came up.
- **B3 is unbounded until B2 runs.** If the cause turns out to be structural rather than a guard,
  the plan stops and re-files rather than expanding scope silently.
- **C1 can reap a pod someone is using.** Idleness plus age is the guard; the util probe is the
  evidence. A wrong reap destroys work, so the thresholds must be conservative and the reap must
  say what it observed.
