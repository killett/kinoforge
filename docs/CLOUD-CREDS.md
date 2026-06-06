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
| AWS          | ⏳ Awaiting user bootstrap    | `.aws/credentials`, `.aws/config`                   | `kinoforge-ci` (to be created by user; see `.aws/README.md`)        |
| fal.ai       | ✅ Bootstrapped               | `.env` → `FAL_KEY`                                  | personal fal API key                                                 |
| HuggingFace  | ✅ Bootstrapped               | `.env` → `HF_TOKEN`                                 | personal HF read-only token                                          |
| CivitAI      | ✅ Bootstrapped               | `.env` → `CIVITAI_TOKEN`                            | personal CivitAI API key                                             |
| RunPod       | ✅ Bootstrapped               | `.env` → `RUNPOD_API_KEY`, `RUNPOD_TERMINATE_KEY`   | personal RunPod API key (terminate-key reuses main; see Layer N)    |
| Azure / B2 / R2 | ❌ Not bootstrapped        | —                                                   | —                                                                    |

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

## AWS — bootstrap protocol (one-time, user-side)

See `.aws/README.md` for the 6-step bootstrap. Summary:

1. User creates IAM user `kinoforge-ci` in AWS Console with `AmazonS3FullAccess`.
2. User creates access key (CLI use case) and pastes both halves directly
   into `/workspace/.aws/credentials` (NOT into chat).
3. Claude verifies via `boto3.client("sts").get_caller_identity()`.
4. Claude creates the test bucket (`kinoforge-realcloud-tests-<account-id>`),
   sets lifecycle, scope-down policy. All subsequent layer work needs no
   further user-side action.

## Rotation policy

| Credential                | Recommended cadence | Owner            |
|---------------------------|---------------------|------------------|
| GCP SA key                | 90 days             | operator         |
| AWS access key            | 90 days             | operator         |
| fal / HF / CivitAI / RunPod tokens | as provider expires | operator |

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
