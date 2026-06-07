# AWS credentials — workspace-local

This directory holds AWS credentials for kinoforge's real-cloud S3 tests.

Gitignored. Never commit. Do not paste contents into chat with Claude.

## What lives here

| File          | Purpose                                                          |
|---------------|------------------------------------------------------------------|
| `credentials` | `[default]` profile with access key + secret. Type in directly.  |
| `config`      | `[default]` profile region (us-east-1) + output format (json).   |
| `README.md`   | This file.                                                       |

`pixi.toml` sets `AWS_SHARED_CREDENTIALS_FILE=$PIXI_PROJECT_ROOT/.aws/credentials`
and `AWS_CONFIG_FILE=$PIXI_PROJECT_ROOT/.aws/config` in `[activation.env]`, so
`boto3` (and any subprocess of `pixi run X`) discovers these automatically.

## Bootstrap — one-time, user-side (~3 min)

1. AWS Console → IAM → Users → **Add user**
   - User name: `kinoforge-ci`
   - Permissions: attach **`AmazonS3FullAccess`** (simplest; scope-down below).
2. Open the new user → **Security credentials** → **Create access key**
   - Use case: **Command Line Interface (CLI)**
   - Confirm + Next + Create.
3. Open `/workspace/.aws/credentials` in this workspace's editor (NOT chat).
4. Paste `Access key ID` after `aws_access_key_id =`
5. Paste `Secret access key` after `aws_secret_access_key =`
6. Save. Done.

After paste, ask Claude to verify with `pixi run python -c "import boto3;
print(boto3.client('sts').get_caller_identity())"` — should print the
`kinoforge-ci` ARN. If it does, Claude takes over: creates the test bucket,
sets lifecycle, attaches a scoped IAM policy, runs the S3 store smoke.

## Scoped IAM policy (optional follow-up)

`AmazonS3FullAccess` is broad. Once the test bucket name is known, swap it for
this scoped policy (Claude can create + attach it via the bootstrap access
key):

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
      "Resource": "arn:aws:s3:::kinoforge-realcloud-tests-*"
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
        "arn:aws:s3:::kinoforge-realcloud-tests-*",
        "arn:aws:s3:::kinoforge-realcloud-tests-*/*"
      ]
    }
  ]
}
```

## SkyPilot policy — apply instructions (Layer W+α T2)

The scoped IAM policy doc lives at
`.aws/policies/skypilot-minimal.json` (tracked, not secret). It covers
EC2 lifecycle + IAM PassRole on `skypilot-*` roles + ServiceQuotas +
S3 access scoped to `kinoforge-realcloud-tests-*` and `skypilot-*`
prefixes + KMS access scoped to the existing Layer W key
(`alias/kinoforge-realcloud-tests`).

To attach it to the existing `kinoforge-ci` IAM user:

1. Open the [AWS IAM Console → Users](https://us-east-1.console.aws.amazon.com/iam/home#/users) — account `<AWS_ACCOUNT>`.
2. Click `kinoforge-ci`.
3. Permissions tab → **Add permissions** → **Create inline policy**
   (or **Attach policies directly → Create policy**).
4. JSON tab → paste the entire contents of
   `.aws/policies/skypilot-minimal.json`.
5. Review → name it `KinoforgeSkypilotMinimal` → Create policy.
6. Confirm the policy is now attached to `kinoforge-ci`.

Leave `AmazonS3FullAccess` attached until `pixi run cloud:perms-probe`
exits 0 against AWS (Layer W+α T3). Once green, detach
`AmazonS3FullAccess`:

> Users → `kinoforge-ci` → Permissions → checkbox `AmazonS3FullAccess` →
> Remove.

The scoped policy covers all S3 operations kinoforge needs against the
`kinoforge-realcloud-tests-*` and `skypilot-*` prefixes; broader S3
access is no longer required.

## Rotation

Access keys age. AWS recommends rotation every 90 days.

- AWS Console → IAM → Users → kinoforge-ci → Security credentials.
- Create a NEW access key first, paste into `credentials`, verify, THEN
  deactivate + delete the old one.
