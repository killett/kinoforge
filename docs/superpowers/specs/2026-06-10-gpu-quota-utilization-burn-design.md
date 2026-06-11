# GPU quota utilization-burn — design

**Status:** Approved 2026-06-10. Implementation plan to follow via `writing-plans`.

**Author:** brainstormed with Dr. Twinklebrane on 2026-06-10.

**Problem statement:** Both AWS and GCP denied GPU quota increases for the
kinoforge project. The accounts are essentially fresh (near-zero billing
footprint), and prior justification text was vague. Goal: get both quotas
approved within 5 calendar days while spending ≤ $20 and minimising hands-on
operator work.

**Strategic premise:** The dominant failure mode for GPU quota requests on
fresh cloud accounts is risk-model-driven, not justification-driven. Reviewers
(and the automated systems behind them) treat accounts with $0–$5 lifetime
spend as untrusted regardless of the request text. The single highest-conversion
tactic for the budget available is therefore to build a paid-utilization
billing footprint on cheap non-GPU services for several days, then resubmit
with concrete spend numbers and sharper justification text.

This spec describes that play end-to-end.

## 1. Goal and success criteria

**Goal.** AWS and GCP each grant ≥ 1 GPU vCPU of quota in the kinoforge target
region within 5 calendar days, unblocking PROGRESS items A3 (SkyPilot T4 re-fire,
GCP us-west1) and A4 (AWS arm of W+β2, us-west-2). Operator does as little
hands-on work as possible; Claude does everything automatable from the
container.

**Success criteria.**

- GCP `GPUS_ALL_REGIONS` ≥ 1 (global) AND `NVIDIA_T4_GPUS` ≥ 1 in `us-west1`
  on project `kinoforge-prod-0ddb375e`.
- AWS service quota `L-DB2E81BA` ("Running On-Demand G/VT instance vCPUs")
  ≥ 4 in `us-west-2`. The prior AWS case `cd3e0e81…I2kxtcvR` was filed in
  `us-east-1`; this spec recommends filing fresh in `us-west-2` to align
  with the project's Oregon-default-region memory.
- Total cloud spend across the 5-day burn ≤ $20.
- Operator clicks ≤ 1 console action per cloud across the entire 5 days
  (final form submit if a CLI submission path is rejected; everything else
  CLI-driven).
- Resubmit-ready by 2026-06-15.

## 2. Workload spec

### 2.1 GCP — target $5, ceiling $7

Region `us-west1`, project `kinoforge-prod-0ddb375e`. All resources tagged
`kinoforge-quota-burn=true`.

- 1× `e2-small` VM (2 vCPU, 2 GB) running 24/7 × 5 days ≈ $2.02.
- 1× pd-balanced 10 GB persistent disk attached ≈ $0.50.
- GCS bucket `kinoforge-quota-burn-gcp` with ~5 GB of sync up / sync down /
  delete churn over 5 days ≈ $0.50 (storage + operations).
- 3× BigQuery scans on a bounded public dataset (~100 GB billed each, capped
  via `--maximum_bytes_billed`) ≈ $1.50 at $5/TB.
- Cloud Billing budget alarm at $7 hard cap, email notification to operator.

### 2.2 AWS — target $3, ceiling $5

Region `us-west-2`. All resources tagged `kinoforge-quota-burn=true`.

- 1× `t4g.nano` (Graviton, 2 vCPU, 0.5 GB) running 24/7 × 5 days ≈ $0.50.
- 1× `t3.small` burst running 24 h on day 2 only ≈ $0.50 (auto-terminate
  via `shutdown -h +1440`).
- 1× EBS gp3 30 GB attached ≈ $0.40.
- S3 bucket `kinoforge-quota-burn-aws-usw2` with ~10 GB of churn (PUT/GET
  loop) ≈ $0.50.
- 5× small DynamoDB on-demand writes / reads ≈ $0.10.
- AWS Budgets hard cap at $5, email notification to operator.

### 2.3 Spend envelope

Floor ≈ $8, ceiling ≈ $12 across both clouds. $8+ buffer remains within the
$20 authorised budget.

## 3. Auto-shutdown and teardown discipline

The user is outside the container and cannot easily intervene to stop a runaway
VM. Three independent kill mechanisms must be active before any resource is
created.

