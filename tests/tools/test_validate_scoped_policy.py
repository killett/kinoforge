"""Tests for the scoped-policy validator.

SDK-free by construction: every client is a fake, mirroring the injection
style of `tests/tools/test_cloud_perms_probe.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tools.validate_scoped_policy import validate_aws, validate_gcp

_ACCOUNT = "9" + "18273645" + "019"
_KEY_ARN = (
    f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/"
    + "4b0dbe0c-"
    + "3a76-401a-ac2e-d0d949b9fa3e"
)


class _FakeIam:
    """Fake IAM client recording calls and returning canned decisions."""

    def __init__(self, decisions: dict[str, str], *, raise_on_simulate: bool = False):
        self.decisions = decisions
        self.raise_on_simulate = raise_on_simulate
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.simulate_calls: list[dict[str, Any]] = []

    def create_user(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.created.append(kwargs["UserName"])
        return {"User": {"Arn": f"arn:aws:iam::{_ACCOUNT}:user/{kwargs['UserName']}"}}

    def put_user_policy(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        return {}

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.simulate_calls.append(kwargs)
        if self.raise_on_simulate:
            raise RuntimeError("simulate exploded")
        return {
            "EvaluationResults": [
                {"EvalActionName": a, "EvalDecision": self.decisions.get(a, "allowed")}
                for a in kwargs["ActionNames"]
            ]
        }

    def delete_user_policy(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        return {}

    def delete_user(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.deleted.append(kwargs["UserName"])
        return {}


def test_all_allowed_reports_exit_zero() -> None:
    """A fully-permitted policy validates clean.

    A bug that would fail this: treating the string "allowed" as falsy, or
    comparing against "Allowed" with a capital A, which AWS does not return.
    """
    iam = _FakeIam(decisions={})
    result = validate_aws(
        iam, policy_document="{}", user_name="probe", kms_key_arn=_KEY_ARN
    )
    assert result["exit_code"] == 0
    assert result["denied"] == []


def test_denied_actions_are_reported_by_name() -> None:
    """An operator needs to know WHICH action the scoped policy misses.

    A bug that would fail this: reporting a bare boolean, which turns the
    fix into guesswork across 15 actions.
    """
    iam = _FakeIam(decisions={"ec2:RunInstances": "implicitDeny"})
    result = validate_aws(
        iam, policy_document="{}", user_name="probe", kms_key_arn=_KEY_ARN
    )
    assert result["exit_code"] == 1
    assert result["denied"] == ["ec2:RunInstances"]


def test_kms_actions_are_simulated_against_the_key_arn() -> None:
    """KMS needs its own pass — the simulator rejects a mixed resource list.

    A bug that would fail this: folding kms:Encrypt into the wildcard pass,
    which resolves to implicitDeny against a resource-scoped policy and
    reports a false failure. This is the exact two-pass split
    `tools/cloud_perms_probe.py:201-241` already documents.
    """
    iam = _FakeIam(decisions={})
    validate_aws(iam, policy_document="{}", user_name="probe", kms_key_arn=_KEY_ARN)

    kms_calls = [c for c in iam.simulate_calls if "ResourceArns" in c]
    assert len(kms_calls) == 1
    assert kms_calls[0]["ResourceArns"] == [_KEY_ARN]
    assert set(kms_calls[0]["ActionNames"]) == {"kms:Encrypt", "kms:Decrypt"}

    wildcard_calls = [c for c in iam.simulate_calls if "ResourceArns" not in c]
    assert len(wildcard_calls) == 1
    assert "kms:Encrypt" not in wildcard_calls[0]["ActionNames"]


def test_throwaway_user_is_deleted_even_when_simulation_raises() -> None:
    """A leaked probe user is a standing credential-shaped liability.

    A bug that would fail this: putting the delete after the simulate call
    without a finally, so any transient API error leaves the user behind.
    """
    iam = _FakeIam(decisions={}, raise_on_simulate=True)
    with pytest.raises(RuntimeError, match="simulate exploded"):
        validate_aws(iam, policy_document="{}", user_name="probe", kms_key_arn=_KEY_ARN)
    assert iam.deleted == ["probe"]


class _FakeProjects:
    """Fake resourcemanager client returning a fixed permission grant."""

    def __init__(self, granted: set[str]):
        self.granted = granted

    def test_iam_permissions(self, *, resource: str, permissions: list[str]) -> Any:  # noqa: ANN401
        class _Resp:
            def __init__(self, perms: list[str]):
                self.permissions = perms

        return _Resp([p for p in permissions if p in self.granted])


def test_gcp_missing_permissions_are_reported_by_name() -> None:
    """The GCP half must name what the role set does not supply.

    A bug that would fail this: asserting only on the count, which cannot
    tell an operator whether to add compute.securityAdmin or storage.admin.
    """
    required = ["compute.instances.create", "compute.firewalls.create"]
    client = _FakeProjects(granted={"compute.instances.create"})
    result = validate_gcp(
        client, project="kinoforge-prod-deadbeef", permissions=required
    )
    assert result["exit_code"] == 1
    assert result["missing"] == ["compute.firewalls.create"]


def test_gcp_all_granted_reports_exit_zero() -> None:
    """The happy path returns 0 and an empty missing list."""
    required = ["compute.instances.create"]
    client = _FakeProjects(granted=set(required))
    result = validate_gcp(
        client, project="kinoforge-prod-deadbeef", permissions=required
    )
    assert result == {"exit_code": 0, "missing": [], "granted": required}
