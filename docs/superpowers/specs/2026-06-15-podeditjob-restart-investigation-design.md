# C33 — `podEditJob` restart-cause investigation

**Status.** Spec drafted 2026-06-15. Diagnose-only phase. No production-code mutation.

**Lineage.** C25 (heartbeat preserve-and-merge) → C26 (util-aware stall) → C27 (restart-loop stall detection) → C28 (restart-loop prevention + diagnostic capture) → C29 (boot-phase heartbeat) → C30 (restart-loop fault-isolation tree, partial-close at A0prime / A1a — both classified `RESTARTED` via negative-uptime rule) → **C33 (this spec — confirm or deny that `podEditJob` mutation itself is the restart trigger, and disambiguate the C30 orphan negative-uptime signal)**.

**Companion-phase pointers.**
- `tests/live/_c26_phase_b_smoke_evidence.json` — `pod o4leekoaqru8cg`, `uptime_seconds=1` at every 30s util tick during Wan 14B + ComfyUI run. Heartbeat cadence `30s`, restart cadence `~30s`. Correlation is the C33 anchor.
- `tests/live/_c30_phase_a1a_evidence.json` + `tests/live/_c30_phase_a0prime_evidence.json` — `ubuntu:22.04` and `runpod/pytorch:2.4.0` pods with NO heartbeat ticks and NO `podEditJob` mutations, but oscillating + negative `uptimeInSeconds` over 10 min. C30 reclassified both as `restarted` after `c30_probe.classify_run` gained a negative-uptime rule. C33 P0 disambiguates whether that rule was sound.
- `src/kinoforge/providers/runpod/heartbeat.py` — `RunPodGraphQLHeartbeatEndpoint.write` issues `podEditJob` with merged `dockerArgs` every tick.
- `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md` §5 — pre-existing assumption that `podEditJob` is non-restarting (verified `dockerArgs` is preserved structurally; did NOT verify the mutation itself is restart-safe).

---

## 1. Goal

Confirm or deny, with decisive empirical evidence, that issuing a RunPod `podEditJob` GraphQL mutation against a running pod restarts the container — regardless of whether the mutation changes meaningful fields. "Confirmed" means a single A/B probe whose `lastStartedAt` outcome matches the pre-registered prediction in §3 P1. "Denied" means the same probe falsifies the prediction AND the read-only denial-branch checks rule out the named fast-falsify alternatives in §2.

Secondary goal: disambiguate the C30 orphan negative-uptime signal — determine whether `runtime.uptimeInSeconds < 0` is a RunPod API quirk on idle pods (in which case A0prime / A1a were SURVIVED misclassified as RESTARTED) or a real second-cause restart signal (in which case the investigation widens).

Output: single named outcome written into `PROGRESS.md` under a new C33 entry citing both sidecars and S3 trap prefixes. Fix is a follow-up phase (C34a-d, routing per §7).

## 2. Non-goals

1. Shipping any fix. C33 does not modify `kinoforge.orchestrator`, `engines/`, `providers/`, `core/heartbeat_*`, `core/util_endpoints.py`, `core/session_claim.py`, or `render_provision`. Fix is C34.
2. Probing mutable fields other than `dockerArgs`. Brainstorm decision: fix path is "stop using `podEditJob` entirely"; mapping the full restart-trigger surface across `imageName` / `env` / `ports` adds spend without altering fix direction.
3. Reproducing on secure-cloud tier. Community-cloud only (matches C28 + C30 baseline).
4. Reverting C25 wire-fix or removing `RunPodGraphQLHeartbeatEndpoint` in C33. Revert + restore `_RUNPOD_HEARTBEAT_SAFE_ENGINES` guard is C34a, contingent on P1 = `confirmed`.
5. Reproducing the full Wan 14B failing smoke. C26 Phase B sidecar already provides the correlation evidence; C33 isolates the mechanism on a $0.022 probe instead of a $0.30 reproduction.

## 3. Hypothesis

**Primary (H_main).** Issuing `podEditJob` against a running RunPod pod restarts the container irrespective of whether the mutation changes meaningful fields. Therefore B5a's `RunPodGraphQLHeartbeatEndpoint.write` — one `podEditJob` per 30s heartbeat tick — drives any pod with `heartbeat_mode=graphql-tag` into a chronic restart loop synchronized to the heartbeat cadence. Confirmation predicts:

