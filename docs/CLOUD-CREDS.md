# Cloud credentials — inventory & bootstrap ledger

This file is the tracked inventory of every cloud credential kinoforge depends
on. It contains NO secrets — only:

- where each credential lives (always under a gitignored workspace path),
- which identity it represents (account / SA / IAM user),
- which roles or policies it carries,
- how to bootstrap a new workspace from scratch,
- how to rotate when keys age out.

The actual secrets are in `.env`, `.gcp/`, and `.aws/`, all gitignored. Their
local-only `README.md` files contain operator-facing detail.

## Why front-load this

Every real-cloud layer (Layer N for RunPod, Phase 31 for SkyPilot, the
upcoming S3/GCS verification layer, future Azure/B2/R2 stores) needs working
credentials before any test can be written. Front-loading credential setup —
once per provider, persisted to gitignored workspace paths — avoids:

- Mid-layer interruption while operator generates a key.
- Operator having to paste secrets into chat (they go into files instead).
- Secrets being re-requested every container restart (workspace paths persist).

The pattern: gitignored `<provider>/` directory + tracked entry in this file.

## Bootstrap status

| Provider     | Status                       | Files                                               | Identity                                                            |
|--------------|------------------------------|-----------------------------------------------------|---------------------------------------------------------------------|
| GCP          | ✅ Bootstrapped               | `.gcp/kinoforge-sa.json`, `.gcp/gcloud-config/`     | `kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com`     |
| AWS          | ✅ Bootstrapped               | `.aws/credentials`, `.aws/config`                   | `kinoforge-ci` (account `<AWS_ACCOUNT>`)                              |
| fal.ai       | ✅ Bootstrapped               | `.env` → `FAL_KEY`                                  | personal fal API key                                                 |
| HuggingFace  | ✅ Bootstrapped               | `.env` → `HF_TOKEN`                                 | personal HF read-only token                                          |
| CivitAI      | ✅ Bootstrapped               | `.env` → `CIVITAI_TOKEN`                            | personal CivitAI API key                                             |
| RunPod       | ✅ Bootstrapped               | `.env` → `RUNPOD_API_KEY`, `RUNPOD_TERMINATE_KEY`   | personal RunPod API key (terminate-key reuses main; see Layer N)    |
| SkyPilot perms (AWS) | ✅ Bootstrapped (Layer W+α) | `.aws/policies/skypilot-minimal.json`, `.aws/perms-snapshot.json` | `kinoforge-ci` + managed policies (EC2/IAM/SQ/S3FullAccess) + `kinoforge-ci-kms` |
| SkyPilot perms (GCP) | ✅ Bootstrapped (Layer W+α) | `.gcp/perms-snapshot.json`                          | `kinoforge-runner` + `compute.instanceAdmin.v1` + `iam.serviceAccountUser` |
| Azure / B2 / R2 | ❌ Not bootstrapped        | —                                                   | —                                                                    |

> Layer W encryption + signed-URL axes require KMS keys. Run
> `pixi run cloud:bootstrap-kms` after the AWS / GCP rows show ✅.
> ARN and resource name are persisted to `.aws/kms-test-key.arn` and
> `.gcp/kms-test-key.name` (both gitignored).

## Discovery — how credentials reach code

All three "default chain" SDKs honour these env vars, set in
`pixi.toml` → `[activation.env]`:

```
GOOGLE_APPLICATION_CREDENTIALS = $PIXI_PROJECT_ROOT/.gcp/kinoforge-sa.json
CLOUDSDK_CONFIG                = $PIXI_PROJECT_ROOT/.gcp/gcloud-config
AWS_SHARED_CREDENTIALS_FILE    = $PIXI_PROJECT_ROOT/.aws/credentials
AWS_CONFIG_FILE                = $PIXI_PROJECT_ROOT/.aws/config
```

Provider-specific REST API keys (fal/HF/CivitAI/RunPod) live in `.env` and
are loaded at CLI entry by the Phase 14 dotenv shim (see PROGRESS:194).

`pixi run X` activates `[activation.env]`, so any subprocess Claude or pytest
spawns inherits the discovery hints. No per-test plumbing required —
`EnvCredentialProvider` (PROGRESS:25) for `.env`-style keys, default chains
for AWS/GCS.

## GCP — provisioning history

- 2026-06-03: service account `kinoforge-runner` created in project
  `<GCP_PROJECT>`. Key file persisted to `/workspace/.gcp/kinoforge-sa.json`.
  Roles granted: see `.gcp/README.md`.
- 2026-06-06: bucket `gs://<GCS_BUCKET>` created in
  `us-central1` for the S3/GCS real-cloud verification layer.
  Uniform bucket-level access ON. Public access prevention ENFORCED.
  Lifecycle: delete objects + abort multipart at age 1 day.