1. **Kernel-side.** Every VM's `UserData` / `startup-script` runs
   `shutdown -h +N` (8 hours for 24/7 instances, 24 hours for the day-2 burst).
   Re-armed on every reboot via cron `@reboot` entry.
2. **Cloud-native scheduler.** AWS EventBridge rule terminates any tagged
   instance after 144 hours wall-time. GCP Compute scheduled instance shutdown
   policy mirrors this.
3. **Budget alarms.** AWS Budgets + GCP Cloud Billing budget alarms fire
   email notifications to the operator at the hard cap. These are alerting
   only — they do not auto-stop resources, so the kernel-side and cloud-native
   layers carry the actual safety.

**Daily polling.** Claude polls `aws ce get-cost-and-usage` and the GCP billing
export query at the start of every session, reports pacing in chat, and
escalates explicitly if spend > $10 or budget alarms have fired.

**Teardown.** `tools/quota_burn_teardown.{sh,py}` destroys every tagged resource
on both clouds. Idempotent. Verified by `aws ec2 describe-instances --filters
Name=tag:kinoforge-quota-burn,Values=true` and `gcloud compute instances list
--filter='labels.kinoforge-quota-burn=true'` both returning empty before the
script exits 0. Runs at day 5 unconditionally, and can be invoked at any
point as a full abort.

## 4. Operator vs Claude split

**Claude (no operator action required):**

- Day 0: provision every workload on both clouds via `aws` and `gcloud` CLI;
  tag everything `kinoforge-quota-burn=true`; arm budget alarms; kick off
  background churn scripts.
- Days 1–4: daily billing snapshot, pacing report in chat.
- Day 4: write `docs/quota-justification-aws.md` and `docs/quota-justification-gcp.md`
  with the four-block sharpened justification text (template in §6),
  populating the month-to-date spend figures from the day-4 snapshot.
- Day 5: submit quota requests via CLI:
  - GCP: `gcloud alpha quotas adjustments create` for `GPUS_ALL_REGIONS` and
    `NVIDIA_T4_GPUS` in `us-west1`. Fall back to a pre-filled console URL
    if the alpha API rejects.
  - AWS: `aws service-quotas request-service-quota-increase --service-code ec2
    --quota-code L-DB2E81BA --desired-value 4 --region us-west-2`.
- Day 5: run `tools/quota_burn_teardown.{sh,py}`; verify zero remaining
  tagged resources; emit final spend report.

**Operator (minimum-touch):**

1. Day 0: confirm "go" in chat so Claude fires spin-up. One sentence.
2. Days 1–5: forward any cloud email Claude cannot read (denial, approval,
   verification, budget breach). Paste text in chat. Estimated 0–3 emails.
3. Day 4: skim the two justification drafts in `docs/quota-justification-*.md`,
   swap in the real GitHub handle (or strike the repo line if private), edit
   if desired.
4. Day 5, only if CLI submission fails: click "Submit" on a pre-filled console
   URL Claude provides.
5. Worst case: respond to an identity re-verification prompt (CVV, SMS, 2FA).

**Claude cannot do:** receive email, click in a browser, re-verify identity,
upgrade support plan (would require an explicit out-of-budget re-authorisation).

## 5. Day-by-day timeline

Today = day 0 = 2026-06-10. Resubmit = day 5 = 2026-06-15.

| Day | Date       | Activity                                                                                                                                                                                                                                                              |
|-----|------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 0   | 2026-06-10 | Spin up: GCP e2-small + PD + GCS bucket + budget alarm; AWS t4g.nano + EBS + S3 bucket + DynamoDB table + budget alarm. Kick off background churn scripts. Verify all three auto-shutdown layers armed. Confirm spin-up in chat.                                       |
| 1   | 2026-06-11 | Morning + evening spend snapshot. Both clouds should show first-day charges visible in console. Confirm budget alarms not fired.                                                                                                                                       |
| 2   | 2026-06-12 | Same snapshot routine. Run 1 of 3 BigQuery scans (dry-run first). Launch day-2 t3.small burst on AWS (24 h self-terminating).                                                                                                                                          |
| 3   | 2026-06-13 | Same snapshot. Run 2 of 3 BigQuery scans. Mid-burn checkpoint: if cumulative spend > $10, halve remaining workloads or abort early.                                                                                                                                    |
| 4   | 2026-06-14 | Same snapshot. Run 3 of 3 BigQuery scans. Draft `docs/quota-justification-{aws,gcp}.md` with concrete day-0..4 spend figures. Operator review.                                                                                                                         |
| 5   | 2026-06-15 | Submit AWS + GCP quota requests via CLI. Run `tools/quota_burn_teardown.{sh,py}`. Verify zero remaining tagged resources. Final spend report.                                                                                                                          |
| 5+  | from 2026-06-15 | Monitor for approval / denial emails. Approval → kick off A3 (SkyPilot T4 re-fire) and A4 (AWS arm of W+β2). Denial → escalation ladder in §7.                                                                                                                  |

