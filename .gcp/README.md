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
| `README.md`          | This file.                                               |

`pixi.toml` sets `GOOGLE_APPLICATION_CREDENTIALS` and `CLOUDSDK_CONFIG` in
`[activation.env]`, so the `google-cloud-storage` Python SDK and the `gcloud`
CLI both auto-discover these.

## Service account

- Email: `kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com`
- Project: `<GCP_PROJECT>`
- Roles (granted on the project):
  - `roles/compute.admin`
  - `roles/iam.securityAdmin` ← lets Claude grant additional roles to self
    without re-auth.
  - `roles/iam.serviceAccountAdmin`
  - `roles/iam.serviceAccountUser`
  - `roles/serviceusage.serviceUsageAdmin`
  - `roles/storage.admin` ← covers all GCS bucket + object ops.
  - `roles/viewer`

## Real-cloud test bucket (Phase TBD — Layer S3GCS-verify)

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

Already configured account: `kinoforge-runner@…` (default).
Operator account: `[personal-email-redacted]` (also authenticated; switch with
`gcloud config set account [personal-email-redacted]`).

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
