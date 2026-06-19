# B5b — Non-Mutating Heartbeat Satisfier — DEFERRED

**Status:** DEFERRED (not built)
**Author:** Claude (Opus 4.7), brainstormed with Dr. Twinklebrane
**Date:** 2026-06-18
**Scope:** RunPod heartbeat substrate post-C33

## 1. Decision

B5b is **deferred indefinitely**. The wire-level `HeartbeatEndpoint`
substrate is decommissioned for RunPod (C33 closure 2026-06-17, commit
`c2526ac`) and no replacement satisfier ships. The local `.kinoforge/`
ledger serves as the de-facto same-host substrate for heartbeat
freshness and cross-CLI session identity. The `HeartbeatEndpoint`
Protocol in `src/kinoforge/core/heartbeat_endpoints.py` is preserved
as an extension point for a future cross-machine or cross-account
scenario that today does not exist for this project.

## 2. Scope rationale — why the wire substrate is redundant

The operational scope this project ships against is **single-operator,
single-container, single-host**: every `kinoforge` CLI invocation
originates inside Dr. Twinklebrane's dev container and shares
`/workspace/.kinoforge/` on disk. Under that scope:

- The local ledger (`src/kinoforge/core/lifecycle.py::Ledger`) is
  itself the cross-CLI shared anchor. Process A and process B see the
  same JSON-on-disk entries.
- `Ledger.touch(instance_id, last_heartbeat=now)` already records
  per-tick freshness with timestamp precision sufficient for the
  Layer V dead-man window (`heartbeat_interval_s * 3`, minimum ~30 s).
- The `ledger/{run_id}` cross-process lock (`Ledger._mutate_ttl_s`
  semantics) serializes writes — process A and B never race on the
  same ledger entry.
- B7's cooperative `provision:<id>` session-claim lock (commit
  `b2d5b8b`) serializes ATTACH contention — only one orchestrator
  drives a given warm pod at a time.
- B4 cross-CLI warm-reuse (commit `54d2867`) already reads `ledger`
  entries plus capability_key to pick a warm pod; freshness gate is
  `now - last_heartbeat < dead_man_window`.

Result: every property the wire-level `HeartbeatEndpoint.write/read`
contract was meant to deliver — fresh-vs-stale signal, single-writer
ordering, cross-CLI visibility, dead-pod detection — is already
delivered by the ledger under same-host scope.

## 3. What B5b would have added (and why we don't need it today)

The wire-level substrate's distinctive properties, the ones the
local ledger cannot provide:

| Property | Substrate gives you | Local ledger gap | Activates when |
|---|---|---|---|
| Cross-machine visibility | Process on host 1 can read process-on-host-2's heartbeat. | Hosts have independent `.kinoforge/` directories. | Two operators / a CI runner / a remote workstation share one RunPod account and contend for the same warm pod. |
| Failure-mode isolation from ledger | Substrate is on a different storage path; ledger corruption doesn't blind the heartbeat. | If `.kinoforge/` is wiped or wedged, both warm-reuse AND heartbeat lookup fail. | A regression corrupts the ledger; the substrate would still surface "pod alive, heartbeat N seconds ago" so the operator can recover state. |
| Pod-side recovery from operator wipe | Substrate state is anchored to the pod, not to operator state. | If operator wipes `.kinoforge/` while pod survives, the pod is invisible to all future invocations. | Operator runs `rm -rf .kinoforge/` (intentional reset) but doesn't realise a live RunPod pod is still under their account. |

None of those scenarios currently obtain in this project. When and
if they do, the deferral can be revisited (see §5).

## 4. What survives in current code (intentional)

`RunPodGraphQLHeartbeatEndpoint.write()` is a no-op as of C33-m
(commit `c2526ac`). The `.read()` half remains FUNCTIONAL — it
parses any pre-existing `# _kinoforge_hb:<ISO>` trailer set in
`dockerArgs` at pod-create time. This asymmetry is intentional:

- New pods (post-C33) never carry a marker — the C25 preserve-and-
  merge create-time marker is also gone since `dockerArgs` mutations
  are unsafe.
- Legacy pods (created before C33 landed, if any survived) would
  still parse correctly. This is belt-and-braces backward
  compatibility, not technical debt.

