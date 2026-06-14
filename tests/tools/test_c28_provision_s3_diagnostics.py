"""Unit tests for the C28 S3 + IAM diagnostics-bucket provisioner."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from tools.c28_provision_s3_diagnostics import (
    BUCKET_NAME,
    LIFECYCLE_PREFIX,
    POLICY_NAME,
    REGION,
    TARGET_USER,
    provision,
)


def _not_found(op: str = "head_bucket") -> ClientError:
    return ClientError({"Error": {"Code": "404"}}, op)


def test_provision_creates_bucket_when_absent() -> None:
    s3 = MagicMock()
    s3.head_bucket.side_effect = _not_found()
    iam = MagicMock()
    iam.get_policy.side_effect = _not_found("get_policy")

    provision(s3=s3, iam=iam, account_id="123456789012")

    s3.create_bucket.assert_called_once_with(
        Bucket=BUCKET_NAME,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )


def test_provision_idempotent_when_bucket_present() -> None:
    s3 = MagicMock()
    s3.head_bucket.return_value = {}
    iam = MagicMock()
    iam.get_policy.return_value = {
        "Policy": {"Arn": f"arn:aws:iam::123456789012:policy/{POLICY_NAME}"},
    }

    provision(s3=s3, iam=iam, account_id="123456789012")

    s3.create_bucket.assert_not_called()
    iam.create_policy.assert_not_called()


def test_provision_sets_7_day_lifecycle() -> None:
    s3 = MagicMock()
    s3.head_bucket.return_value = {}
    iam = MagicMock()
    iam.get_policy.return_value = {
        "Policy": {"Arn": f"arn:aws:iam::123456789012:policy/{POLICY_NAME}"},
    }

    provision(s3=s3, iam=iam, account_id="123456789012")

    call = s3.put_bucket_lifecycle_configuration.call_args
    rules = call.kwargs["LifecycleConfiguration"]["Rules"]
    assert any(
        r["Filter"]["Prefix"] == LIFECYCLE_PREFIX and r["Expiration"]["Days"] == 7
        for r in rules
    )


def test_provision_iam_policy_is_putobject_only() -> None:
    s3 = MagicMock()
    s3.head_bucket.return_value = {}
    iam = MagicMock()
    iam.get_policy.side_effect = ClientError(
        {"Error": {"Code": "NoSuchEntity"}},
        "get_policy",
    )

    provision(s3=s3, iam=iam, account_id="123456789012")

    call = iam.create_policy.call_args
    doc = json.loads(call.kwargs["PolicyDocument"])
    actions: set[str] = set()
    for stmt in doc["Statement"]:
        raw = stmt["Action"]
        actions.update(raw if isinstance(raw, list) else [raw])
    assert actions == {"s3:PutObject"}


def test_provision_attaches_policy_to_kinoforge_ci() -> None:
    s3 = MagicMock()
    s3.head_bucket.return_value = {}
    iam = MagicMock()
    iam.get_policy.return_value = {
        "Policy": {"Arn": f"arn:aws:iam::123456789012:policy/{POLICY_NAME}"},
    }

    provision(s3=s3, iam=iam, account_id="123456789012")

    iam.attach_user_policy.assert_called_once_with(
        UserName=TARGET_USER,
        PolicyArn=f"arn:aws:iam::123456789012:policy/{POLICY_NAME}",
    )


def test_provision_resource_arn_is_scoped_to_boot_logs_prefix() -> None:
    s3 = MagicMock()
    s3.head_bucket.return_value = {}
    iam = MagicMock()
    iam.get_policy.side_effect = ClientError(
        {"Error": {"Code": "NoSuchEntity"}},
        "get_policy",
    )

    provision(s3=s3, iam=iam, account_id="123456789012")

    call = iam.create_policy.call_args
    doc = json.loads(call.kwargs["PolicyDocument"])
    resources: set[str] = set()
    for stmt in doc["Statement"]:
        raw = stmt["Resource"]
        resources.update(raw if isinstance(raw, list) else [raw])
    expected = f"arn:aws:s3:::{BUCKET_NAME}/{LIFECYCLE_PREFIX}*"
    assert resources == {expected}, (
        f"policy must be scoped to {expected!r} only, got {resources!r}"
    )
