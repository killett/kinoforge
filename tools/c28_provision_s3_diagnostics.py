"""C28 A1 — provision the S3 bucket + scoped IAM policy for diagnostic uploads.

Idempotent: safe to re-run; only acts when state diverges.

Bucket: ``kinoforge-pod-diagnostics`` in ``us-west-2`` (per
``feedback_default_region_oregon``). 7-day lifecycle on the ``boot-logs/``
prefix keeps storage cost ~$0 even at sustained capture volume. IAM policy
``kinoforge-c28-diag-put`` grants ``s3:PutObject`` only, scoped to that
prefix, attached to the ``kinoforge-ci`` user so RunPod-side pods using
that key can upload boot logs but cannot list or read existing objects.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import ClientError

BUCKET_NAME = "kinoforge-pod-diagnostics"
REGION = "us-west-2"
LIFECYCLE_PREFIX = "boot-logs/"
POLICY_NAME = "kinoforge-c28-diag-put"
TARGET_USER = "kinoforge-ci"
_LIFECYCLE_RULE_ID = "expire-boot-logs-7d"


def _iam_policy_doc() -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:PutObject",
                    "Resource": (f"arn:aws:s3:::{BUCKET_NAME}/{LIFECYCLE_PREFIX}*"),
                },
            ],
        },
    )


def provision(
    *,
    s3: Any,  # noqa: ANN401
    iam: Any,  # noqa: ANN401
    account_id: str,
) -> None:
    """Bring the bucket + lifecycle + IAM policy + attachment to desired state."""
    _ensure_bucket(s3)
    _ensure_lifecycle(s3)
    _ensure_policy_and_attachment(iam, account_id)


def _ensure_bucket(s3: Any) -> None:  # noqa: ANN401
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
        return
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise
    s3.create_bucket(
        Bucket=BUCKET_NAME,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )


def _ensure_lifecycle(s3: Any) -> None:  # noqa: ANN401
    s3.put_bucket_lifecycle_configuration(
        Bucket=BUCKET_NAME,
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


def _ensure_policy_and_attachment(iam: Any, account_id: str) -> None:  # noqa: ANN401
    arn = f"arn:aws:iam::{account_id}:policy/{POLICY_NAME}"
    try:
        iam.get_policy(PolicyArn=arn)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("NoSuchEntity", "404"):
            raise
        iam.create_policy(
            PolicyName=POLICY_NAME,
            PolicyDocument=_iam_policy_doc(),
        )
    iam.attach_user_policy(UserName=TARGET_USER, PolicyArn=arn)


def main() -> None:
    """CLI entry point — runs the provisioner against the live AWS account."""
    sts = boto3.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    provision(
        s3=boto3.client("s3", region_name=REGION),
        iam=boto3.client("iam"),
        account_id=account_id,
    )
    print(
        f"OK: bucket={BUCKET_NAME} region={REGION} "
        f"policy={POLICY_NAME} user={TARGET_USER}",
    )


if __name__ == "__main__":
    main()
