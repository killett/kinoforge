# AWS GPU quota — justification

> **Status (as of day 6 = 2026-06-17):** ready to submit. MTD figures
> populated from `tools/quota_burn.py snapshot` taken 2026-06-17T06:49:53.

## Workload

Continuous-integration smoke tests for an open-source Python SDK
(`kinoforge`) that orchestrates GPU video-generation jobs across cloud
providers. Need 4 vCPU of On-Demand G/VT instances in `us-west-2`
(= 1x g4dn.xlarge) for ~8 minutes per test run; <= 10 runs/week;
<= $20/month total GPU spend. SkyPilot-driven launch -> run -> terminate
pattern. No persistent GPU fleet.

## Account context

Active billing customer. Month-to-date spend across VPC ($0.57),
EC2-compute ($0.48), EC2-other / EBS ($0.37), KMS ($0.33), Cost
Explorer ($0.01), S3 ($0.00): **$1.76 total**. Region preference:
`us-west-2`.

## Cost controls

- AWS Budgets hard cap at $50/month with email alert.
- EventBridge auto-terminate rule destroys tagged smoke instances after
  30 minutes wall-time.
- Spot / On-Demand mix monitored via CloudWatch.

## Repository

https://github.com/killett/kinoforge
