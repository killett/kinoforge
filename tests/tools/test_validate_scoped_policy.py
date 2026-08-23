"""Tests for the scoped-policy validator.

SDK-free by construction: every client is a fake, mirroring the injection
style of `tests/tools/test_cloud_perms_probe.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tools.cloud_perms_probe import _REQUIRED_AWS_ACTIONS
from tools.validate_scoped_policy import (
    GCP_REQUIRED_PERMISSIONS,
    _read_gcp_roles,
    main,
    validate_aws,
    validate_gcp,
)

_ACCOUNT = "9" + "18273645" + "019"
_KEY_ARN = (
    f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/"
    + "4b0dbe0c-"
    + "3a76-401a-ac2e-d0d949b9fa3e"
)
_BUCKET_PREFIX = "bkt"


def _sample_policy_document() -> str:
    """Build a policy fixture whose resource-scoping mirrors the real template.

    Deliberately mirrors `.aws/policies/skypilot-minimal.template.json`'s
    statement shape -- wildcard EC2/ServiceQuotas, resource-scoped
    IAM/S3/KMS -- covering every action in `_REQUIRED_AWS_ACTIONS` exactly
    once, so the grouping tests exercise the same shape the real template
    has rather than a simplified stand-in.
    """
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "EC2LifecycleWrite",
                    "Effect": "Allow",
                    "Action": [
                        "ec2:RunInstances",
                        "ec2:TerminateInstances",
                        "ec2:CreateTags",
                        "ec2:CreateSecurityGroup",
                        "ec2:CreateVpc",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "EC2LifecycleRead",
                    "Effect": "Allow",
                    "Action": ["ec2:Describe*"],
                    "Resource": "*",
                },
                {
                    "Sid": "IAMForSkyPilotRoles",
                    "Effect": "Allow",
                    "Action": [
                        "iam:CreateRole",
                        "iam:CreateInstanceProfile",
                        "iam:PassRole",
                    ],
                    "Resource": [
                        f"arn:aws:iam::{_ACCOUNT}:role/skypilot-*",
                        f"arn:aws:iam::{_ACCOUNT}:instance-profile/skypilot-*",
                    ],
                },
                {
                    "Sid": "ServiceQuotas",
                    "Effect": "Allow",
                    "Action": [
                        "servicequotas:GetServiceQuota",
                        "servicequotas:RequestServiceQuotaIncrease",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "S3KinoforgeBuckets",
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:GetObject"],
                    "Resource": [
                        f"arn:aws:s3:::{_BUCKET_PREFIX}-*",
                        f"arn:aws:s3:::{_BUCKET_PREFIX}-*/*",
                    ],
                },
                {
                    "Sid": "KMSLayerW",
                    "Effect": "Allow",
                    "Action": ["kms:Encrypt", "kms:Decrypt"],
                    "Resource": [_KEY_ARN],
                },
            ],
        }
    )


class _FakeIam:
    """Fake IAM client recording calls and returning canned decisions.

    Real `simulate_principal_policy` returns one `EvaluationResult` per
    (action, resource) pair when `ResourceArns` has more than one entry --
    not one per action -- so this fake does the same. `decisions` keys may
    be a bare action name (applies to every resource) or an
    `(action, resource)` tuple for per-resource overrides.
    """

    def __init__(self, decisions: dict[Any, str], *, raise_on_simulate: bool = False):
        self.decisions = decisions
        self.raise_on_simulate = raise_on_simulate
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.put_calls: list[dict[str, Any]] = []
        self.simulate_calls: list[dict[str, Any]] = []

    def create_user(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.created.append(kwargs["UserName"])
        return {"User": {"Arn": f"arn:aws:iam::{_ACCOUNT}:user/{kwargs['UserName']}"}}

    def put_user_policy(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.put_calls.append(kwargs)
        return {}

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.simulate_calls.append(kwargs)
        if self.raise_on_simulate:
            raise RuntimeError("simulate exploded")
        resources = kwargs.get("ResourceArns") or [None]
        results = []
        for action in kwargs["ActionNames"]:
            for resource in resources:
                decision = self.decisions.get(
                    (action, resource), self.decisions.get(action, "allowed")
                )
                results.append(
                    {
                        "EvalActionName": action,
                        "EvalDecision": decision,
                        "EvalResourceName": resource,
                    }
                )
        return {"EvaluationResults": results}

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
        iam, policy_document=_sample_policy_document(), user_name="probe"
    )
    assert result["exit_code"] == 0
    assert result["denied"] == []
    assert result["missing"] == []


def test_denied_actions_are_reported_by_name() -> None:
    """An operator needs to know WHICH action the scoped policy misses.

    A bug that would fail this: reporting a bare boolean, which turns the
    fix into guesswork across 15 actions.
    """
    iam = _FakeIam(decisions={"ec2:RunInstances": "implicitDeny"})
    result = validate_aws(
        iam, policy_document=_sample_policy_document(), user_name="probe"
    )
    assert result["exit_code"] == 1
    assert result["denied"] == ["ec2:RunInstances"]


def test_kms_actions_are_simulated_against_the_key_arn_from_the_policy() -> None:
    """KMS needs its own pass -- the simulator rejects a mixed resource list.

    The KMS resource ARN is now derived from the policy document's own
    KMSLayerW statement, not a separately-injected kms_key_arn parameter --
    so the ARN actually simulated can never drift from what the policy
    under test says. Mirrors `tools/cloud_perms_probe.py:201-241`.
    """
    iam = _FakeIam(decisions={})
    validate_aws(iam, policy_document=_sample_policy_document(), user_name="probe")

    kms_calls = [c for c in iam.simulate_calls if "kms:Encrypt" in c["ActionNames"]]
    assert len(kms_calls) == 1
    assert kms_calls[0]["ResourceArns"] == [_KEY_ARN]
    assert set(kms_calls[0]["ActionNames"]) == {"kms:Encrypt", "kms:Decrypt"}


def test_resource_scoped_non_kms_action_gets_a_resource_scoped_pass() -> None:
    """A non-KMS resource-scoped action must not be simulated against "*".

    Pins the fix for the false-denial bug review caught: `iam:CreateRole`
    is scoped to role/instance-profile ARNs in the real template, not "*".
    A validator that only special-cases kms:* actions and wildcards
    everything else -- what shipped first -- would put this call in the
    wildcard pass with no ResourceArns key at all, so
    `kms_calls[0]["ResourceArns"]` below would KeyError.
    """
    iam = _FakeIam(decisions={})
    validate_aws(iam, policy_document=_sample_policy_document(), user_name="probe")

    iam_calls = [c for c in iam.simulate_calls if "iam:CreateRole" in c["ActionNames"]]
    assert len(iam_calls) == 1
    assert set(iam_calls[0]["ResourceArns"]) == {
        f"arn:aws:iam::{_ACCOUNT}:role/skypilot-*",
        f"arn:aws:iam::{_ACCOUNT}:instance-profile/skypilot-*",
    }
    assert set(iam_calls[0]["ActionNames"]) == {
        "iam:CreateRole",
        "iam:CreateInstanceProfile",
        "iam:PassRole",
    }

    wildcard_calls = [c for c in iam.simulate_calls if "ResourceArns" not in c]
    assert not any("iam:CreateRole" in c["ActionNames"] for c in wildcard_calls)


def test_all_required_actions_are_simulated() -> None:
    """Every action in `_REQUIRED_AWS_ACTIONS` must actually get simulated.

    A bug that would fail this: dropping actions while building resource
    groups, or a truncated response silently under-populating `simulated`
    -- either way a false green having evaluated only a subset.
    """
    iam = _FakeIam(decisions={})
    result = validate_aws(
        iam, policy_document=_sample_policy_document(), user_name="probe"
    )
    assert set(result["simulated"]) == set(_REQUIRED_AWS_ACTIONS)
    assert result["missing"] == []


def test_a_deny_on_one_resource_is_not_masked_by_an_allow_on_another() -> None:
    """One denied resource in a scoped group must still surface as denied.

    A bug that would fail this: reducing EvaluationResults with a plain
    dict comprehension (last-write-wins), which lets an `allowed` decision
    against the second resource in ResourceArns silently overwrite an
    `implicitDeny` against the first.
    """
    role_arn = f"arn:aws:iam::{_ACCOUNT}:role/skypilot-*"
    profile_arn = f"arn:aws:iam::{_ACCOUNT}:instance-profile/skypilot-*"
    iam = _FakeIam(
        decisions={
            ("iam:CreateRole", role_arn): "implicitDeny",
            ("iam:CreateRole", profile_arn): "allowed",
        }
    )
    result = validate_aws(
        iam, policy_document=_sample_policy_document(), user_name="probe"
    )
    assert "iam:CreateRole" in result["denied"]


def test_truncated_simulate_response_raises_loudly() -> None:
    """A truncated simulate response must not silently look like a clean pass.

    A bug that would fail this: reading only EvaluationResults and ignoring
    IsTruncated, which lets a paginated response report a false green
    having evaluated only the first page.
    """

    class _TruncatingIam(_FakeIam):
        def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            result = super().simulate_principal_policy(**kwargs)
            result["IsTruncated"] = True
            return result

    iam = _TruncatingIam(decisions={})
    with pytest.raises(RuntimeError, match="IsTruncated"):
        validate_aws(iam, policy_document=_sample_policy_document(), user_name="probe")


def test_put_user_policy_attaches_the_policy_under_test() -> None:
    """The simulation must reflect the policy under test, not a bare user.

    A bug that would fail this: removing the put_user_policy call (or
    calling it with the wrong document/principal) -- nothing else would
    catch a validator that simulates a bare/default user and stays green
    regardless of what the policy document actually says.
    """
    iam = _FakeIam(decisions={})
    policy_doc = _sample_policy_document()
    validate_aws(iam, policy_document=policy_doc, user_name="probe")

    assert len(iam.put_calls) == 1
    assert iam.put_calls[0]["UserName"] == "probe"
    assert iam.put_calls[0]["PolicyDocument"] == policy_doc

    principal_arn = f"arn:aws:iam::{_ACCOUNT}:user/probe"
    assert iam.simulate_calls
    assert all(c["PolicySourceArn"] == principal_arn for c in iam.simulate_calls)


def test_throwaway_user_is_deleted_even_when_simulation_raises() -> None:
    """A leaked probe user is a standing credential-shaped liability.

    A bug that would fail this: putting the delete after the simulate call
    without a finally, so any transient API error leaves the user behind.
    """
    iam = _FakeIam(decisions={}, raise_on_simulate=True)
    with pytest.raises(RuntimeError, match="simulate exploded"):
        validate_aws(iam, policy_document=_sample_policy_document(), user_name="probe")
    assert iam.deleted == ["probe"]


def test_delete_user_failure_does_not_mask_the_original_exception() -> None:
    """A failed cleanup call must not replace the real error the operator needs.

    A bug that would fail this: leaving `iam.delete_user(...)` unguarded in
    the finally block, so a delete failure there raises and replaces the
    original simulate exception instead of merely being logged.
    """

    class _FailDeleteIam(_FakeIam):
        def delete_user(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            raise RuntimeError("delete_user exploded too")

    iam = _FailDeleteIam(decisions={}, raise_on_simulate=True)
    with pytest.raises(RuntimeError, match="simulate exploded"):
        validate_aws(iam, policy_document=_sample_policy_document(), user_name="probe")


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
    assert result == {
        "exit_code": 0,
        "missing": [],
        "granted": required,
        "caller_identity": None,
    }


def test_gcp_result_carries_the_resolved_caller_identity() -> None:
    """A false green must be attributable to a caller, not silent.

    `testIamPermissions` evaluates the AUTHENTICATED CALLER, not
    necessarily the runner service account the roles are meant for. A bug
    that would fail this: dropping caller_identity from the result, which
    makes a false green (ambient creds richer than the runner SA)
    invisible in the output.
    """
    required = list(GCP_REQUIRED_PERMISSIONS)
    client = _FakeProjects(granted=set(required))
    result = validate_gcp(
        client,
        project="kinoforge-prod-deadbeef",
        permissions=required,
        caller_identity="probe@example.iam.gserviceaccount.com",
    )
    assert result["caller_identity"] == "probe@example.iam.gserviceaccount.com"


def test_read_gcp_roles_includes_storage_admin() -> None:
    """The printed role set must be the one .gcp/policies/roles.txt actually grants.

    A bug that would fail this: printing the two-role `_GCP_REQUIRED_ROLES`
    probe tuple instead, which is missing `roles/storage.admin` -- the
    role that actually supplies the storage.* permissions
    `GCP_REQUIRED_PERMISSIONS` tests for.
    """
    roles = _read_gcp_roles()
    assert "roles/storage.admin" in roles
    assert "roles/compute.instanceAdmin.v1" in roles
    assert "roles/iam.serviceAccountUser" in roles
    assert all(not r.startswith("#") for r in roles)


def test_main_refuses_without_confirm_live(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI must not touch a real cloud without an explicit flag.

    A bug that would fail this: reaching `import boto3` / client
    construction under only ambient credentials as the gate -- exactly the
    accident that produced a real CreateUser/DeleteUser call during this
    tool's own self-review.
    """
    monkeypatch.delenv("KINOFORGE_LIVE", raising=False)
    with pytest.raises(SystemExit):
        main(["--cloud", "aws", "--policy-file", "/dev/null"])
    assert "confirm-live" in capsys.readouterr().err


