# GCP GPU quota — justification

> **Status (as of day 5 = 2026-06-15):** ready to submit. Placeholder
> `$MTD_SPEND_USD$` is replaced by `tools/quota_burn.py snapshot` output
> immediately before submission.

## Workload

Continuous-integration smoke tests for an open-source Python SDK
(`kinoforge`) that orchestrates GPU video-generation jobs across cloud
providers. Need 1x preemptible NVIDIA T4 in `us-west1-a` for ~8 minutes
per test run; <= 10 runs/week; <= $20/month total GPU spend. Workload
pattern: launch via SkyPilot -> run smoke test -> autostop. Never
persistent. No production traffic.

## Account context

Active pay-as-you-go customer since 2026-06-07. Month-to-date spend
across Compute Engine, Cloud Storage, BigQuery, and other services on
project `kinoforge-prod-0ddb375e`: **$MTD_SPEND_USD$**.

## Cost controls

- Cloud Billing budget alarm at $50/month notifies the project owner
  via email.
- SkyPilot's `autostop=10m` flag terminates idle GPU instances.
- All GPU launches restricted to the preemptible / spot tier; no
  on-demand reservations.

## Repository

<add public repo URL here on day 4, or strike this line if repo is private>