1. P1 mutation issued at `t_mut` → `pod.lastStartedAt` reads `≥ t_mut` within 60s of the mutation.
2. P1 `runtime.uptimeInSeconds` post-mutation drops to a value strictly less than `t0_uptime + elapsed_since_mutation`.

**Alternative hypotheses** (pre-registered, with discriminators):

| Label | Claim | Discriminator |
|---|---|---|
| **H_orphan_quirk** | RunPod `runtime.uptimeInSeconds` can return non-physical negative values without actual restart; C30's negative-uptime rule misclassified A0prime / A1a. | P0: `lastStartedAt` stable across full 10-min window AND `≥ 1` negative uptime sample → quirk confirmed. |
| **H_orphan_second_cause** | Community-cloud RunPod pods on minimal images self-restart (port health probe misfire on missing listener, OOM on small instance, kernel quirk, restartPolicy auto-cycle). | P0: `lastStartedAt` advances `≥ 2` times AND `≥ 1` negative uptime sample → second cause confirmed. |
| **H_bash_trailer_breaks** | C25 `# _kinoforge_hb:<ISO>` trailer breaks bash parsing under some shell/init combo; pod boot fails with `set -euo pipefail`, RunPod `restartPolicy=ALWAYS` cycles it. | Read-only: locally `bash -n` the rendered `dockerArgs` from C26-B reconstruction. Parse-clean → falsified. |
| **H_selfterm_30s_watchdog** | Phase 24 in-pod selfterm has a watchdog firing at ~30s under heartbeat-mode misconfig. | Read-only: grep `src/kinoforge/providers/runpod/selfterm.py` for 30s constants / watchdog logic. None found → falsified. |
| **H_network_race** | Concurrent `_read_dockerargs` + `podEditJob` from different orchestrator processes corrupt state. | Read-only: read `src/kinoforge/core/session_claim.py` + `core/heartbeat_loop.py` to verify single-writer invariant per B7. Invariant holds → falsified. |

## 4. Probe matrix

### P0 — Orphan disambiguation (always runs)

| Element | Value |
|---|---|
| Image | `ubuntu:22.04` |
| `ports` | unset |
| `dockerArgs` | `bash -c "<C28 trap pre-amble> && sleep 600"` |
| GPU type ID | `NVIDIA GeForce RTX 3070` |
| `cloudType` | `ALL` (matches C30 A0prime supply path) |
| Mutations | NONE — pure observation |
| Poll window | 600s |
| Poll interval | 30s |
| Poll fields | `runtime.uptimeInSeconds`, `lastStartedAt`, `desiredStatus` |
| Pre-registered cost | $0.022 |

Pre-registered sidecar `tests/live/_c33_probe_p0_evidence.json`:

```json
{
  "phase": "p0",
  "run_id": "c33-p0-<localtime YYYYMMDDTHHMMSS>",
  "pod_id": "<rp-id>",
  "image": "ubuntu:22.04",
  "gpu_type_id": "NVIDIA GeForce RTX 3070",
  "cents_per_hr": 13,
  "s3_prefix": "boot-logs/c33-p0-<run_id>/",
  "fire_count": 0,
  "poll_trail": [
    [<elapsed_s>, <uptime_int_or_null>, "<last_started_at_iso_or_null>", "<desired_status>"]
  ],
  "n_last_started_at_advances": 0,
  "n_negative_uptime_samples": 0,
  "n_null_uptime_samples": 0,
  "verdict": "orphan_quirk | orphan_real_restart | ambiguous",
  "est_spend_usd": 0.022,
  "captured_at": "<ISO local>"
}
```

