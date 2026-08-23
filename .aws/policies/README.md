# AWS scoped IAM policy templates

This directory holds tracked (not secret) IAM policy documents. None of
these bytes are a credential — they are permission scoping, safe to commit.

## `skypilot-minimal.template.json`

**UNVALIDATED against a real SkyPilot launch.** Simulate-validated only
(`tools/validate_scoped_policy.py`, IAM policy simulation via
`iam:SimulatePrincipalPolicy`). Simulation proves the policy's *logic* —
that the listed actions resolve to `allowed` against the listed resources —
not that a real `sky launch` succeeds end-to-end against it. A live launch
can still fail on an action the simulation didn't cover, a service
interaction IAM's simulator doesn't model, or a quota/capacity limit that
has nothing to do with permissions. Task 9 of
`docs/superpowers/plans/2026-08-21-least-privilege-onboarding.md` owns
closing that gap; until it lands (or a later note in this file says
otherwise), treat this policy as **not yet proven against a real launch**.

The `.template` in the filename is load-bearing, not decorative: the file
carries `<AWS_ACCOUNT>`, `<KMS_KEY_ID>`, and `<S3_BUCKET_PREFIX>`
placeholders and cannot be handed to `aws iam put-user-policy` or pasted
into the IAM console as-is — AWS rejects a malformed ARN. Render it first:

```bash
pixi run python tools/render_aws_policy.py \
  --bucket-prefix <your-bucket-prefix> \
  --out /tmp/skypilot-minimal.rendered.json
```

`--account` defaults to the caller's own account via
`sts:GetCallerIdentity`; `--kms-key-id` defaults to the key id parsed out
of the gitignored `.aws/kms-test-key.arn`. The renderer refuses to write
its output inside this repository, so the rendered (concrete-identifier-
bearing) file never becomes a tracked-file candidate.

**Do not attach `skypilot-minimal.template.json` directly** — neither via
console paste nor `--policy-document file://.aws/policies/skypilot-minimal.template.json`.
Attach the rendered output instead. See `.aws/README.md` for the full
apply walkthrough.