`_HEARTBEAT_SUPPORTED` in `src/kinoforge/core/heartbeat_endpoints.py`
keeps `"runpod"` in the supported set. Membership reflects "a
ledger-backed substrate is available for this provider", not "a
wire-level write satisfier ships for this provider". Removing
RunPod from the set would cascade into `HEARTBEAT_SUBSTRATE_MISSING`
verdicts from `reaper.classify` for every RunPod pod — a behaviour
regression with no compensating safety win.

The `heartbeat_mode` config enum
(`"none" | "graphql-tag" | "selfterm-http" | "ssh-touch"`)
keeps the two forward-looking sentinels (`selfterm-http`,
`ssh-touch`) as documented extension points. Operators must pass
`"none"` for RunPod today; `"graphql-tag"` is documented as a
no-op pass-through. No values are removed because the enum is
public surface.

## 5. Resumption criteria

Build B5b when ANY of the following first occurs:

1. **Second-host contention.** Two operator containers, a CI runner,
   or a remote workstation join the same RunPod account and attempt
   to share warm pods. Trigger: any non-Dr.-Twinklebrane host wants
   to call `kinoforge generate` against a pod the local container
   provisioned.
   **Design pivot it forces:** ship a substrate on a cross-host store
   (S3, or RunPod's own non-mutating metadata fields if RunPod ever
   adds one — `Pod.uptimeSeconds` is permanently disqualified per
   C33 (d) banner test, but other read-only fields may emerge).

2. **Ledger durability incidents.** Observed-in-practice cases where
   `.kinoforge/` is corrupted, wedged on cross-process lock, or
   wiped while RunPod pods survive. Trigger: a single incident where
   recovery required manual `runpodctl` listing because kinoforge
   couldn't find a known-live pod.
   **Design pivot it forces:** ship a substrate independent of
   ledger storage — pod-side write into a kinoforge-owned S3 prefix
   would survive ledger loss.

3. **Provider whose semantics don't transfer.** If a new compute
   provider lands whose ledger-as-substrate assumption fails — e.g.
   a provider where pod identity is not stable across orchestrator
   restarts, or where ledger writes can race with provider-side
   state changes — the wire-level abstraction becomes load-bearing
   again. Trigger: new provider PR where reaper classification
   surfaces incoherent verdicts under load.
   **Design pivot it forces:** revisit the `HeartbeatEndpoint`
   contract itself; same-host assumption may need to be relaxed.

4. **B7 marker semantics needed for non-warm-reuse purpose.** If a
   new feature (e.g. a cross-orchestrator audit trail, a multi-CLI
   coordination mode) requires the `# _kinoforge_hb:<ISO>` marker
   semantics that C33 disabled, B5b becomes the path back.
   **Design pivot it forces:** spec the new feature first, then
   pick a substrate (likely selfterm-http or S3) that supports the
   marker contract.

In all four cases, the wire-level `HeartbeatEndpoint` Protocol is
already in place — no breaking surface change is needed to ship the
satisfier. The deferral is reversible.

## 6. In-code changes shipped alongside this spec

To prevent code drift from the post-C33 reality:

- `src/kinoforge/providers/runpod/heartbeat.py` module docstring
  acknowledges B5b deferred; `RunPodGraphQLHeartbeatEndpoint` class
  docstring documents the asymmetric `read`-functional /
  `write`-disabled state as permanent under the same-host scope, not
  a temporary C33 patch.
- `src/kinoforge/core/heartbeat_endpoints.py` —
  `_HEARTBEAT_SUPPORTED` docstring acknowledges that `"runpod"`
  membership means "ledger-backed substrate available", not
  "wire-level write satisfier shipped".

No registry edits, no Protocol changes, no test changes.

## 7. Cross-references

- C33 closure: `c2526ac` (heartbeat write disabled), preceded by
  `aacd49e` (start_heartbeat moved post-provision). Probe evidence
  files: `tests/live/_c33_probe_{h,j,k,l,m,n}_evidence.json`.
- C33 (d) banner test: `48f012d` —
  `tests/core/test_no_top_level_pod_uptime_reads.py`.
- C33 (a) classify_run refinement: `0ed4db5` — corroborating signal
  for negative-uptime RESTARTED verdict.
- Local ledger: `src/kinoforge/core/lifecycle.py::Ledger`.
- B4 cross-CLI warm-reuse: commit `54d2867`.
- B5a heartbeat substrate: commit `bade08c`.
- B7 cooperative session-claim lock: commit `b2d5b8b`.
- Stage E follow-up close-outs: PROGRESS §"Stage E follow-ups
  (filed 2026-06-18)" and Parked queue.