- 2026-06-06 (Layer W T5 bootstrap): GCP Cloud KMS keyring
  `kinoforge-realcloud-tests` + key `bucket-cmek` created in `us-central1`.
  `kinoforge-runner` SA + GCS service agent
  (`service-<GCP_PROJECT_NUMBER>@gs-project-accounts.iam.gserviceaccount.com`)
  granted `roles/cloudkms.cryptoKeyEncrypterDecrypter`. Key resource name
  persisted to `.gcp/kms-test-key.name` (gitignored).
  Rotation: NOT auto-rotated — rotation invalidates Layer W recorded fixtures.

## AWS — provisioning history

- 2026-06-06: IAM user `kinoforge-ci` created by operator in account
  `<AWS_ACCOUNT>`. Policy `AmazonS3FullAccess` attached. Access key pasted
  directly into `/workspace/.aws/credentials` (gitignored).
- 2026-06-06: bucket `s3://<S3_BUCKET>` created in
  `us-east-1`. Public access block enforced (all 4 flags). Lifecycle: object
  expiration + abort-incomplete-multipart, both at age 1 day.
- 2026-06-06: end-to-end S3 smoke (boto3 default chain + bucket
  put/get/delete) verified clean.
- 2026-06-06 (Layer W T5 bootstrap): AWS KMS key `alias/kinoforge-realcloud-tests`
  created in `us-east-1`. ARN persisted to `.aws/kms-test-key.arn` (gitignored).
  Key policy grants `kinoforge-ci` `kms:Encrypt`, `kms:Decrypt`,
  `kms:GenerateDataKey`, `kms:DescribeKey`. Root account retains `kms:*`.
  Rotation: NOT auto-rotated — rotation invalidates Layer W recorded fixtures.

### Layer 3 (Nova Reel) — IAM policies

| Policy name | Grants | Attachment | Date | Source |
|---|---|---|---|---|
| `kinoforge-nova-reel` | Bedrock InvokeModel + StartAsyncInvoke + GetAsyncInvoke (Nova Reel 1.1 ARN) + ListFoundationModels (`*`) + S3 read/write on `kinoforge-nova-reel-output` | Inline on `kinoforge-ci` | 2026-06-07 | `.aws/policies/bedrock-nova-reel.json` |

Reversible: `aws iam delete-user-policy --user-name kinoforge-ci --policy-name kinoforge-nova-reel`

### Layer 3 (Nova Reel) — S3 buckets

| Bucket | Purpose | Region | Date | Notes |
|---|---|---|---|---|
| `kinoforge-nova-reel-output` | Nova Reel async-invoke output prefix | `us-east-1` | 2026-06-07 | Created by Layer 3 Task 4; Bedrock writes `{invocation_id}/output.mp4` here |

Reversible: `aws s3 rb s3://kinoforge-nova-reel-output --force`

### Scope-down follow-up (operator action recommended)

`AmazonS3FullAccess` is broader than required. Once the layer is shipped,
operator should swap it for the scoped policy in `.aws/README.md` (limits
the `kinoforge-ci` key to the `kinoforge-realcloud-tests-*` bucket prefix).
This requires IAM perms that the `kinoforge-ci` key does NOT itself hold;
the swap is done in the AWS Console.

## SkyPilot permissions (Layer W+α)

Front-load layer that landed every AWS + GCP permission and GPU quota
needed for the SkyPilot multi-cloud T4 smoke (Layer W+β). Spec:
`docs/superpowers/specs/2026-06-06-layer-w-alpha-cloud-bootstrap-design.md`.
Plan: `docs/superpowers/plans/2026-06-06-layer-w-alpha-cloud-bootstrap.md`.

- **AWS scoped policy doc:** `.aws/policies/skypilot-minimal.json` (tracked,
  not secret). Covers EC2 lifecycle + IAM PassRole on `skypilot-*` +
  ServiceQuotas + S3 scoped to `kinoforge-realcloud-tests-*`/`skypilot-*`
  prefixes + KMS scoped to `alias/kinoforge-realcloud-tests`. NOT attached
  to `kinoforge-ci` in this layer (operator opted for AWS-managed broad
  policies instead — see "AWS — actually attached policies" below). The
  doc stays in repo as the scope-down target for a future layer.
- **AWS — actually attached policies:** `AmazonEC2FullAccess` +
  `IAMFullAccess` + `AmazonS3FullAccess` + `ServiceQuotasFullAccess`
  (AWS managed) + `kinoforge-ci-kms` (customer-managed, scoped to the
  Layer W KMS key ARN — auto-created by the probe when `kms:Encrypt`
  simulated as `implicitDeny`). The four managed policies are broader than
  required; the scoped `.aws/policies/skypilot-minimal.json` is the
  documented swap-in target.
