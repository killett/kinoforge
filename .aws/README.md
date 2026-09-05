# AWS credentials — workspace-local

This directory holds AWS credentials for kinoforge's real-cloud S3 tests.

Gitignored. Never commit. Do not paste contents into any chat tool.

## What lives here

| File          | Purpose                                                          |
|---------------|------------------------------------------------------------------|
| `credentials` | `[default]` profile with access key + secret. Type in directly.  |
| `config`      | `[default]` profile region (us-west-2) + output format (json).   |
| `README.md`   | This file.                                                       |

`pixi.toml` sets `AWS_SHARED_CREDENTIALS_FILE=$PIXI_PROJECT_ROOT/.aws/credentials`
and `AWS_CONFIG_FILE=$PIXI_PROJECT_ROOT/.aws/config` in `[activation.env]`, so
`boto3` (and any subprocess of `pixi run X`) discovers these automatically.

## Bootstrap — one-time, user-side (~3 min)

1. AWS Console → IAM → Users → **Add user**
   - User name: `kinoforge-ci`
   - Permissions: **none yet.** Create the user bare, then attach the scoped
     policy via the CLI path in "SkyPilot policy — apply instructions" below.
   - **Blocked?** The one thing that blocks this is not being able to create
     or attach a customer-managed policy (an operator identity without
     `iam:CreatePolicy` / `iam:AttachUserPolicy`). Only then, and only in an
     account holding nothing you care about, attach the AWS-managed
     `AmazonS3FullAccess` to get moving:

     ```bash
     aws iam attach-user-policy --user-name kinoforge-ci \
       --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
     ```

     That unblocks the S3 test-bucket work only — it grants no EC2, so
     SkyPilot launches still need the scoped policy. Detach it as soon as
     the scoped policy is attached and validated; the exact command is in
     "Detaching the fallback" at the end of that section. The separate
     compute identity (`kinoforge-runner`) has its own, wider shortcut
     documented in `.env.example` under "BOOTSTRAP SHORTCUT" — do not mix
     the two.
2. Open the new user → **Security credentials** → **Create access key**
   - Use case: **Command Line Interface (CLI)**
   - Confirm + Next + Create.
3. Open `/workspace/.aws/credentials` in your editor (not any chat tool).
4. Paste `Access key ID` after `aws_access_key_id =`
5. Paste `Secret access key` after `aws_secret_access_key =`
6. Save. Done.

After paste, verify with `pixi run python -c "import boto3;
print(boto3.client('sts').get_caller_identity())"` — should print the
`kinoforge-ci` ARN. The user has no permissions yet; continue to
"SkyPilot policy — apply instructions" below to attach the scoped policy
before running the test bucket creation, lifecycle, or S3 store smoke.

## Scoped IAM policy — S3-only alternative (optional, narrower than the SkyPilot template)

Bootstrap step 1 above attaches nothing by default, so there is no wide grant
here to "swap out." This section is for a narrower case: if you only need S3
test-bucket access — not SkyPilot compute launches — this JSON is a lighter
alternative to the SkyPilot template below, since the template's own S3
statements already cover this exact scope
(`<S3_BUCKET_PREFIX>-*` and `skypilot-*` prefixes) plus everything SkyPilot
needs. If you're also going to run SkyPilot launches, skip this section and
go straight to "SkyPilot policy — apply instructions" below — attaching both
would be redundant.

