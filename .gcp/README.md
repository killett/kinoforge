# GCP credentials — workspace-local

This directory holds GCP credentials for kinoforge's real-cloud GCS tests and
the SkyPilot live smoke.

Survives container restarts.

## What lives here

| Path                 | Purpose                                                  |
|----------------------|----------------------------------------------------------|
| `kinoforge-sa.json`  | Service-account key for `kinoforge-runner@…`.            |
| `gcloud-config/`     | `gcloud` config dir (active account, project, tokens).   |
| `kms-test-key.name`  | KMS key resource name (CMEK bucket test).                |
| `perms-snapshot.json`| SkyPilot-perms probe snapshot.                           |
| `README.md`          | This file.                                               |

`pixi.toml` sets `GOOGLE_APPLICATION_CREDENTIALS` and `CLOUDSDK_CONFIG` in
`[activation.env]`, so the `google-cloud-storage` Python SDK and the `gcloud`
CLI both auto-discover these.

## Service account

- Email: `kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com`
- Project: `<GCP_PROJECT>`
- Operator account: `<OPERATOR_EMAIL>`
- Billing account: `<GCP_BILLING_ACCOUNT>`
- Roles — see `.gcp/policies/roles.txt` for the authoritative list and the
  rationale. Runtime set:
  - `roles/compute.instanceAdmin.v1`
  - `roles/iam.serviceAccountUser`
  - `roles/storage.admin`
- **Bootstrap-only, revoke after binding:** `roles/iam.securityAdmin`,
  `roles/iam.serviceAccountAdmin`, `roles/serviceusage.serviceUsageAdmin`.

  `roles/iam.securityAdmin` can modify IAM bindings, so an identity holding
  it can grant itself any role in the project — project-owner under a
  quieter name, with the key sitting in a `.env` on a laptop. It is required
  only while binding the runtime roles and never at runtime; nothing in
  `src/` or `tools/` calls `setIamPolicy`. The revoke command is in
  `.gcp/policies/roles.txt`. **If it is still bound, revoke it now.**

## Real-cloud test bucket

- Bucket: `gs://<GCS_BUCKET>`
- Location: `US-CENTRAL1`
- Uniform bucket-level access: ON
- Public access prevention: ENFORCED
- Lifecycle: delete objects + abort incomplete multipart uploads at age 1 day
  (smoke artifacts are throwaway; auto-cleanup avoids accumulating cost).

Recreate or inspect with:

```
pixi run -e live-skypilot bash -c '
  gcloud storage buckets describe gs://<GCS_BUCKET>
'
```

## gcloud CLI

Lives in the `live-skypilot` pixi feature env. Invoke via:

```
pixi run -e live-skypilot gcloud <args>
```

Already configured account: `kinoforge-runner@…` (default via
`GOOGLE_APPLICATION_CREDENTIALS`).
Operator account: `<OPERATOR_EMAIL>` (also authenticated; switch with
`gcloud config set account <OPERATOR_EMAIL>`).

## Rotation

SA keys age. Best practice is to rotate every 90 days.

Substitute placeholders at the shell, not in this file.

```
pixi run -e live-skypilot gcloud iam service-accounts keys create \
  /workspace/.gcp/kinoforge-sa.json.new \
  --iam-account kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com
# verify by re-running a smoke, then:
mv /workspace/.gcp/kinoforge-sa.json.new /workspace/.gcp/kinoforge-sa.json
pixi run -e live-skypilot gcloud iam service-accounts keys delete <old-key-id> \
  --iam-account kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com
```
