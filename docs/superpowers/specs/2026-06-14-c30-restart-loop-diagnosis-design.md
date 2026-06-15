# C30 — RunPod container restart-loop root-cause diagnosis

**Status.** Spec drafted 2026-06-14. Diagnose-only phase. No production-code mutation.

**Lineage.** C25 (heartbeat preserve-and-merge) → C26 (util-aware stall) → C27 (restart-loop stall detection) → C28 (restart-loop prevention + diagnostic capture) → C29 (boot-phase heartbeat) → **C30 (this spec — diagnose the *why* of the chronic ~30 s container restart loop captured by C28)**.

**Companion-phase pointers.** C28 Phase A v5 sidecar `tests/live/_c28_phase_a_evidence.json` captured 20 identical ~31 s restart cycles on `runpod/pytorch:2.4.0` with `provision_script` reaching `git clone ComfyUI` before container death. C28 Phase B (prebake image) is a candidate mitigation for multiple possible RCAs but is operator-blocked on Docker Hub push (PROGRESS.md line 208). C31 (`_destroy_safely` verify-and-retry against restart-policy race) is open; C30 inlines its pattern so leaks are prevented before C31 merges.

---

## 1. Goal

Confirm the root cause of the chronic ~30 s container restart loop observed in C28 Phase A v5. "Confirmed" means:

1. One decisive experiment whose outcome matches exactly one named hypothesis from the fault-isolation tree (§3).
2. One inverse-control experiment whose outcome flips that hypothesis's prediction the opposite way.

Output is a single named hypothesis written into `PROGRESS.md` under the C30 entry, with both sidecar paths and both S3 bundle prefixes cited. The mitigation is **not** part of C30 — it is a follow-up phase chosen mechanically per the table in §6.

## 2. Non-goals

1. **Shipping any fix.** C30 does not modify `kinoforge.orchestrator`, `engines/`, `providers/`, `render_provision`, or the prebake image. The fix is a follow-up phase (C32+).
2. **Replicating C28's full Wan provision as the primary control.** C28's 20-cycle evidence is the reproduction baseline. A6 (full Wan) runs only if the platform-level branch exonerates platform AND we need to confirm the clone-or-later path is the killer.
3. **Reproducing on secure-cloud tier.** Community-cloud only — the same backplane class where C28 observed the loop. Cross-tier comparison is a separate study.
4. **General hardening of `kinoforge deploy` against restart loops.** C27 already ships protection (`RESTART_LOOP_REAP`). C30 is forensic, not preventive.

## 3. Fault-isolation tree

The tree is null-first depth-first serial (per brainstorm decision). Each node has a single decisive verdict; an inverse-control sibling runs only when the decisive verdict points at a hypothesis.

### Phase A1 — Platform + healthcheck (always runs)

- **A1a.** Stock RunPod community-cloud GPU pod (cheapest tier — `NVIDIA RTX A2000` or `NVIDIA RTX A4000`). Image `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`. `ports` field **unset**. `dockerArgs = bash -c "<C28 trap pre-amble> && sleep 600"`. Observe for 10 min via GraphQL `pod(podId)` poll @ 30 s + S3 trap-fire count.
  - SURVIVED (0 fires, `runtime.uptimeSeconds` monotonically increasing over window) → A1a green. Proceed to A1b.
  - RESTARTED (≥ 3 fires in 10 min, uptime resets to 0 or `lastStatusChange` advances ≥ 3×) → fork to A0' (image-isolation).
  - AMBIGUOUS (1–2 fires) → repeat A1a once. If still ambiguous, treat as RESTARTED.

- **A1b.** Same as A1a but with `ports = "8188/http"` declared. Same provision script.
  - RESTARTED ≥ 3 in 10 min → matches hypothesis **H1 (port healthcheck on declared port without listener)**. Proceed to A1c.
  - SURVIVED → healthcheck ruled out. Enter provision walk-down at A2.
  - AMBIGUOUS → repeat once.