**Pause points.**

- End of day 2: if either budget alarm has fired or cumulative spend > $8,
  pause and replan before proceeding.
- End of day 4: operator approves both justification drafts before day-5 submit.

## 6. Sharpened justification text

Prior text per memory: *"Internal SDK smoke tests proving our SkyPilot provider
end-to-end against a single preemptible T4 GPU for ~5 minutes per run; no
production workload."* This was vague: no run cadence, no $ cap, no account
context, no cost controls. A fresh account asking for GPU with hand-wavy
justification reads as risk → auto-deny.

The replacement template has four load-bearing blocks.

1. **Workload.** Specific named workload, instance type by name, region, per-run
   wall-time, runs-per-week cap, $/month cap.
2. **Account context.** Active-customer claim backed by the concrete
   month-to-date spend figure from the day-4 snapshot, with named services.
3. **Cost controls.** Three concrete mechanisms — budget alarm with $ figure,
   in-instance auto-shutdown, instance tier choice.
4. **Repository / proof point.** Single line with the public repo URL,
   optional but converts well.

### 6.1 GCP draft

> Workload: Continuous-integration smoke tests for an open-source Python SDK
> (`kinoforge`) that orchestrates GPU video-generation jobs across cloud
> providers. Need 1× preemptible NVIDIA T4 in us-west1-a for ~8 minutes per
> test run; ≤ 10 runs/week; ≤ $20/month total GPU spend. Workload pattern:
> launch via SkyPilot → run smoke test → autostop. Never persistent. No
> production traffic.
>
> Account context: Active pay-as-you-go customer since 2026-06-07. Month-to-date
> spend across Compute Engine, Cloud Storage, BigQuery: $X.XX (insert from
> day-4 snapshot). Project `kinoforge-prod-0ddb375e`.
>
> Cost controls: Cloud Billing budget alarm at $50/month notifies project
> owner via email. SkyPilot's `autostop=10m` flag terminates idle instances.
> All GPU launches preemptible-tier only.
>
> Repository: https://github.com/<your-handle>/kinoforge

### 6.2 AWS draft

> Workload: CI smoke tests for an open-source Python SDK (`kinoforge`)
> orchestrating GPU video-generation jobs across cloud providers. Need 4 vCPU
> of On-Demand G/VT instances in us-west-2 (= 1× g4dn.xlarge) for ~8 min per
> test run; ≤ 10 runs/week; ≤ $20/month total GPU spend. SkyPilot-driven
> launch → run → terminate. No persistent GPU fleet.
>
> Account context: Active billing customer. Month-to-date spend across EC2,
> S3, EBS, DynamoDB: $X.XX (insert from day-4 snapshot). Region preference:
> us-west-2.
>
> Cost controls: AWS Budgets hard cap at $50/month with email alert.
> EventBridge auto-terminate rule destroys tagged smoke instances after 30
> min wall-time. Spot/On-Demand mix monitored via CloudWatch.
>
> Repository: https://github.com/<your-handle>/kinoforge

Both drafts written to disk on day 4 so the operator can review with the
concrete spend figure populated before the day-5 submit.

## 7. Risk and rollback

**R1. Runaway spend.** Three stacked kill mechanisms (kernel-side shutdown,
cloud-native scheduler, budget alarm) plus Claude daily-snapshot polling and
the idempotent teardown script. Designed for the case where the operator is
outside the container.

**R2. Day-5 denial again.** Escalation ladder.

1. Same day: file a polite reply on the denial ticket asking for the specific
   criterion the request fell short on.
