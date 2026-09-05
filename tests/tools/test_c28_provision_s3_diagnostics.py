"""Unit tests for the C28 S3 + IAM diagnostics-bucket provisioner."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from tools.c28_provision_s3_diagnostics import (
    LIFECYCLE_PREFIX,
    POLICY_NAME,
    REGION,
    TARGET_USER,
    _bucket_from_env,
    provision,
)

#: Explicit test double, passed in rather than imported from the tool. The
#: tool used to export a real bucket name as a constant, and a test that
#: compares that constant against itself proves nothing about scoping.
_BUCKET = "example-diag-bucket"


def _not_found(op: str = "head_bucket") -> ClientError:
    return ClientError({"Error": {"Code": "404"}}, op)


def test_provision_creates_bucket_when_absent() -> None:
    s3 = MagicMock()
    s3.head_bucket.side_effect = _not_found()
    iam = MagicMock()
    iam.get_policy.side_effect = _not_found("get_policy")

    provision(s3=s3, iam=iam, account_id="123456789012", bucket=_BUCKET)

    s3.create_bucket.assert_called_once_with(
        Bucket=_BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )


def test_provision_idempotent_when_bucket_present() -> None:
    s3 = MagicMock()
    s3.head_bucket.return_value = {}
    iam = MagicMock()
    iam.get_policy.return_value = {
        "Policy": {"Arn": f"arn:aws:iam::123456789012:policy/{POLICY_NAME}"},
    }

    provision(s3=s3, iam=iam, account_id="123456789012", bucket=_BUCKET)

    s3.create_bucket.assert_not_called()
    iam.create_policy.assert_not_called()


def test_provision_sets_7_day_lifecycle() -> None:
    s3 = MagicMock()
    s3.head_bucket.return_value = {}
    iam = MagicMock()
    iam.get_policy.return_value = {
        "Policy": {"Arn": f"arn:aws:iam::123456789012:policy/{POLICY_NAME}"},
    }

    provision(s3=s3, iam=iam, account_id="123456789012", bucket=_BUCKET)

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

    provision(s3=s3, iam=iam, account_id="123456789012", bucket=_BUCKET)

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

    provision(s3=s3, iam=iam, account_id="123456789012", bucket=_BUCKET)

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

    provision(s3=s3, iam=iam, account_id="123456789012", bucket=_BUCKET)

    call = iam.create_policy.call_args
    doc = json.loads(call.kwargs["PolicyDocument"])
    resources: set[str] = set()
    for stmt in doc["Statement"]:
        raw = stmt["Resource"]
        resources.update(raw if isinstance(raw, list) else [raw])
    expected = f"arn:aws:s3:::{_BUCKET}/{LIFECYCLE_PREFIX}*"
    assert resources == {expected}, (
        f"policy must be scoped to {expected!r} only, got {resources!r}"
    )


def test_bucket_from_env_returns_the_configured_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KINOFORGE_DIAG_BUCKET", "operator-bucket")
    assert _bucket_from_env() == "operator-bucket"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_bucket_from_env_refuses_when_unset_or_blank(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """No bucket configured → refuse, naming the variable to set.

    Bug caught: a fallback bucket name. The tool shipped with one for three
    months, and it was a real bucket in a real account — the provisioner
    would silently act on it for anyone who ran the script without reading
    it. Blank-but-present is included because `.env` files copied from
    `.env.example` carry `KINOFORGE_DIAG_BUCKET=` verbatim.
    """
    if value is None:
        monkeypatch.delenv("KINOFORGE_DIAG_BUCKET", raising=False)
    else:
        monkeypatch.setenv("KINOFORGE_DIAG_BUCKET", value)
    with pytest.raises(SystemExit, match="KINOFORGE_DIAG_BUCKET"):
        _bucket_from_env()