def test_main_aws_missing_policy_file_errors_with_confirm_live(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--policy-file is still required once the live gate is satisfied."""
    monkeypatch.delenv("KINOFORGE_LIVE", raising=False)
    with pytest.raises(SystemExit):
        main(["--cloud", "aws", "--confirm-live"])
    assert "--policy-file" in capsys.readouterr().err


def test_main_aws_refuses_an_unrendered_policy_template(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A template with a surviving placeholder must not reach create_user.

    A bug that would fail this: skipping the placeholder scan and passing
    the raw template straight to put_user_policy -- either the AWS
    boundary 400s after a real throwaway user already exists, or worse,
    the simulation silently evaluates a policy that grants nothing because
    every Resource entry is a literal, non-matching "<AWS_ACCOUNT>" string.
    """
    monkeypatch.delenv("KINOFORGE_LIVE", raising=False)
    unrendered = tmp_path / "unrendered.json"
    unrendered.write_text(
        '{"Statement": [{"Resource": "arn:aws:s3:::<S3_BUCKET_PREFIX>-*"}]}'
    )
    with pytest.raises(SystemExit):
        main(["--cloud", "aws", "--confirm-live", "--policy-file", str(unrendered)])
    assert "placeholder" in capsys.readouterr().err


def test_main_gcp_missing_project_errors_with_confirm_live(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--project is still required once the live gate is satisfied."""
    monkeypatch.delenv("KINOFORGE_LIVE", raising=False)
    with pytest.raises(SystemExit):
        main(["--cloud", "gcp", "--confirm-live"])
    assert "--project" in capsys.readouterr().err