- **A1c — INVERSE CONTROL** (runs only if A1b RESTARTED). Same as A1b but `dockerArgs = bash -c "<C28 trap pre-amble> && python3 -m http.server 8188 & sleep 600"`. The HTTP server binds 8188 within ~1 s of pod boot.
  - SURVIVED → H1 confirmed. C30 CLOSED with RCA = H1.
  - RESTARTED → H1 falsified. Re-open A1b interpretation; if A1b reproduced reliably and A1c does not, the difference between them is the listener, but if A1c also restarts the cause may not be the declared port at all. Walk-down resumes at A2 with the residual platform-level hypothesis open.

### Phase A0' — Image-isolation fork (runs only if A1a RESTARTED)

- **A0'a.** `ubuntu:22.04` minimal image. `ports` unset. `dockerArgs = bash -c "apt-get -qq update && apt-get -qq install -y awscli >/dev/null && <C28 trap pre-amble> && sleep 600"`. AWS CLI install is required because the trap pre-amble assumes `aws` on PATH.
  - SURVIVED → matches hypothesis **H2 (`runpod/pytorch:2.4.0` PID 1 quirk)**. Inverse control is to re-run A1a on `runpod/pytorch:2.4.0` with an explicit `init` (e.g. wrap with `tini -- bash -c "sleep 600"`) — if survives → H2 confirmed.
  - RESTARTED → platform unconditional kill on community-cloud. Very unlikely; if observed, escalate to operator + file RunPod support ticket. C30 partial-close.

### Phase A2..A6 — Provision walk-down (runs only if A1a + A1b BOTH SURVIVED)

The walk-down is a ladder over what the `provision_script` does. Each rung adds one variable. Stop at the first RESTARTED. Run that rung's inverse immediately.

- **A2.** `kinoforge deploy` with cfg `c30_phase_a2.yaml`. `provision_script` contents: `echo c30-a2-baseline && sleep 600`. Tests the kinoforge orchestrator + selfterm + `render_provision` preamble alone.
  - RESTARTED → matches hypothesis **H5 (selfterm bootstrap edge case)**. Inverse: re-run with `cfg.provider.runpod.selfterm_enabled = False` (assume this knob exists; if not, ship a one-line bypass via env var). Inverse SURVIVED → H5 confirmed.
  - SURVIVED → A3.

- **A3.** `c30_phase_a3.yaml`. `provision_script`: `git clone https://github.com/comfyanonymous/ComfyUI /workspace/ComfyUI && sleep 600`.
  - RESTARTED → matches hypothesis **H_clone (clone-phase OOM / disk pressure / network)**. Inverse: replace clone with `dd if=/dev/zero of=/tmp/clone-equivalent bs=1M count=$(estimated_clone_size_mb)` then `sleep 600`. If inverse RESTARTED → confirms disk/RAM pressure; if SURVIVED → confirms network or git-specific edge.
  - SURVIVED → A4.

- **A4.** `c30_phase_a4.yaml`. A3 + `cd /workspace/ComfyUI && pip install -r requirements.txt && sleep 600`.
  - RESTARTED → matches hypothesis **H_pip (pip install RAM blowup or post-install import side-effect)**. Inverse: same as A4 but `pip install --no-deps -r requirements.txt` (skips transitive resolution; lower RAM ceiling). SURVIVED → H_pip confirmed at the resolver layer.
  - SURVIVED → A5.

- **A5.** `c30_phase_a5.yaml`. A4 + Kijai Wan custom-node clones into `ComfyUI/custom_nodes/` + their `pip install -r requirements.txt` per node + the `python -c "import comfy"` smoke. No model downloads, no server start.
  - RESTARTED → matches hypothesis **H4 (Kijai / KJNodes import crashes PID 1)**. Inverse: same as A5 but skip the `import comfy` smoke. SURVIVED → H4 confirmed; if still RESTARTED, the crash is at provision time, not smoke time.
  - SURVIVED → A6.

