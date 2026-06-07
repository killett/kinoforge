# Layer W+α — Cloud Bootstrap (design)

**Status:** approved 2026-06-06.
**Predecessors:** Phase 31 (SkyPilot CPU lifecycle, GCP), Phase 38 / Layer W (S3 + GCS real-cloud verification).
**Successor (gated on this):** Layer W+β — SkyPilot multi-cloud T4 GPU smoke.
**Spend budget for this layer:** **$0.** Zero `sky launch`, zero EC2/GCE instances. Probes only.

---

## 1. Goal

Front-load every permission and quota the SkyPilot multi-cloud T4 smoke (Layer W+β)
will need, so the spend layer never stalls mid-run on an IAM denial or a quota=0.
Persist verification snapshots to gitignored workspace-local paths, matching the
existing `.aws/` / `.gcp/` pattern. Refuse to declare done if a gap remains.

The work is split between:

- **Operator-console actions** — IAM policy attachment, GCP quota increases that
  have no SDK surface. Gated by user confirmation; Claude provides exact text + URL.
- **SDK-driven verification** — Claude runs probes via boto3 + google-cloud SDKs
  and persists snapshots.

## 2. Out of scope

- **Azure / B2 / R2** — deferred to a later layer.
- **Any GPU spend** — no `sky launch`, no `sky exec`. `sky check` is offline metadata.
- **Engine deployment on GPU** (ComfyUI, Diffusers, Wan) — Layer W+β / later.
- **AWS bucket scope-down** — `AmazonS3FullAccess` → `kinoforge-realcloud-tests-*`
  scoped policy is already in `CLOUD-CREDS.md` follow-up, separate work.
- **Multipart knobs, encryption modes** — covered by Layer W, not revisited.

## 3. Files

| Path | Tracked | Purpose |
|---|---|---|
| `.aws/policies/skypilot-minimal.json` | tracked | Scoped IAM policy doc. Not secret. |
| `.aws/README.md` | tracked | Updated with policy-apply instructions. |
| `.aws/perms-snapshot.json` | gitignored | Output of probe — caller identity, simulated actions, quota. |
| `.gcp/perms-snapshot.json` | gitignored | Same for GCP. |
| `docs/CLOUD-CREDS.md` | tracked | Row + new "SkyPilot permissions" section + appendix. |
| `tools/cloud_perms_probe.py` | tracked | Idempotent probe; non-zero exit on gap. |
| `pixi.toml` | tracked | New `[tasks]` entry `cloud:perms-probe`. |
| `PROGRESS.md` | tracked | Phase 39 entry. |
| `README.md` | tracked | Brief pointer to bootstrap layer. |

## 4. Architecture

```
operator                                  Claude / pytest
   │                                            │
   │   T1: Claude writes policy JSON +          │
   │       apply instructions                   │
   │ ◄──────────────────────────────────────────┤
   │   T2: operator attaches policy in          │
   │       AWS IAM console                      │
   │ ──────────────────────────────────────────►│  USER GATE
   │                                            │
   │                                            │  T3–T4: probes
   │                                            │  (boto3 / google.cloud)
   │                                            │
   │   T5: AWS quota request via SDK            │
   │ ◄──────────────────────────────────────────┤
   │   T5: GCP quota request via console URL    │
   │ ──────────────────────────────────────────►│  USER GATE (GCP only)
   │                                            │
   │                                            │  T6: sky check
   │                                            │  (no spend)
   │                                            │
   │                                            │  T7: CLOUD-CREDS.md +
   │                                            │       PROGRESS + commit
```

## 5. Tasks

### T1 — AWS scoped IAM policy

Draft `.aws/policies/skypilot-minimal.json`. Tracked (policy bytes are not
secrets; the access keys are). Combines:

1. SkyPilot's documented minimal AWS policy (EC2 lifecycle, IAM PassRole for the
   role SkyPilot auto-creates, ServiceQuotas read/request).
