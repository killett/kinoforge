# AWS scoped IAM policy templates

This directory holds tracked (not secret) IAM policy documents. None of
these bytes are a credential — they are permission scoping, safe to commit.

## `skypilot-minimal.template.json`

**UNVALIDATED against a real SkyPilot launch.** Simulate-validated
2026-08-23 against real IAM (`tools/validate_scoped_policy.py`,
`iam:SimulatePrincipalPolicy` under this workspace's own AWS account):
**all 15 required actions returned `allowed`** — `denied: []`, `ungranted: []`,
`missing: []`, rc=0. The render under test included the `KMSLayerW`
statement (a key id resolved from `.aws/kms-test-key.arn`), so both
`kms:Encrypt` and `kms:Decrypt` were simulated against the real key ARN
rather than dropped. Free calls only: no EC2 instance was created, and the
throwaway probe user was confirmed deleted afterwards.

Six follow-up `iam:SimulateCustomPolicy` probes with *concrete* ARNs
confirm the scoping is narrow as well as sufficient. In each pair the only
thing that changed was the resource:

| action | resource | decision |
|---|---|---|
| `s3:PutObject` | `arn:aws:s3:::kinoforge-abc/key.txt` | `allowed` |
| `s3:PutObject` | `arn:aws:s3:::not-kinoforge-abc/key.txt` | `implicitDeny` |
| `s3:GetObject` | `arn:aws:s3:::skypilot-abc/key.txt` | `allowed` |
| `iam:CreateRole` | `…:role/skypilot-x` | `allowed` |
| `iam:CreateRole` | `…:role/admin-x` | `implicitDeny` |
| `iam:PassRole` | `…:role/sky-x` | `allowed` |

`kms:Encrypt` was probed the same way: `allowed` against the key ARN the
render resolved, `implicitDeny` against an all-zeroes key id in the same
account.

Simulation proves the policy's *logic* — that the listed actions resolve
to `allowed` against the listed resources, and to `implicitDeny` outside
them — **not** that a real `sky launch` succeeds end-to-end against it. A
live launch can still fail on an action the simulation didn't cover, a
service interaction IAM's simulator doesn't model, or a quota/capacity
limit that has nothing to do with permissions. This policy has still never
been attached to a real principal that then launched anything, so treat it
as **simulate-clean, launch-unvalidated**.

Two AWS API limits worth knowing before re-running the validation by hand:
`iam:PutUserPolicy` caps a user's inline policies at **2048 characters** in
aggregate and this policy renders to 3422, so the document cannot be
attached to the probe user — the validator passes it as
`PolicyInputList` on the simulate call instead. `iam:SimulateCustomPolicy`
caps each `policyInputList` member at **2000 characters**, so the whole
policy will not fit there either; the concrete-ARN probes above were run
one statement at a time, passed inline (the `file://` form makes the AWS
CLI parse the document as a structure and the call fails with
`InvalidInput`).

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