- **A6.** `c30_phase_a6.yaml` — byte-identical to C28 Phase A v5 cfg with both reap predicates set false. This is a control that must reproduce the loop.
  - RESTARTED → expected, but we've passed all walk-down rungs as SURVIVED — implying a variable in A6 that none of A2-A5 isolated. Re-examine: model downloads (added in A6 only), the ComfyUI server start, or the warmup. Sub-investigation; partial-close C30 if not narrowed within $0.50 additional spend.
  - SURVIVED → red flag. C28's environment has drifted; the bug is gone in the wild. Re-baseline C28 Phase A v5 verbatim; if THAT no longer reproduces, close C30 with `outcome = NO_REPRODUCTION_BUG_FLED`.

### Tree diagram

```
                       A1a
                  ┌─────┴─────┐
              SURVIVED      RESTARTED
                  │             │
                  A1b           A0'a
              ┌────┴────┐    ┌──┴──┐
          SURVIVED  RESTARTED SURV  RESTART
              │         │      │      │
              A2..A6  A1c    H2    escalate
              ladder  ┌──┴──┐
                    SURV  RESTART
                     │      │
                    H1    walk-down at A2
```

## 4. Spend budget

- **Phase cap.** $2.00 across all C30 live experiments.
- **Hard cap enforcement.** `tests/live/_c30_spend_ledger.json` accumulates `est_spend_usd` per probe. Every live test calls `c30_probe.assert_under_cap(ledger_path, hard_cap_usd=1.50)` BEFORE pod create. Cumulative ≥ $1.50 aborts the test (raises before any RunPod call).
- **Per-probe cap.** Each test asserts its own probe stays under $0.10 (10 min × $0.20/hr GPU = $0.033 typical; $0.10 cap absorbs price jitter).
- **Expected case.** If H1 is confirmed (which the 30 s cadence strongly suggests — RunPod's port healthcheck has documented ~30 s grace), total spend = A1a + A1b + A1c ≈ $0.10.
- **Worst case full walk-down + inverse.** A1a + A1b + A2 + A3 + A4 + A5 + A6 + one inverse ≈ $0.27.

## 5. Files to commit

### New module

`src/kinoforge/diagnostics/c30_probe.py` — pure helper, no `kinoforge.orchestrator` dependency. Exposes:

| Symbol | Purpose |
|---|---|
| `create_probe_pod(client, image, ports, provision_script, env, gpu_type_id) -> str` | Direct GraphQL `podFindAndDeployOnDemand` wrapper. Returns `pod_id`. Reuses C28's S3 trap pre-amble renderer (or has an equivalent inlined). |
| `PodStatusPoller(client, pod_id, window_s=600, interval_s=30)` | Polls `pod(podId)` for `lastStatusChange` and `runtime.uptimeSeconds`. Returns trail `[(t, uptime_s, status), …]`. |
| `count_trap_fires(s3_client, bucket, prefix) -> int` | Lists `boot-logs/<run_id>/diag-*.txt`. Returns object count. |
| `classify_run(poll_trail, fire_count) -> Verdict` | `SURVIVED` iff `fire_count == 0` AND uptime monotonically increases over window. `RESTARTED_N(n)` iff `fire_count >= 3`. `AMBIGUOUS` for 1–2 fires. |
| `assert_under_cap(ledger_path, hard_cap_usd=1.50)` | Reads ledger JSON; raises if cumulative ≥ cap. |
| `destroy_with_retry(client, pod_id, attempts=5, sleep_s=3)` | Issues `podTerminate`; polls `myself.pods`; re-issues if `pod_id` still present. C31 pattern inlined. |

### Spend ledger

`tests/live/_c30_spend_ledger.json` — accumulating record. Schema:

```json
{
  "cumulative_usd": 0.0,
  "entries": [
    {
      "phase": "a1a",
      "pod_id": "...",
      "gpu_type_id": "...",
      "cents_per_hr": 20,
      "start_ts": "2026-06-14T...",
      "end_ts": "2026-06-14T...",
      "est_spend_usd": 0.033
    }
  ]
}
```

Local-TZ timestamps per global rule.

### Test files (committed up front as RED scaffolds, satisfying durability rule)

```
tests/diagnostics/test_c30_classify_run.py
tests/diagnostics/test_c30_count_trap_fires.py
tests/diagnostics/test_c30_spend_ledger.py
tests/diagnostics/test_c30_create_probe_pod_wire_shape.py
tests/diagnostics/test_c30_pod_status_poller.py
tests/diagnostics/test_c30_destroy_with_retry.py

tests/live/test_c30_phase_a1a_sleep_no_port_live.py
tests/live/test_c30_phase_a1b_sleep_port_declared_live.py
tests/live/test_c30_phase_a1c_sleep_port_listener_live.py
tests/live/test_c30_phase_a0prime_alt_image_live.py
tests/live/test_c30_phase_a2_empty_provision_live.py
tests/live/test_c30_phase_a3_clone_only_live.py
tests/live/test_c30_phase_a4_clone_pip_live.py
tests/live/test_c30_phase_a5_custom_nodes_live.py
tests/live/test_c30_phase_a6_full_wan_control_live.py
```

Each live test:

1. Session-scoped fixture runs `pixi run preflight` — asserts `.env` loaded, zero active RunPod pods, clean git tree.
2. Reads `_c30_spend_ledger.json`; calls `assert_under_cap`.
3. Reads predecessor sidecar (`tests/live/_c30_phase_<X>_evidence.json`). If predecessor's verdict ≠ expected gate, calls `pytest.skip("gated on Phase X = <expected>, found <actual>")`.
4. Calls `c30_probe.create_probe_pod(...)` with the phase's exact arguments.
5. Polls 10 min via `PodStatusPoller`. Counts S3 trap fires.
6. Writes own sidecar `_c30_phase_<X>_evidence.json` with `{outcome, run_id, pod_id, s3_prefix, fire_count, poll_trail, est_spend_usd, captured_at, hypothesis_matched}`.
7. `atexit` hook + finalizer: `destroy_with_retry`. Ledger updated with actual end timestamp + final spend.
8. Asserts the phase's expected outcome (or `pytest.xfail` for the RED-scaffold phase before its run produces a sidecar).

### cfg files (for A2–A6 only; A1a/A1b/A1c/A0' bypass `kinoforge deploy`)

```
src/kinoforge/cfg/c30_phase_a2.yaml
src/kinoforge/cfg/c30_phase_a3.yaml
src/kinoforge/cfg/c30_phase_a4.yaml
src/kinoforge/cfg/c30_phase_a5.yaml
src/kinoforge/cfg/c30_phase_a6.yaml
```

All five set `diagnostic_mode: True`, both reap predicates (`stall_reap_enabled`, `restart_loop_reap_enabled`) `False`, and use the same GPU tier as A1.

### Commit order

1. `c30_probe.py` + all unit tests in `tests/diagnostics/`. **No live spend.** Pre-commit clean.
2. `_c30_spend_ledger.json` with `{cumulative_usd: 0.0, entries: []}`. (Tracked from the start so the ledger lives in git.)
3. All 9 RED live test files committed as a single commit `test(c30): RED scaffold for fault-isolation tree`. Each test runs but immediately skips (predecessor sidecars absent) — except A1a, which is the tree root and proceeds to live spend.
4. A1a sidecar committed when its result lands (`test(c30): Phase A1a evidence — <verdict>`).
5. Iterate per phase result. One commit per phase. PROGRESS.md C30 entry updated with each new sidecar.

### Existing infrastructure reused

- S3 bucket `kinoforge-pod-diagnostics` (us-west-2, 7-day lifecycle on `boot-logs/`) from C28 Task A1.
- `kinoforge-c28-diag-put` IAM policy attached to `kinoforge-ci` from C28.
- C28 EXIT-trap renderer + boto3 default-chain wiring.
- `RunPodGraphQLClient` from `src/kinoforge/providers/runpod/graphql.py` (used by C25/C26/C27/C28).
- C29 boot-phase heartbeat ledger — irrelevant for A1 (no `kinoforge deploy`); matters for A2-A6 (operator can `kinoforge status` to confirm liveness during the 10 min window without polling logs).

### Files NOT touched

No source file in `src/kinoforge/orchestrator/`, `src/kinoforge/engines/`, `src/kinoforge/providers/runpod/` (other than read-only use of the existing `RunPodGraphQLClient`), or `src/kinoforge/core/` is modified by C30. C30 is read-only over the production code path.

## 6. Exit criterion and post-RCA branches

C30 is CLOSED when **all** of the following hold:

- All unit tests green: `pixi run test`.
- Pre-commit clean across all touched files.
- One decisive live probe sidecar committed; outcome matches exactly one hypothesis from §3.
- One inverse-control live probe sidecar committed; outcome flips the decisive probe's prediction the opposite way.
- `_c30_spend_ledger.json` shows cumulative spend ≤ $2.00 and committed alongside final sidecar.
- `PROGRESS.md` C30 entry rewritten with: named hypothesis, both sidecar paths, both S3 prefixes, total spend, follow-up phase identifier from the table below.
- `destroy_with_retry` confirmed: no leaked pod after `pytest` exits 0. Operator may double-check via direct GraphQL `myself.pods`.

### Post-RCA branch table

Each leaf of the fault tree maps to a specific follow-up phase. The C30 closing edit to PROGRESS.md MUST cite the row.

| RCA | Follow-up | Mitigation sketch |
|---|---|---|
| **H1** — RunPod port healthcheck on declared port w/o listener within ~30 s grace | **C32 — kinoforge port-listener preamble** | `render_provision` injects `python3 -m http.server ${declared_port} &` as the first command in the provision script (background, before clone). Optional cfg knob `cfg.early_port_listener: bool = True`. Lives in kinoforge, no Docker rebuild. ≤ 1 day. |
| **H2** — `runpod/pytorch:2.4.0` PID 1 quirk | **C28 Phase B operator push** | Already-built `kinoforge/wan-comfyui:v0.3.10-088128b2-cu124` (deferred-blocker per PROGRESS.md line 208) is the fix. C30 closing unblocks operator action to push Docker Hub; no new code. |
| **H3** — cgroup OOM-on-fork / disk-quota | **C33 — lifecycle-cfg memory probe** | Add `provider="runpod"` runtime detection of cgroup limits to `c30_probe.py`'s expanded form; ship cfg knob `cfg.runpod.min_ram_gb` enforcement before provision. Image prebake from H2 path is also a partial fix. |
| **H4** — Kijai / KJNodes custom-node import crashes PID 1 | **C34 — import-smoke in slim-mode image OR per-node pin downgrade** | Prebake image's build-time `import comfy` smoke (already in C28 Phase B Dockerfile) catches this at build time. If a specific node is the culprit, pin its commit hash in cfg. |
| **H5** — selfterm bootstrap edge case | **C35 — selfterm rewrite** | Replace inline `python3 -c "..." && nohup ...` with a small file-based bootstrap; add a unit test pinning the exact bash interpolation. |
| **H_clone / H_pip** — clone- or pip-phase OOM / network | **C28 Phase B** (prebake skips both) | Image prebake removes both surfaces from the live boot path; same operator-push unblock as H2. |
| **NO_REPRODUCTION_BUG_FLED** | none | Close C30. C29 boot-phase heartbeat remains standing protection. |

### Convergence observation

H2, H4 (partial), H_clone, and H_pip all converge on **C28 Phase B prebake** as the mitigation. Pushing the Phase B image is the dominant low-regret action regardless of which of those RCAs is confirmed. C30 closing should note this in PROGRESS.md when applicable.

### Failure modes that do NOT close C30

- All probes A1a → A6 produce SURVIVED. The loop never reproduces. Re-baseline by running C28 Phase A v5 cfg verbatim; if THAT no longer loops, close C30 with `outcome = NO_REPRODUCTION_BUG_FLED`. Live spend cap for re-baseline: $0.50.
- Every walk-down node restarts (impossible if A1a survives, but plausible if A1a→A2 boundary itself is the trigger). RCA is at the orchestrator boundary. Sub-investigation: bisect `render_provision`'s preamble. Tracked as in-flight C30 task, not a follow-up phase.
- Inverse-control flips the wrong way. Hypothesis falsified. Walk resumes from next node. Worst case: full tree walked without stable inverse → escalation to operator + RunPod support ticket; C30 partial-close with all evidence collected.

## 7. Testing strategy

### Unit tests (no live spend)

All under `tests/diagnostics/`. Red-then-green per test.

| Test file | Behaviors under test |
|---|---|
| `test_c30_classify_run.py` | `classify_run` returns `SURVIVED` only when fire_count == 0 AND uptime monotonically increases over window; `RESTARTED_N` for fire_count ≥ 3; `AMBIGUOUS` for 1–2. Edge cases: empty trail, single sample, uptime resets to 0 mid-window, poll API returns `None` for uptime. |
| `test_c30_count_trap_fires.py` | `count_trap_fires` lists S3 prefix correctly, ignores non-`diag-*.txt` keys, handles pagination, treats `NoSuchKey` as 0. |
| `test_c30_spend_ledger.py` | `assert_under_cap` raises when cumulative ≥ cap, accumulates across multiple appends, treats missing file as zero, refuses non-monotonic timestamps. |
| `test_c30_create_probe_pod_wire_shape.py` | Mock GraphQL client; assert `podFindAndDeployOnDemand` payload matches expected `{imageName, ports, dockerArgs, gpuTypeId, env}` for each of {A1a, A1b, A1c, A0'a}. Pins the exact bash one-liners. |
| `test_c30_pod_status_poller.py` | Poller emits one sample per `interval_s`, stops at window end, captures `lastStatusChange` and `runtime.uptimeSeconds`, gracefully handles `pod(podId)` returning null mid-window (pod destroyed externally). |
| `test_c30_destroy_with_retry.py` | First terminate succeeds-but-pod-persists → second terminate issued; success on first try → no retry; max attempts honoured; logs each attempt. |

Coverage target: 100% of `c30_probe.py` lines. Baseline `pixi run test` 2450 passed → expected 2470+ after C30 unit tests.

### Live tests (paid)

Pattern matches established C26/C27/C28/C29 sidecar shape. See §5 step list for the exact 8-step flow each live test follows.

### Acceptance gates

- ✓ Unit tests green.
- ✓ Pre-commit clean.
- ✓ Decisive + inverse live sidecars committed; verdicts consistent.
- ✓ Spend ledger ≤ $2.00.
- ✓ PROGRESS.md C30 entry rewritten with named hypothesis + follow-up phase identifier.
- ✓ No leaked pod after `pytest` exits 0.

### Regression guard

The touched-file set is entirely additive: a new `src/kinoforge/diagnostics/` subtree, new tests under `tests/diagnostics/` and `tests/live/`, new cfg files under `src/kinoforge/cfg/`. No file in `orchestrator/`, `engines/`, `providers/`, or `core/` is modified.

## 8. Open questions for the implementation plan

These are deferred to `writing-plans`, not blocking the spec:

1. Exact `cfg.provider.runpod.selfterm_enabled` knob — does it exist today? If not, the A2 inverse needs a one-line bypass via env var. Plan task to grep.
2. Exact `runtime.uptimeSeconds` accessor path on the RunPod GraphQL `pod(podId)` response — confirmed present from C26/C27 work but the exact field path needs pinning in `PodStatusPoller`.
3. C28's S3 trap pre-amble — currently lives in `render_provision`. C30's `create_probe_pod` wants it stand-alone for the bare-bash A1 cases. Either extract a `_render_c28_trap_preamble(run_id, bucket)` helper into a shared module or inline the string template in `c30_probe.py`. Plan decision.
4. `c30_probe.py` location — `src/kinoforge/diagnostics/` is new. Confirm no naming collision with anything elsewhere in the tree.

## 9. One-line summary

**Diagnose-only fault-isolation phase. Direct-GraphQL probes walk a null-first decision tree. Decisive + inverse evidence required to close. Zero production-code mutation. ≤ $2 spend cap. Output is a single named RCA pointing to a specific follow-up phase.**
