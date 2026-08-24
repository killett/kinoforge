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
     `AmazonS3FullAccess` is the fallback if you are blocked, not the default —
     see the same section.
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
> `.aws/policies/skypilot-minimal.template.json` directly. Full
> UNVALIDATED-against-a-real-launch warning:
> `.aws/policies/README.md`.

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

Confirm the scoped policy is sufficient with `pixi run cloud:perms-probe`
against AWS — it should exit 0 without `AmazonS3FullAccess` ever being
attached. If you used the `AmazonS3FullAccess` fallback from step 1 of
Bootstrap because you were blocked, detach it once the probe is green:

> Users → `kinoforge-ci` → Permissions → checkbox `AmazonS3FullAccess` →
> Remove.

The scoped policy covers all S3 operations kinoforge needs against the
`<S3_BUCKET_PREFIX>-*` and `skypilot-*` prefixes; broader S3
access is not required.

## Rotation

Access keys age. AWS recommends rotation every 90 days.

- AWS Console → IAM → Users → kinoforge-ci → Security credentials.
- Create a NEW access key first, paste into `credentials`, verify, THEN
  deactivate + delete the old one.
