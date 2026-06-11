# AWS GPU quota — justification

> **Status (as of day 5 = 2026-06-15):** ready to submit. Placeholder
> `$MTD_SPEND_USD$` is replaced by `tools/quota_burn.py snapshot` output
> immediately before submission via the support-case follow-up.

## Workload

Continuous-integration smoke tests for an open-source Python SDK
(`kinoforge`) that orchestrates GPU video-generation jobs across cloud
providers. Need 4 vCPU of On-Demand G/VT instances in `us-west-2`
(= 1x g4dn.xlarge) for ~8 minutes per test run; <= 10 runs/week;
<= $20/month total GPU spend. SkyPilot-driven launch -> run -> terminate
pattern. No persistent GPU fleet.

## Account context

Active billing customer. Month-to-date spend across EC2, S3, EBS, and
DynamoDB: **$MTD_SPEND_USD$**. Region preference: `us-west-2`.

## Cost controls

- AWS Budgets hard cap at $50/month with email alert.
- EventBridge auto-terminate rule destroys tagged smoke instances after
  30 minutes wall-time.
- Spot / On-Demand mix monitored via CloudWatch.

## Repository

<add public repo URL here on day 4, or strike this line if repo is private>
