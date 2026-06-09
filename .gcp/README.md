# GCP credentials — workspace-local

This directory holds GCP credentials for kinoforge's real-cloud GCS tests and
the SkyPilot live smoke (Phase 31).

Gitignored. Never commit. Survives container restarts (see auto-memory
`reference_gcloud_persistence.md`).

## What lives here

| Path                 | Purpose                                                  |
|----------------------|----------------------------------------------------------|
| `kinoforge-sa.json`  | Service-account key for `kinoforge-runner@…`.            |
| `gcloud-config/`     | `gcloud` config dir (active account, project, tokens).   |
| `kms-test-key.name`  | KMS key resource name (Layer W CMEK bucket).             |
| `perms-snapshot.json`| SkyPilot-perms probe snapshot (Layer W+α).               |
| `README.md`          | This file.                                               |

`pixi.toml` sets `GOOGLE_APPLICATION_CREDENTIALS` and `CLOUDSDK_CONFIG` in
`[activation.env]`, so the `google-cloud-storage` Python SDK and the `gcloud`
CLI both auto-discover these.

## Service account

- Email: `kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com`
- Project: `<GCP_PROJECT>`
- Operator account: `<OPERATOR_EMAIL>`
- Billing account: `<GCP_BILLING_ACCOUNT>`
- Roles (granted on the project):
  - `roles/compute.admin`
  - `roles/iam.securityAdmin` ← lets Claude grant additional roles to self
    without re-auth.
  - `roles/iam.serviceAccountAdmin`
  - `roles/iam.serviceAccountUser`
  - `roles/serviceusage.serviceUsageAdmin`
  - `roles/storage.admin` ← covers all GCS bucket + object ops.
  - `roles/viewer`

> Migrated 2026-06-09 from a previous GCP project under a separate
> operator account (now retired). Old SA key file has been deleted;
> legacy OAuth + legacy SA revoked from local gcloud config.

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

```
pixi run -e live-skypilot gcloud iam service-accounts keys create \
  /workspace/.gcp/kinoforge-sa.json.new \
  --iam-account kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com
# verify by re-running a smoke, then:
mv /workspace/.gcp/kinoforge-sa.json.new /workspace/.gcp/kinoforge-sa.json
pixi run -e live-skypilot gcloud iam service-accounts keys delete <old-key-id> \
  --iam-account kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com
```
