"""C28 A1 — provision the S3 bucket + scoped IAM policy for diagnostic uploads.

Idempotent: safe to re-run; only acts when state diverges.

Bucket: ``$KINOFORGE_DIAG_BUCKET`` (required) in ``us-west-2`` (per
``feedback_default_region_oregon``). 7-day lifecycle on the ``boot-logs/``
prefix keeps storage cost ~$0 even at sustained capture volume. IAM policy
``kinoforge-c28-diag-put`` grants ``s3:PutObject`` only, scoped to that
prefix, attached to the ``kinoforge-ci`` user so RunPod-side pods using
that key can upload boot logs but cannot list or read existing objects.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Deliberately no bucket-name constant. The name is an account identifier;
# a literal here is exactly how it ended up in tracked source before. It is
# read from the environment at RUN time (`_bucket_from_env`, called by
# `main`), never at import, so unit tests can import this module without a
# configured bucket.
REGION = "us-west-2"
LIFECYCLE_PREFIX = "boot-logs/"
POLICY_NAME = "kinoforge-c28-diag-put"
TARGET_USER = "kinoforge-ci"
_LIFECYCLE_RULE_ID = "expire-boot-logs-7d"


def _bucket_from_env() -> str:
    """Return the diagnostics bucket name from ``KINOFORGE_DIAG_BUCKET``.

    Returns:
        The configured bucket name, whitespace-stripped.

    Raises:
        SystemExit: The variable is unset or blank. Blank counts as unset
            because a ``.env`` copied from ``.env.example`` carries
            ``KINOFORGE_DIAG_BUCKET=`` verbatim.
    """
    bucket = os.environ.get("KINOFORGE_DIAG_BUCKET", "").strip()
    if not bucket:
        raise SystemExit(
            "c28_provision_s3_diagnostics: set KINOFORGE_DIAG_BUCKET to the "
            "diagnostics bucket to provision (see .env.example)"
        )
    return bucket


def _iam_policy_doc(bucket: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:PutObject",
                    "Resource": (f"arn:aws:s3:::{bucket}/{LIFECYCLE_PREFIX}*"),
                },
            ],
        },
    )


def provision(
    *,
    s3: Any,  # noqa: ANN401
    iam: Any,  # noqa: ANN401
    account_id: str,
    bucket: str,
) -> None:
    """Bring the bucket + lifecycle + IAM policy + attachment to desired state.

    Args:
        s3: boto3 S3 client (region-bound).
        iam: boto3 IAM client.
        account_id: 12-digit AWS account id, used to build the policy ARN.
        bucket: Diagnostics bucket name. Explicit, never defaulted.
    """
    _ensure_bucket(s3, bucket)
    _ensure_lifecycle(s3, bucket)
    _ensure_policy_and_attachment(iam, account_id, bucket)


def _ensure_bucket(s3: Any, bucket: str) -> None:  # noqa: ANN401
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise
    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )


def _ensure_lifecycle(s3: Any, bucket: str) -> None:  # noqa: ANN401
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": _LIFECYCLE_RULE_ID,
                    "Status": "Enabled",
                    "Filter": {"Prefix": LIFECYCLE_PREFIX},
                    "Expiration": {"Days": 7},
                },
            ],
        },
    )


def _ensure_policy_and_attachment(
    iam: Any,  # noqa: ANN401
    account_id: str,
    bucket: str,
) -> None:
    arn = f"arn:aws:iam::{account_id}:policy/{POLICY_NAME}"
    try:
        iam.get_policy(PolicyArn=arn)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("NoSuchEntity", "404"):
            raise
        iam.create_policy(
            PolicyName=POLICY_NAME,
            PolicyDocument=_iam_policy_doc(bucket),
        )
    iam.attach_user_policy(UserName=TARGET_USER, PolicyArn=arn)


def main() -> None:
    """CLI entry point — runs the provisioner against the live AWS account."""
    bucket = _bucket_from_env()
    sts = boto3.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    provision(
        s3=boto3.client("s3", region_name=REGION),
        iam=boto3.client("iam"),
        account_id=account_id,
        bucket=bucket,
    )
    print(
        f"OK: bucket={bucket} region={REGION} policy={POLICY_NAME} user={TARGET_USER}",
    )


if __name__ == "__main__":
    main()