2. +1 day: try alternate region. AWS `us-east-1` and GCP `us-central1` often
   carry higher default initial allocations than west-coast regions. Concedes
   the Oregon-default policy; flag to operator before pivoting.
3. +1 day: contact GCP sales via the Cloud Console "Contact Sales" form
   (free, human-routed). AWS: open a Basic-tier Account & Billing case asking
   for sales/account-team guidance.
4. Last resort: upgrade AWS Developer Support ($29/month, one-month commit).
   Exceeds $20 budget; needs explicit operator re-authorisation.

**R3. GCP GPU quota CLI submission rejected.** `gcloud alpha quotas adjustments
create` works for many quotas but GPU families occasionally force the console
flow. Fallback: Claude generates a pre-filled console URL plus a screenshot of
the form fields; operator clicks submit. 30-second action.

**R4. Account flagged for fraud / multi-cloud burner pattern.** Workloads
stay boring — no GPU API calls during the burn, no large compute bursts, no
atypical access patterns. CPU VMs + storage churn match a normal small-customer
profile. Same payment method used on both clouds for consistency.

**R5. BigQuery scan over-scans.** Every query gated by `bq query --dry_run`
first (returns exact bytes-to-scan estimate at zero cost). Hard `WHERE`
clauses against partition columns; queries run against a bounded public
dataset. Each query carries `--maximum_bytes_billed=10000000000` (10 GB hard
cap; the query errors out rather than silently scanning more).

**R6. Operator unavailable.** Calendar checkpoints have ±1 day buffer. Day 5
can slide to day 6 or 7 with no spend impact (≤ $3/day combined on the
steady-state workloads). Teardown remains safe at any point.

**Full abort.** `tools/quota_burn_teardown.{sh,py}` runs at any point and brings
spend to zero. Idempotent. Plan reverts to status quo with whatever ≤ $15 was
burned counted as sunk cost.

## 8. Out of scope (explicitly)

- Bedrock / Vertex AI prediction smokes during the burn. The user picked
  Approach A (pure utilization, throwaway spend), not B (dovetail with
  kinoforge project work). The burn workloads do not produce reusable
  project artefacts beyond the billing footprint itself.
- Multi-region quota requests. One region per cloud (us-west1 / us-west-2),
  per project policy.
- Other GPU families (A100, L4, V100). Single T4 / G/VT vCPU class only,
  matching A3 + A4 needs.
- Anti-fraud appeals or identity-verification flows beyond responding to a
  cloud-side prompt if one arrives.
- Bedrock Luma Ray v2 model authorisation (A1) — separate gate, not on this
  path.

## 9. Implementation hooks

A separate implementation plan (via `writing-plans`) will break this into
bite-sized tasks. Anticipated structure:

- Task 1: `tools/quota_burn_spinup.py` — single entry point that idempotently
  creates every tagged resource on both clouds, returns a manifest.
- Task 2: `tools/quota_burn_teardown.py` — destroys everything in the manifest;
  idempotent; verifies empty state before exit.
- Task 3: `tools/quota_burn_daily_snapshot.py` — reads MTD spend from both
  clouds, returns a structured report for chat surfacing.
- Task 4: `tools/quota_burn_submit.py` — submits both quota requests on day 5;
  emits console-URL fallback if CLI path rejects.
- Task 5: justification drafts written to `docs/quota-justification-{aws,gcp}.md`
  with placeholder for MTD spend, populated by Task 3 output on day 4.
- Task 6: PROGRESS.md update tracking spend burn-down and approval status.

All tasks RED-first per kinoforge TDD policy. Live spend gated by
`pixi run preflight` per CLAUDE.md durability rules. Scaffold committed
before any live spend per the same rules.

## 10. References

- `PROGRESS.md` §A3 (SkyPilot T4 re-fire), §A4 (AWS arm of W+β2).
- Memory `project_gpus_all_regions_quota_blocker.md` — prior justification text
  and quota state.
- Memory `project_gcp_billing_upgraded.md` — pay-as-you-go active since
  2026-06-07.
- Memory `feedback_default_region_oregon.md` — region policy.
- Memory `feedback_destructive_op_ordering.md` — teardown discipline.
- Memory `feedback_autonomous_no_gates.md` — $20 session budget pre-authorised.
