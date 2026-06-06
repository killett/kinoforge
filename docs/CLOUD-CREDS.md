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
| AWS          | ✅ Bootstrapped               | `.aws/credentials`, `.aws/config`                   | `kinoforge-ci` (account `<AWS_ACCOUNT>`, policy `AmazonS3FullAccess`) |
| fal.ai       | ✅ Bootstrapped               | `.env` → `FAL_KEY`                                  | personal fal API key                                                 |
| HuggingFace  | ✅ Bootstrapped               | `.env` → `HF_TOKEN`                                 | personal HF read-only token                                          |
| CivitAI      | ✅ Bootstrapped               | `.env` → `CIVITAI_TOKEN`                            | personal CivitAI API key                                             |
| RunPod       | ✅ Bootstrapped               | `.env` → `RUNPOD_API_KEY`, `RUNPOD_TERMINATE_KEY`   | personal RunPod API key (terminate-key reuses main; see Layer N)    |
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

### Scope-down follow-up (operator action recommended)

`AmazonS3FullAccess` is broader than required. Once the layer is shipped,
operator should swap it for the scoped policy in `.aws/README.md` (limits
the `kinoforge-ci` key to the `kinoforge-realcloud-tests-*` bucket prefix).
This requires IAM perms that the `kinoforge-ci` key does NOT itself hold;
the swap is done in the AWS Console.

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