- **AWS GPU quota:** `L-DB2E81BA` (Running On-Demand G/VT instance vCPUs)
  ≥ 4 in `us-east-1`. Initial value was 0; probe auto-submitted case
  `cd3e0e81b66b4055bcc189bbf8653542I2kxtcvR` via
  `service-quotas.RequestServiceQuotaIncrease`. AWS reviews
  asynchronously; status visible in the AWS Service Quotas console.
- **GCP SA roles:** `kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com`
  holds `roles/compute.instanceAdmin.v1` + `roles/iam.serviceAccountUser`
  (plus several superset roles from earlier layers). Probe grants the
  required roles programmatically if missing (SA already holds
  `roles/iam.securityAdmin`).
- **GCP GPU quota:** `NVIDIA_T4_GPUS` ≥ 1 in `us-central1`. Already at
  target before the probe ran; no console action required.

Re-run with `pixi run cloud:perms-probe`. Snapshots written atomically
via temp-file + rename so a crashed probe never leaves a half-write.
Exit codes: 0 green; 1 auth failure or required action denied; 2 quota
gap pending (AWS auto-submits; GCP emits console URL — neither happened
on the green run).

## Rotation policy

| Credential                           | Recommended cadence                  | Owner    |
|--------------------------------------|--------------------------------------|----------|
| GCP SA key                           | 90 days                              | operator |
| AWS access key                       | 90 days                              | operator |
| fal / HF / CivitAI / RunPod tokens   | as provider expires                  | operator |
| KMS keys (S3 + GCS Layer W)          | defer until next real-cloud layer    | operator |

Rotation steps live in each provider's local README (`.gcp/README.md`,
`.aws/README.md`). Update this file's table when rotation completes.

## Adding a new provider

1. Pick a workspace-local path: `.<provider>/` (gitignored).
2. Add the path to `.gitignore` if not covered by an existing pattern.
3. Place credential files inside.
4. Set the SDK's default-chain env var in `pixi.toml` → `[activation.env]`.
5. Add a row to the bootstrap-status table above.
6. Write a `.<provider>/README.md` with bootstrap + rotation instructions.
7. Reference the new provider from the layer's spec/plan that introduced it.

## SkyPilot check — captured output (Layer W+α T6)

Layer W+α verified permissions via two channels:

1. `pixi run cloud:perms-probe` — covers AWS + GCP. Identity + IAM action
   simulation (AWS), SA role audit (GCP), GPU quota readouts on both.
2. `pixi run -e live-skypilot sky check gcp` — SkyPilot's own credential
   check, captured below.

```text
✓ SkyPilot API server started.
Checking credentials to enable infra for SkyPilot.
  GCP: enabled [compute, storage]

🎉 Enabled infra 🎉
  GCP [compute, storage]
```

Captured 2026-06-06. Zero EC2 instances in `us-east-1`, zero GCE instances
in `us-central1` at the time of capture (verified via
`aws ec2 describe-instances` and `gcloud compute instances list`).

**Why no `sky check aws`:** the `live-skypilot` pixi feature pins
`skypilot[gcp]` only. Adding `extras=["aws", "gcp"]` triggers a conda↔PyPI
pin conflict (conda-pinned `botocore`/`urllib3`/`grpcio`/`protobuf` vs
`skypilot[aws]`'s upstream constraints, gated further by the workspace's
`exclude-newer = "7d"`). The AWS permission surface is already covered
end-to-end by `tools/cloud_perms_probe.py --cloud aws` (identity +
`iam.simulate_principal_policy` for every required action +
`ec2.describe_instance_types` + `service-quotas.get_service_quota`).
`sky check aws` adds nothing the probe does not already cover for this
zero-spend layer; the real validation happens at Layer W+β's first
`sky launch`. Resolving the pin conflict is a Layer W+β prerequisite.

### Layer W+β attempt — blocked on GCP billing

Captured 2026-06-06: attempting the live T4 lifecycle surfaced GCP's
free-tier restriction:

> Your billing account is currently in the free tier where non-TPU
> accelerators are not available.

Per-region quota (`NVIDIA_T4_GPUS=1` in `us-central1`) is pre-granted
but only activates after the billing account is upgraded. The
`GPUS_ALL_REGIONS=0` global quota is a free-tier consequence, not a
separately adjustable quota.

**Operator action to unblock:** upgrade billing at
<https://cloud.google.com/free/docs/gcp-free-tier#how-to-upgrade>. No
immediate spend obligation — only future VMs are billed at standard
rates.

Five adapter/test bug fixes shipped on the discovery path
(`ee90ac3`, `c9a5aa6`, `f0c7783`, `819d130`, `f3ade88`); see
`PROGRESS.md` Phase 40 for details. Live smoke re-fires for ~$0.05
once billing is upgraded.
