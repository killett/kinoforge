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

**Eight** follow-up `iam:SimulateCustomPolicy` probes with *concrete* ARNs
confirm the scoping is narrow as well as sufficient: the six in the table
below, plus the KMS pair described under it. (Three of the eight are
allow/deny *pairs* where the only thing that changed was the resource;
the other two — `s3:GetObject`, `iam:PassRole` — are single allow probes.)

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

### Size: attach this as a MANAGED policy, never inline

Measured 2026-08-23, and the reason the onboarding path in `.aws/README.md`
and `.env.example` uses `create-policy` + `attach-user-policy` rather than
`put-user-policy`:

| | characters |
|---|---|
| this policy, rendered — as IAM counts it | **3,422** |
| this policy, rendered — raw bytes on disk | 4,847 |
| IAM limit — a user's **inline** policies, in aggregate | 2,048 |
| IAM limit — a **managed** policy document | 6,144 |
| IAM limit — `SimulateCustomPolicy` `policyInputList` member | 2,000 |

The two rendered figures differ because **IAM does not count whitespace**
when sizing a policy, and the renderer writes pretty-printed JSON. Measure
compact, not `wc -c`, or you will scare yourself with the wrong number.

So `aws iam put-user-policy` — and the console's **Create inline policy** —
fail on this document with `LimitExceeded: Maximum policy size of 2048 bytes
exceeded`. That is not a hypothetical: it is what the first live validation
run hit on 2026-08-23, and it is why `tools/validate_scoped_policy.py` no
longer attaches the policy at all (it creates the probe user bare and passes
the document as `PolicyInputList` on each simulate call instead).

**The managed-policy sequence itself is EXERCISED, not merely inferred from
those numbers** (2026-08-23, free IAM calls, against a throwaway policy named
`…MinimalProbe` and a throwaway user, both destroyed afterwards):

- `create-policy --policy-document file://<rendered>` **succeeds** at 3,422
  characters and returns the policy ARN.
- `list-policies --scope Local --query "Policies[?PolicyName=='…'].Arn"`
  captures that ARN, so no account id has to be typed by hand.
- `attach-user-policy` succeeds; `list-attached-user-policies` confirms it.
- Re-running `create-policy` fails with `EntityAlreadyExists: A policy called
  … already exists. Duplicate names are not allowed.` — exactly the re-run
  case `.aws/README.md` documents.
- `create-policy-version --set-as-default` then returns `v2` as the default,
  with `v1` retained and non-default.

Note the contrast with `--policy-input-list` above: `file://` is **fine** for
`--policy-document`, which is a plain string parameter. It is only the
*list*-typed `--policy-input-list` that the AWS CLI JSON-parses into a
structure and rejects.

Not exercised: the 5-version cap. That figure is AWS's documented limit,
cited rather than measured — only one extra version was ever created here.

This proves the **attach mechanism**, nothing more. It says nothing about
whether a SkyPilot launch succeeds under the policy; that caveat is
unchanged and still open.

**If you are adding a statement to this template, mind the 6,144 ceiling.**
At 3,422 there is room, but it is finite — 2,722 characters of headroom, and
the S3 and EC2 statements are the ones that grow. Re-measure after any
addition:

```bash
pixi run python tools/render_aws_policy.py \
  --bucket-prefix kinoforge --out /tmp/p.json
python -c "import json;print(len(json.dumps(json.load(open('/tmp/p.json')),separators=(',',':'))))"
rm /tmp/p.json
```

The 2,000-char `SimulateCustomPolicy` limit is smaller still, so the whole
policy never fits there — the concrete-ARN probes above were run one
statement at a time, each passed **inline**. The `file://` form does not
work for `--policy-input-list`: the AWS CLI parses a `file://` JSON payload
into a structure for a list-typed parameter instead of passing it as a
string, and the call fails with `InvalidInput: Policy input list item 1 has
invalid content`. Corrected form:

```bash
pixi run -e live-skypilot aws iam simulate-custom-policy \
  --policy-input-list "$(cat /tmp/one-statement.json)" \
  --action-names s3:PutObject \
  --resource-arns arn:aws:s3:::kinoforge-abc/key.txt
```

The `.template` in the filename is load-bearing, not decorative: the file
carries `<AWS_ACCOUNT>`, `<KMS_KEY_ID>`, and `<S3_BUCKET_PREFIX>`
placeholders and cannot be handed to `aws iam create-policy` or pasted
into the IAM console as-is — AWS rejects a malformed ARN. Render it first:

```bash
pixi run python tools/render_aws_policy.py \
  --bucket-prefix <your-bucket-prefix> \
  --out /tmp/skypilot-minimal.rendered.json
```

`--account` defaults to the caller's own account via
`sts:GetCallerIdentity`; `--kms-key-id` defaults to the key id parsed out
of the gitignored `.aws/kms-test-key.arn`.

> **That ARN file will not exist on a fresh workspace.** The only thing
> that writes it is `pixi run cloud:bootstrap-kms`, and that task is
> currently **broken** — `tools/bootstrap_kms.py` still carries literal
> `<GCS_KMS_KEYRING>` placeholders as its operational constants, so it
> cannot provision the key. Until that is fixed, either create the file by
> hand (one line: the full `arn:aws:kms:…:key/…` ARN of an existing key)
> or pass `--kms-key-id` explicitly. **Neither is required for a plain
> SkyPilot setup**: with no key id available the renderer simply drops the
> `KMSLayerW` statement, and `tools/validate_scoped_policy.py` then
> reports `kms:Encrypt`/`kms:Decrypt` under `not_applicable` and still
> exits 0.

The renderer refuses to write
its output inside this repository, so the rendered (concrete-identifier-
bearing) file never becomes a tracked-file candidate.

**Do not attach `skypilot-minimal.template.json` directly** — neither via
console paste nor `--policy-document file://.aws/policies/skypilot-minimal.template.json`.
Attach the rendered output instead. See `.aws/README.md` for the full
apply walkthrough.