Verdict rules (mechanical, applied by `_classify_p0` in `c33_probe.py`). Verdict enum: `{orphan_quirk, orphan_real_restart, ambiguous}` — no fourth value:
- `n_last_started_at_advances >= 2` → **`orphan_real_restart`** (H_orphan_second_cause confirmed; P1 deferred to C34c).
- `n_last_started_at_advances == 1 AND n_negative_uptime_samples == 0` → **`ambiguous`** (rerun P0 once; if still ambiguous, treat as `orphan_real_restart`).
- `n_last_started_at_advances == 1 AND n_negative_uptime_samples >= 1` → **`ambiguous`** (same rerun rule).
- `n_last_started_at_advances == 0` (regardless of negative-uptime count) → **`orphan_quirk`** (H_orphan_quirk confirmed; the C30 negative-uptime rule was over-broad — either negatives appeared without restart, or none appeared at all and the rule's premise never even triggered).

### P1 — Main hypothesis A/B (runs only if P0 = `orphan_quirk`)

| Element | Value |
|---|---|
| Image | `ubuntu:22.04` |
| `ports` | unset |
| `dockerArgs` (initial) | `bash -c "<C28 trap pre-amble> && sleep 600"` |
| GPU type ID | `NVIDIA GeForce RTX 3070` |
| `cloudType` | `ALL` |
| Sequence | (1) `create_probe_pod`; (2) poll until `uptimeInSeconds >= 90` (pre-mutation stabilization); (3) snapshot `lastStartedAt` as `t0_last_started_at`, `uptimeInSeconds` as `t0_uptime`; (4) issue ONE `podEditJob` with `dockerArgs` mutated via `_merge_marker(base, datetime.now(tz=local))` from `heartbeat.py:105` (B5a's exact merge); (5) begin 90s poll at 10s interval of `lastStartedAt` + `uptimeInSeconds` |
| Pre-registered cost | $0.013 (~6 min pod lifetime) |

Pre-registered sidecar `tests/live/_c33_probe_p1_evidence.json`:

```json
{
  "phase": "p1",
  "run_id": "c33-p1-<localtime YYYYMMDDTHHMMSS>",
  "pod_id": "<rp-id>",
  "image": "ubuntu:22.04",
  "gpu_type_id": "NVIDIA GeForce RTX 3070",
  "cents_per_hr": 13,
  "s3_prefix": "boot-logs/c33-p1-<run_id>/",
  "t0_last_started_at": "<iso>",
  "t0_uptime": <int>,
  "t0_snapshot_at": "<iso local>",
  "mutation_issued_at": "<iso local>",
  "mutation_response": {"data": {"podEditJob": {"id": "..."}}, "errors": null},
  "post_mutation_trail": [
    [<elapsed_s_from_mutation>, <uptime_int_or_null>, "<last_started_at_iso_or_null>"]
  ],
  "last_started_at_advanced": <bool>,
  "last_started_at_advance_observed_at_elapsed_s": <float_or_null>,
  "uptime_reset_observed": <bool>,
  "uptime_monotonic_for_90s": <bool>,
  "verdict": "confirmed | denied | ambiguous",
  "est_spend_usd": 0.013,
  "captured_at": "<ISO local>"
}
```

Verdict rules (mechanical, applied by `_classify_p1` in `c33_probe.py`):
- `last_started_at_advanced == true AND uptime_reset_observed == true` (both within 60s of mutation) → **`confirmed`**.
- `last_started_at_advanced == false AND uptime_monotonic_for_90s == true` → **`denied`**.
- Otherwise → **`ambiguous`** (rerun P1 once; if still ambiguous, treat as `denied` and escalate to denial branch).

Operational definitions:
- `last_started_at_advanced`: any post-mutation sample's `lastStartedAt` > `t0_last_started_at` (string compare on ISO is sound for tz-aware timestamps from same source).
- `uptime_reset_observed`: any post-mutation sample's `uptimeInSeconds` `< t0_uptime`.
- `uptime_monotonic_for_90s`: every consecutive `(prev, curr)` pair of non-null uptime samples in the 90s window satisfies `curr >= prev - 2` (the `- 2` absorbs RunPod's documented `lastStatusChange` rounding without admitting real restarts).

### P_alt_branch — denial-branch read-only probes (run only if P1 = `denied`)

Three zero-spend probes. Run in order; the first that survives is the new lead.

1. **H_bash_trailer_breaks check.** Reconstruct C26-B rendered `dockerArgs` from `tests/live/cfg_c26_phase_b.yaml` via the existing `provisioner.py` + `selfterm.py` + `engines/comfyui/__init__.py:render_provision` render path. Pipe the rendered string to `bash -n` (no-exec parse) in the local pixi env. Result: PASS = parse-clean = H_bash_trailer_breaks **falsified**. FAIL = parse-error = new lead.
2. **H_selfterm_30s_watchdog check.** Grep `src/kinoforge/providers/runpod/selfterm.py` for any numeric literal in `[20, 30, 40]` and any of `{watchdog, timer, sleep, alarm, signal}`. Inspect each hit manually. No 30s constant + no watchdog logic = H_selfterm_30s_watchdog **falsified**. Any 30s timer present + plausibly tied to liveness = new lead.
3. **H_network_race check.** Read `src/kinoforge/core/session_claim.py` and `core/heartbeat_loop.py`. Confirm: (a) `HeartbeatLoop` is single-threaded per-process; (b) `session_claim` B7 lock guarantees single-orchestrator writer per pod. Both invariants hold = H_network_race **falsified**. Either broken = new lead.

If ALL three falsify → outcome `HYPOTHESIS_DENIED_NO_LEAD_REMAINING`; escalate to RunPod support with P0 + P1 evidence bundle.

## 5. Spend budget

| Item | Value |
|---|---|
| Phase cap (hard) | $0.20 |
| Cumulative ledger cap (shared with C30) | $5.00 |
| Per-probe cap | $0.05 |
| Expected case (P0 orphan_quirk + P1 confirmed) | $0.022 + $0.013 = $0.035 |
| Worst case (P0 ambiguous-rerun + P1 ambiguous-rerun) | $0.022 × 2 + $0.013 × 2 = $0.070 |
| Denial branch (read-only) | $0.00 |

Ledger: append entries to existing `tests/live/_c30_spend_ledger.json` with `phase` ∈ `{p0, p0_rerun, p1, p1_rerun}`. `c30_probe.assert_under_cap(ledger_path, hard_cap_usd=5.00)` called BEFORE every `podFindAndDeployOnDemand`.

Per-pod monetary fuse: 12-minute hard kill via `destroy_with_retry` in the `finally:` clause of every probe test. RTX 3070 at 13¢/hr × 12 min = $0.026 ceiling per pod.

## 6. Files to commit

| Path | Mode | Purpose |
|---|---|---|
| `docs/superpowers/specs/2026-06-15-podeditjob-restart-investigation-design.md` | new | This spec |
| `tests/live/test_c33_p0_orphan_disambig_live.py` | new | P0 probe; RED-first skeleton committed before any spend |
| `tests/live/test_c33_p1_podeditjob_restart_ab_live.py` | new | P1 probe; RED-first skeleton committed before any spend |
| `tests/live/_c33_probe_p0_evidence.json` | created at runtime | P0 sidecar |
| `tests/live/_c33_probe_p1_evidence.json` | created at runtime | P1 sidecar |
| `src/kinoforge/diagnostics/c33_probe.py` | new (or inline-extend `c30_probe.py`) | Adds three helpers: `snapshot_last_started_at(client, pod_id) -> str | None`; `poll_with_last_started_at(client, pod_id, window_s, interval_s, *, sleep, clock) -> list[tuple[float, int | None, str | None, str | None]]`; `issue_single_pod_edit_job(client, pod_id, new_docker_args) -> dict`. Plus mechanical classifiers `_classify_p0(sidecar) -> Verdict_P0` and `_classify_p1(sidecar) -> Verdict_P1`. |
| `tests/diagnostics/test_c33_probe.py` | new | Unit tests for the three helpers + two classifiers; pure offline, injected GraphQL client; RED-first. |

Inline-extend `c30_probe.py` preferred over new `c33_probe.py` module — keeps the diagnostics surface compact and the spend-ledger helpers single-sourced. Decision deferred to plan phase.

No production-code mutation. C25 + B5a code untouched in C33.

## 7. Exit criteria

C33 closes when ANY of:

1. P1 verdict = `confirmed` → write `PROGRESS.md` C33 entry citing both sidecars + S3 prefixes; declare RCA = "podEditJob mutation restarts container"; spec follow-up phase per §8 row 1.
2. P0 verdict = `orphan_real_restart` → declare two-cause finding; P1 deferred; spec C34c per §8 row 3 to characterize the second cause first; P1 reopens only after second cause closed.
3. P1 verdict = `denied` AND all three denial-branch read-only probes falsify → declare `HYPOTHESIS_DENIED_NO_LEAD_REMAINING`; escalate to RunPod support; spec a follow-up only if support response yields a lead.
4. Any probe forces ambiguous-rerun-of-rerun → escalate to operator; brainstorm cycle restarts.

## 8. Fix decision tree

| P0 verdict | P1 verdict | Next phase | Fix direction |
|---|---|---|---|
| `orphan_quirk` | `confirmed` | **C34a** | Restore `_RUNPOD_HEARTBEAT_SAFE_ENGINES` guard immediately (engines with `provision_script` refuse `graphql-tag` mode). Then C34b: move heartbeat carrier out of `podEditJob` entirely. Two viable carriers: (i) in-pod selfterm writes `/tmp/kinoforge.hb` periodically, orchestrator reads via existing `util_endpoints.py` HTTP probe; (ii) drop B5a write entirely and use `pod.runtime.lastStartedAt` as the liveness signal — RunPod itself authoritatively reports container start, no orchestrator-side mutation needed. Choice between (i) and (ii) is a C34 brainstorm. |
| `orphan_quirk` | `denied` (with surviving denial-branch lead) | **C34b** | Investigate the surviving lead. New brainstorm scoped to that lead. |
| `orphan_real_restart` | (deferred) | **C34c** | Characterize the second cause: vary image, GPU type ID, `cloudType` (community vs secure), explicit `restartPolicy`. Spec is a separate fault-isolation tree like C30. Only after C34c closes does P1 re-run (since the second cause confounds P1's measurement). |
| `orphan_real_restart` | (after C34c closes) | **C34d** | Either confirms H_main on top of the second cause, or denies it; routes to C34a / support per outcome. |
| `orphan_quirk` | `denied` (all denial-branch falsifies) | **C34e** | RunPod support escalation. No further internal phase until support responds. |

## 9. Constraints (inherited)

- `c30_probe.assert_under_cap(ledger_path, hard_cap_usd=5.00)` called BEFORE every `podFindAndDeployOnDemand`.
- `c30_probe.destroy_with_retry(client, pod_id=...)` called in the `finally:` clause of EVERY probe test (C31 pattern).
- Local TZ ISO timestamps via `datetime.now(tz=<local>)`. Never `utcnow()`.
- RED test scaffolds commit BEFORE any live spend (CLAUDE.md durability rule).
- Diagnostic artifacts → S3 `kinoforge-pod-diagnostics`, prefix `boot-logs/c33-<phase>-<run_id>/`, via existing C28 trap pre-amble.
- Autonomous execution — no user-gates, no pre-spend confirmation handshakes (per session memory `feedback_autonomous_no_gates`).
- `pixi run preflight` is the only mechanical pre-spend gate (zero active pods + clean working tree + creds present).
- Probe tests pixi env: `pixi run -e live-hosted` if any hosted SDK imports leak in via shared diagnostics module; otherwise default env. Decide at plan phase.

## 10. Open questions deferred to plan phase

1. Inline `c33_probe.py` helpers into `c30_probe.py` vs new module. Trade-off: single-source vs blast radius of edits to a phase-closed module.
2. Whether to capture `desiredStatus` AND `lastStatusChange` (in addition to `lastStartedAt`) in the P0 poll trail. Adds two GraphQL fields per query; zero cost; potential corroboration value. Default: yes, capture both, ignore unless P0 outcome is ambiguous.
3. Whether P1's mutation should use a FRESH timestamp (B5a behavior) or an INERT timestamp (e.g. `1970-01-01T00:00:00-07:00`) to discriminate "any mutation restarts" from "only meaningfully-changed mutations restart". Spec defers — fresh timestamp matches the production code path exactly and is the conservative test. If P1 confirms, the fix path is independent of which-mutations-restart anyway.
4. Whether to capture C26-B's rendered `dockerArgs` string from a real failing smoke (for the H_bash_trailer_breaks read-only probe in §4 P_alt_branch) vs reconstructing from cfg. Defer to denial-branch trigger.