Attach the same way as any inline policy: IAM console → `kinoforge-ci` →
Permissions → **Add permissions** → **Create inline policy** → JSON tab,
using an admin/operator identity — the bare `kinoforge-ci` user created in
Bootstrap has no IAM permissions of its own to self-attach a policy.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListOwnedBuckets",
      "Effect": "Allow",
      "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"],
      "Resource": "*"
    },
    {
      "Sid": "TestBucketAdmin",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:PutBucketLifecycleConfiguration",
        "s3:PutBucketVersioning",
        "s3:PutBucketPublicAccessBlock",
        "s3:GetBucketLifecycleConfiguration",
        "s3:GetBucketVersioning"
      ],
      "Resource": "arn:aws:s3:::<S3_BUCKET_PREFIX>-*"
    },
    {
      "Sid": "TestBucketObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListBucket",
        "s3:ListMultipartUploadParts",
        "s3:ListBucketMultipartUploads"
      ],
      "Resource": [
        "arn:aws:s3:::<S3_BUCKET_PREFIX>-*",
        "arn:aws:s3:::<S3_BUCKET_PREFIX>-*/*"
      ]
    }
  ]
}
```

## SkyPilot policy — apply instructions

The scoped IAM policy template lives at
`.aws/policies/skypilot-minimal.template.json` (tracked, not secret; the
`.template` in the name is deliberate — see below). It covers
EC2 lifecycle + IAM PassRole on `skypilot-*` roles + ServiceQuotas +
S3 access scoped to `<S3_BUCKET_PREFIX>-*` and `skypilot-*`
prefixes, plus — only when a KMS key id is supplied — KMS access scoped
to the existing CMEK key. A plain SkyPilot launch does not need that KMS
statement; the renderer drops it by default (see step 1 below).

> The template carries `<AWS_ACCOUNT>`, `<KMS_KEY_ID>`, and
> `<S3_BUCKET_PREFIX>` placeholders and cannot be pasted into the console or
> passed to `create-policy` as-is — AWS rejects a malformed ARN. Render it
> first with `tools/render_aws_policy.py`; never attach
> `.aws/policies/skypilot-minimal.template.json` directly. What the policy
> has and has not been proven to carry — it launched a real EC2 instance on
> 2026-09-04, with a named list of what that run did *not* exercise — is in
> `.aws/policies/README.md`, along with what to run when a launch fails on
> permissions.

> **Attach it as a MANAGED policy, never an inline one.** IAM caps a user's
> inline policies at **2048 characters in aggregate**; this policy renders
> to ~3.4 KB, so `aws iam put-user-policy` — and the console's **Create
> inline policy** — fail with `LimitExceeded: Maximum policy size of 2048
> bytes exceeded`. A managed policy's ceiling is **6144**, which this fits
> with room to spare. Measured 2026-08-23; see `.aws/policies/README.md`.

To attach it to the existing `kinoforge-ci` IAM user:

1. Render the template — this is not optional, see the warning above:

   ```bash
   pixi run python tools/render_aws_policy.py \
     --bucket-prefix <your-bucket-prefix> \
     --out /tmp/skypilot-minimal.rendered.json
   ```

   No KMS key id needed for a plain SkyPilot launch: with no
   `--kms-key-id` and no gitignored `.aws/kms-test-key.arn` on disk, the
   renderer drops the `KMSLayerW` statement and prints a one-line notice
   to stderr saying so — the rest of the policy is still valid and
   attachable. CMEK / Layer W bucket-test users pass
   `--kms-key-id <key-id>` to keep that statement.

2. Create the managed policy and attach it. The CLI path (preferred — it
   captures the ARN for you instead of making you type an account id):

   ```bash
   aws iam create-policy --policy-name KinoforgeSkypilotMinimal \
     --policy-document file:///tmp/skypilot-minimal.rendered.json \
     --query 'Policy.Arn' --output text

   POLICY_ARN=$(aws iam list-policies --scope Local \
     --query "Policies[?PolicyName=='KinoforgeSkypilotMinimal'].Arn" \
     --output text)

   aws iam attach-user-policy --user-name kinoforge-ci \
     --policy-arn "$POLICY_ARN"
   ```

   If you would rather not shell out, `sts:GetCallerIdentity` gives the
   same account id the ARN needs:

   ```bash
   aws sts get-caller-identity --query Account --output text
   # -> arn:aws:iam::<that>:policy/KinoforgeSkypilotMinimal
   ```

3. **Re-running this?** `create-policy` fails with `EntityAlreadyExists`
   the second time. Publish a new default version rather than deleting and
   recreating the policy — deleting it detaches it from every principal:

   ```bash
   aws iam create-policy-version --policy-arn "$POLICY_ARN" \
     --policy-document file:///tmp/skypilot-minimal.rendered.json \
     --set-as-default
   ```

   A managed policy holds at most **5 versions**. If that call errors with
   `LimitExceeded`, drop the oldest non-default version first:

   ```bash
   aws iam list-policy-versions --policy-arn "$POLICY_ARN"
   aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id v1
   ```

4. Console equivalent, if you prefer clicking: [AWS IAM Console →
   Users](https://us-west-2.console.aws.amazon.com/iam/home#/users) →
   `kinoforge-ci` → Permissions tab → **Add permissions** → **Attach
   policies directly** → **Create policy** → JSON tab → paste the entire
   contents of the *rendered* file (`/tmp/skypilot-minimal.rendered.json`),
   not the tracked template → Review → name it `KinoforgeSkypilotMinimal` →
   Create policy → then attach it to the user.

   Do **not** use **Create inline policy** here — that is the 2048-char
   path and it will reject this policy.

5. Confirm the policy is now attached to `kinoforge-ci`:

   ```bash
   aws iam list-attached-user-policies --user-name kinoforge-ci \
     --query 'AttachedPolicies[].PolicyName' --output text
   ```

6. Confirm the scoped policy is sufficient — with
   **`tools/validate_scoped_policy.py`**, which takes the rendered policy
   as input and simulates it against a bare throwaway principal:

   ```bash
   pixi run python tools/validate_scoped_policy.py --cloud aws \
     --policy-file /tmp/skypilot-minimal.rendered.json \
     --confirm-live
   ```

   Run this under an **admin/operator** identity, not the `kinoforge-ci`
   key: it creates and deletes a throwaway IAM user (`iam:CreateUser`,
   `iam:DeleteUser`, `iam:SimulatePrincipalPolicy`), none of which the
   scoped policy grants. Free calls only; no EC2 instance.

   **Expected clean result** for the default step-1 render (no
   `--kms-key-id`): exit 0, `"denied": []`, `"ungranted": []`,
   `"missing": []`, and `"not_applicable": ["kms:Decrypt",
   "kms:Encrypt"]`. Those two are listed because the render dropped the
   `KMSLayerW` statement on purpose — they were never required. **That is
   success.** Do not add KMS grants and do not widen a `Resource` to `*`
   to shorten a list. Rendering *with* `--kms-key-id` instead gives an
   empty `not_applicable` and all 15 actions `allowed`.

   > `pixi run cloud:perms-probe` is a **different** tool and does not
   > answer this question. It simulates against whatever identity your
   > shell already carries, with no policy document as input, so a KMS-less
   > setup shows `kms:Encrypt`/`kms:Decrypt` in **`denied`** — which reads
   > as a scoping bug it is not. It also has a `--submit-quota-increase`
   > flag that calls `RequestServiceQuotaIncrease`, i.e. **opens a real AWS
   > support case**; that is opt-in and never fires on a plain run, but it
   > is a reason to know which tool you are invoking. Use the probe to
   > audit a live identity's permissions and GPU quota; use
   > `validate_scoped_policy.py` to audit the policy document.

### Detaching the fallback

If you attached `AmazonS3FullAccess` at Bootstrap step 1 because you were
blocked, remove it now that the scoped policy is attached and validated:

```bash
aws iam detach-user-policy --user-name kinoforge-ci \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
```

Console equivalent: Users → `kinoforge-ci` → Permissions → checkbox
`AmazonS3FullAccess` → Remove. Confirm with the
`list-attached-user-policies` command in step 5 — only
`KinoforgeSkypilotMinimal` should remain.

The scoped policy covers all S3 operations kinoforge needs against the
`<S3_BUCKET_PREFIX>-*` and `skypilot-*` prefixes; broader S3
access is not required.

## Bedrock policies — apply instructions

`.aws/policies/bedrock-nova-reel.template.json` and
`.aws/policies/bedrock-luma-ray.template.json` scope Bedrock async-invoke plus
`s3:PutObject`/`GetObject`/`HeadObject` on the **one** bucket the job writes
its video to. Like the SkyPilot template, they carry placeholders
(`<AWS_ACCOUNT>`, `<S3_OUTPUT_BUCKET>`) and must be rendered first:

```bash
pixi run python tools/render_aws_policy.py \
  --policy bedrock-luma-ray \
  --output-bucket "$KINOFORGE_LIVE_S3_BUCKET" \
  --out /tmp/bedrock-luma-ray.rendered.json
```

`--output-bucket` is the same bucket the live smokes read from
`KINOFORGE_LIVE_S3_BUCKET` in `.env`; the renderer refuses a wildcard or an
illegal name, because the grant is meant to cover exactly one bucket. These
policies are small enough to attach **inline** (unlike the SkyPilot one):

```bash
aws iam put-user-policy --user-name kinoforge-ci \
  --policy-name kinoforge-luma-ray \
  --policy-document file:///tmp/bedrock-luma-ray.rendered.json
```

Until 2026-09-04 both files hardcoded a real bucket name in their S3 ARNs
while templating `<AWS_ACCOUNT>` in the same document. That is the reason
for the `.template` rename: the name is what tells you not to attach it as-is.

## Rotation

Access keys age. AWS recommends rotation every 90 days.

- AWS Console → IAM → Users → kinoforge-ci → Security credentials.
- Create a NEW access key first, paste into `credentials`, verify, THEN
  deactivate + delete the old one.