2. S3 access scoped to `arn:aws:s3:::kinoforge-realcloud-tests-*` and
   `arn:aws:s3:::skypilot-*` (SkyPilot's auto-created bucket prefix).
3. KMS access scoped to the existing Layer W key
   (`alias/kinoforge-realcloud-tests`).

Source-of-truth reference for SkyPilot's minimum: SkyPilot docs
`/docs/reference/cloud-permissions/aws.html`. Pin the version captured at draft time.

### T2 — Apply policy + user gate

Update `.aws/README.md` with:

- Copy/paste path: IAM Console → Users → `kinoforge-ci` → Add permissions →
  Attach policies directly → Create policy → JSON → paste contents of
  `.aws/policies/skypilot-minimal.json` → name it `KinoforgeSkypilotMinimal`.
- Detach `AmazonS3FullAccess` after the new policy verifies clean (T3).

Claude pauses here. User confirms in chat that the policy is attached before T3 runs.

### T3 — AWS probe

`tools/cloud_perms_probe.py --cloud aws`. RED-first: unit tests with a fake boto3
client land before the live probe runs. Calls:

- `sts.get_caller_identity()` — must match `kinoforge-ci` ARN.
- `iam.simulate_principal_policy(PolicyInputList=[arn], ActionNames=[…])` for the
  union of:
  - `ec2:RunInstances`, `ec2:TerminateInstances`, `ec2:DescribeInstances`,
    `ec2:CreateTags`, `ec2:CreateVpc` (and friends),
  - `iam:PassRole`, `iam:CreateRole`, `iam:CreateInstanceProfile`,
  - `service-quotas:GetServiceQuota`, `service-quotas:RequestServiceQuotaIncrease`.
- `ec2.describe_instance_types(InstanceTypes=['g4dn.xlarge'])` — sanity that the
  shape exists in the region.
- `service-quotas.get_service_quota(ServiceCode='ec2', QuotaCode='L-DB2E81BA')`
  — Running On-Demand G and VT instances (vCPUs).

Writes `.aws/perms-snapshot.json`:

```json
{
  "captured_at": "2026-06-06T…-0700",
  "identity": {…},
  "simulated": {"ec2:RunInstances": "allowed", …},
  "quotas": {"L-DB2E81BA": {"value": 0, "code": "L-DB2E81BA", "name": "…"}}
}
```

Exit codes: 0 = all green. 1 = auth failure. 2 = quota gap pending (see T5).

### T4 — GCP probe

`tools/cloud_perms_probe.py --cloud gcp`. Symmetric to T3 using
`google.cloud.compute_v1` + `google.cloud.service_usage_v1`. Calls:

- `compute.RegionsClient.get(project, region='us-central1')` — auth + project.
- `compute.RegionsClient.list_quotas` — find `NVIDIA_T4_GPUS` quota in
  `us-central1`.
- SA roles audit via `google.cloud.resourcemanager` — verify `kinoforge-runner`
  has `roles/compute.instanceAdmin.v1` and `roles/iam.serviceAccountUser`. Grant
  programmatically if Layer W's `iam.securityAdmin` is still attached
  (see existing CLOUD-CREDS.md memory).

Writes `.gcp/perms-snapshot.json` with same shape as AWS snapshot. Exit codes same.

### T5 — Quota gap handler

- **AWS**: if `L-DB2E81BA < 4`, call
  `service-quotas.request_service_quota_increase(ServiceCode='ec2',
  QuotaCode='L-DB2E81BA', DesiredValue=4.0)`. Capture the
  `RequestedQuota.CaseId` to the snapshot. Re-runs are naturally idempotent —
  AWS returns the existing case if one is open.
- **GCP**: no SDK surface for compute quota requests. Probe emits a console URL
  template:
  `https://console.cloud.google.com/iam-admin/quotas?project={project}&filter=NVIDIA_T4_GPUS`
  and exits code 2 with operator-actionable instructions. The user acks
  completion in chat after the request is filed; rerun T4 to confirm.

### T6 — `sky check` smoke

Activate `live-skypilot` pixi env. Run `sky check aws gcp` and capture full
stdout/stderr. **No spend.** `sky check` only reads metadata. Append output to
`docs/CLOUD-CREDS.md` "SkyPilot perms" appendix.

If `sky check` reports a cloud as unhealthy, layer α is not done — fix the
upstream gap and re-run.

### T7 — Docs + commit

- `docs/CLOUD-CREDS.md` table: add row "SkyPilot perms (AWS)" + "SkyPilot perms
  (GCP)" with status indicators tied to snapshot files.
- New `## SkyPilot permissions` section: scope summary + rotation note (quotas
  don't expire; policy bytes versioned in repo).
- `PROGRESS.md` Phase 39 entry: per-task SHAs + design decisions + snapshot
  excerpts.
- `README.md`: one-paragraph pointer.

## 6. Error handling + reliability

- Probe is one-shot, no retries — boto3/google-cloud SDKs do their own retries.
- Re-running the probe overwrites the snapshot in place — committed snapshots
  reflect the most recent green run, never a partial one (write to a temp file +
  rename).
- Quota request idempotency: AWS returns the open case; GCP is operator-driven.
- All probes honor the `[activation.env]` discovery chains — no `--profile`,
  no `--key-file` flags.

## 7. Testing strategy

- **Unit tests** (`tests/tools/test_cloud_perms_probe.py`): inject fake boto3 +
  fake google-cloud clients. Each AWS/GCP call gated by a single assertion against
  a recorded fixture. RED before any live call.
- **Live probe**: run once per cloud by Claude; snapshot file is the artifact.
  No live test in CI.
- `sky check` output: committed to docs, not asserted in tests (output format is
  not a kinoforge contract).

## 8. Done criteria

- `pixi run cloud:perms-probe` exits 0 on **both** clouds.
- `.aws/perms-snapshot.json` and `.gcp/perms-snapshot.json` both committed
  (gitignored at the path level; the files exist locally and are not lost on
  container restart).
- `sky check aws gcp` reports both clouds green.
- AWS quota `L-DB2E81BA` ≥ 4 vCPUs in `us-east-1`.
- GCP quota `NVIDIA_T4_GPUS` ≥ 1 in `us-central1`.
- `docs/CLOUD-CREDS.md` SkyPilot rows ✅ on both clouds.
- `PROGRESS.md` Phase 39 entry committed with per-task SHAs.

## 9. Open follow-ups (carried forward — not blockers)

- AWS bucket-scope-down on `AmazonS3FullAccess` — separate work, predates this layer.
- Azure / B2 / R2 SkyPilot enablement — future layer.
- GPU engine smoke on a real SkyPilot pod — Layer W+β + downstream.
- Cross-cloud cost estimate via `sky launch --dryrun` — nice to have, not
  required here.
