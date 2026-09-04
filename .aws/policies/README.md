# AWS scoped IAM policy templates

This directory holds tracked (not secret) IAM policy documents. None of
these bytes are a credential — they are permission scoping, safe to commit.

## `skypilot-minimal.template.json`

**LAUNCH-VALIDATED 2026-09-04.** A throwaway IAM user holding this policy —
and nothing else — launched a real EC2 instance in `us-west-2` via SkyPilot,
ran a job on it and tore it down. `rc=0`, no denial anywhere in the log, and
CloudTrail records `errorCode` NONE on all 37 API calls that principal made.
See **"What the live launch proved"** below for exactly what that covers, and
the shorter list of what it still does not. Reproduce with
`tests/live/test_scoped_policy_aws_live.py`; evidence in
`tests/live/_scoped_policy_aws_live_evidence.json`.

The simulate-validation below stands, and is what made the live run pass on
the first attempt:

Simulate-validated
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
limit that has nothing to do with permissions. That gap is what the live
run below closes.

## What the live launch proved

2026-09-04, `tests/live/test_scoped_policy_aws_live.py`, ~$0.02 of EC2.

The setup, because the result is only worth what the isolation is worth:

- A throwaway IAM user (`kinoforge-scope-live-<8 hex>`) created for the run
  and deleted after it. Its grants were asserted, not assumed: **exactly one**
  attached policy — this one, rendered — with no inline policies and no group
  memberships.
- The launch ran in a subprocess whose environment was built from an
  **allow-list**, not inherited. pixi's `[activation.env]` exports
  `AWS_SHARED_CREDENTIALS_FILE` pointing at the real `kinoforge-ci`
  credential; inheriting it would have run the whole thing as an admin and
  reported a triumphant false pass. `sts:GetCallerIdentity`, executed inside
  that environment, was asserted to return the throwaway user.
- SkyPilot's API server was stopped first and its absence confirmed from the
  process table. That server is a separate, long-lived process holding
  whatever environment it was born with — this workspace had one running
  continuously since 2026-08-27, and it would have served the launch under
  the ambient credential.
- The instance was confirmed by EC2 under a *different* principal. An empty
  answer fails the test rather than passing vacuously.

**Result: green on the first attempt.** The brief that commissioned this work
budgeted an afternoon for an iteration loop of one-denial-at-a-time; there
were no denials to iterate on. The simulate-derived action list was already
sufficient for this path.

### The call inventory — what the policy is load-bearing for

More useful than a denial log, and the same information: this is every AWS
call the scoped principal actually made, read from CloudTrail after the fact
(`Username = kinoforge-scope-live-61de6b66`). `errorCode` was NONE on every
one.

| region | calls |
|---|---|
| `us-west-2` (32) | `DescribeInstances` ×9, `DescribeVpcs` ×5, `DescribeSecurityGroups` ×4, `DescribeRouteTables` ×2, `DescribeAvailabilityZones` ×2, `GetCallerIdentity` ×2, `RunInstances`, `TerminateInstances`, `CreateSecurityGroup`, `AuthorizeSecurityGroupIngress`, `CreateTags`, `DescribeSubnets`, `DescribeImages`, `ListBuckets` |
| `us-east-1` (5) | `DescribeRegions` ×2, `DescribeAvailabilityZones` ×2, `GetInstanceProfile` |

Two things that inventory says which the policy document does not:

- **`ListBuckets` is not optional.** `s3:ListAllMyBuckets` has its own
  statement (`S3ListAll`) and looks like a leftover; it is not. SkyPilot calls
  it during cloud-enablement checks, and the negative control below died on
  exactly that call.
- **`DescribeRegions` is the first thing that fails.** With no policy at all,
  sky aborts before provisioning with `Failed to retrieve AWS regions. Please
  ensure that the ec2:DescribeRegions action is enabled for your AWS account
  in IAM.` It is covered here by `ec2:Describe*`.

### What the live run did NOT exercise

Stated plainly, because a green run invites over-reading:

- **The IAM write path.** `GetInstanceProfile` succeeded because
  `skypilot-v1` already existed in this account (created 2026-08-16). So
  `iam:CreateRole`, `iam:CreateInstanceProfile`, `iam:AddRoleToInstanceProfile`
  and `iam:PutRolePolicy` were never called. On a **fresh account the first
  launch runs all of them**, and only simulation covers them today. If you are
  the first launch in a new account and it fails on IAM, that is the untested
  seam — the actions are granted here, scoped to `skypilot-*` / `sky-*`.
