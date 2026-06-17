# GCP GPU quota — justification

> **Status (as of day 6 = 2026-06-17):** ready to submit. MTD figures
> populated from `tools/quota_burn.py snapshot` taken 2026-06-17T06:49:53.

## Workload

Continuous-integration smoke tests for an open-source Python SDK
(`kinoforge`) that orchestrates GPU video-generation jobs across cloud
providers. Need 1x preemptible NVIDIA T4 in `us-west1-a` for ~8 minutes
per test run; <= 10 runs/week; <= $20/month total GPU spend. Workload
pattern: launch via SkyPilot -> run smoke test -> autostop. Never
persistent. No production traffic.

## Account context

Active pay-as-you-go customer since 2026-06-07. Month-to-date spend
across Compute Engine ($2.20), Networking ($0.36), Cloud KMS ($0.02),
Cloud Storage ($0.00), BigQuery ($0.00), Cloud Logging ($0.00) on
project `kinoforge-prod-0ddb375e`: **$2.58 total**.

## Cost controls

- Cloud Billing budget alarm at $50/month notifies the project owner
  via email.
- SkyPilot's `autostop=10m` flag terminates idle GPU instances.
- All GPU launches restricted to the preemptible / spot tier; no
  on-demand reservations.

## Repository

https://github.com/killett/kinoforge