- **The key-pair actions.** `us-west-2` holds no EC2 key pairs at all and no
  `CreateKeyPair` / `ImportKeyPair` / `DescribeKeyPairs` call appears in the
  trail: sky 0.12.3 provisioned SSH without one. `ec2:CreateKeyPair`,
  `ec2:ImportKeyPair` and `ec2:DeleteKeyPair` are therefore unexercised. They
  have NOT been removed — an older sky, a different backend, or a config that
  pins a key would use them, and dropping a granted-but-unused action to
  "tidy up" is how the next launch breaks.
- **KMS, and most of S3.** The rendered document under test carried
  `KMSLayerW` (a real key ARN from `.aws/kms-test-key.arn`), but a plain
  CPU launch touches neither it nor the `<S3_BUCKET_PREFIX>-*` object
  actions. Those remain simulate-only.
- **GPU capacity, quotas, and anything about cost.** A permission test says
  nothing about whether the SKU you want is available to you.

### The negative control

Worth as much as the green run, and it is what stops this whole exercise
being decorative: the *same* launch under a principal holding **no policy at
all** fails in 17 seconds, books no EC2 instance, and names the missing
permission. Without that, a green result could just mean the account grants
everything to everyone and this file is scenery.

## When a launch fails on permissions

The live run above covers one path. Yours may be a different one — a fresh
account hitting the IAM write path, a GPU SKU in another region, an S3 or KMS
operation nothing here exercised. When that happens you get an opaque failure
mid-launch, and the fastest-looking way out is `AdministratorAccess`. It is
also the one that permanently un-scopes this account. Two commands instead.

**1. What was actually denied — CloudTrail.** Verified 2026-09-04; the output
below is real, from the negative-control principal of that run.

```bash
PROBE=kinoforge-ci   # or whichever principal ran the failing launch

aws cloudtrail lookup-events \
  --region us-west-2 \
  --lookup-attributes AttributeKey=Username,AttributeValue="$PROBE" \
  --max-results 50 \
  --query 'Events[].CloudTrailEvent' --output text \
| tr '\t' '\n' \
| python -c 'import sys, json
for line in sys.stdin:
    if not line.strip():
        continue
    e = json.loads(line)
    if e.get("errorCode"):
        print(e["eventTime"], e["eventName"], e["errorCode"],
              (e.get("errorMessage") or "")[:160])'
```

Real output from that run:

```
2026-09-04T21:54:29Z ListBuckets AccessDenied User: arn:aws:iam::…:user/kinoforge-scope-ctl-… is not authorized to perform: s3:ListAllMyBuckets
```

**THE FIELD TO READ** is the action name inside `errorMessage`, after
`is not authorized to perform:` — `s3:ListAllMyBuckets` above. That is what
goes in the policy. `eventName` beside it is the *API* name (`ListBuckets`),
which is often spelled differently from the IAM action and is not what you
add.

Four things that will waste your time if you don't know them:

- **`cloudtrail:LookupEvents` is not granted by this policy, and
  `kinoforge-ci` does not hold it by default.** Both are deliberate: you run
  the lookup as the operator investigating, not as the principal under test.
  It was attached to `kinoforge-ci` on 2026-09-04 purely to verify the command
  above works, then detached the same session. To grant it again:
  `aws iam attach-user-policy --user-name kinoforge-ci --policy-arn
  arn:aws:iam::aws:policy/AWSCloudTrail_ReadOnlyAccess` — and detach it when
  you are done.
- **CloudTrail lags.** Events took ~15-25 minutes to appear during this
  validation. An empty result right after a failure means "not yet", not "no
  denial".
- **IAM, STS and other global services log to `us-east-1`,** not to the region
  you launched in. Run the query twice.
- **A client-side refusal never reaches AWS,** so it is never in the trail.
  SkyPilot's `Failed to retrieve AWS regions … ec2:DescribeRegions` is sky's
  own wording for a denial it caught; read its stderr as well as the trail.

**2. Whether the policy grants it — simulate before you re-launch.** Once you
have an action name, `tools/validate_scoped_policy.py` and the
`simulate-custom-policy` form documented further down answer "would this
policy allow it, against this exact ARN" for free, in seconds, with no
instance involved. That loop is much faster than re-launching to find out.

**`AdministratorAccess` / `AmazonEC2FullAccess` are the fallback of last
resort.** Widening to one of them to get unblocked under time pressure is a
legitimate call. **Record it here when you do** — the date, the denial that
forced it, and whether it is still attached. A temporary widening that nobody
writes down is how the permanent state gets decided by accident, and a scoped
policy file that quietly describes something nobody is running is worse than
no file at all. `.aws/README.md` → "Detaching the fallback" has the reverse
command.

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

This proves the **attach mechanism**, nothing more — whether a launch
succeeds under the policy is a separate question, answered by the live run
documented above rather than by anything in this section.

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
