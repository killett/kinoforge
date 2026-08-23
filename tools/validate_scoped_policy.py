"""Validate the scoped AWS policy and GCP role list without launching anything.

Simulation only — `iam:SimulatePrincipalPolicy` on AWS,
`projects.testIamPermissions` on GCP. Both are free API calls. No EC2
instance, no GCE instance, no compute spend.

What this proves: the policy's own logic grants the actions kinoforge asks
for. What it does NOT prove: that a real SkyPilot launch succeeds. Simulation
cannot see an undocumented API call sky makes at launch time — see the
firewall caveat in `.gcp/policies/roles.txt`. Both artifacts stay marked
UNVALIDATED for that reason.

Usage::

    pixi run python tools/validate_scoped_policy.py --cloud aws
    pixi run python tools/validate_scoped_policy.py --cloud gcp
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from tools.cloud_perms_probe import (
    _AWS_KMS_ACTIONS,
    _GCP_REQUIRED_ROLES,
    _REQUIRED_AWS_ACTIONS,
)

# Permissions the roles in .gcp/policies/roles.txt are claimed to supply.
# Deliberately a small, load-bearing subset: one permission per capability
# kinoforge actually exercises.
GCP_REQUIRED_PERMISSIONS: tuple[str, ...] = (
    "compute.instances.create",
    "compute.instances.delete",
    "compute.instances.list",
    "compute.disks.create",
    "iam.serviceAccounts.actAs",
    "storage.buckets.get",
    "storage.objects.create",
    "storage.objects.get",
)


def validate_aws(
    iam: Any,  # noqa: ANN401
    *,
    policy_document: str,
    user_name: str,
    kms_key_arn: str | None,
    required_actions: tuple[str, ...] = _REQUIRED_AWS_ACTIONS,
) -> dict[str, Any]:
    """Attach *policy_document* to a throwaway user and simulate every action.

    The user is created bare and carries only the inline policy under test,
    so the simulation reflects that policy alone rather than the union with
    whatever the caller already holds.

    Args:
        iam: IAM client (injected so tests need no SDK).
        policy_document: Rendered policy JSON.
        user_name: Throwaway IAM user name.
        kms_key_arn: Full KMS key ARN for the resource-scoped pass, or None.
        required_actions: Actions to simulate.

    Returns:
        Dict with `exit_code`, `denied`, and `simulated`.

    Raises:
        Exception: Re-raises any client error after deleting the user.
    """
    created = iam.create_user(UserName=user_name)
    principal_arn = created["User"]["Arn"]
    policy_name = "KinoforgeScopeProbe"
    try:
        iam.put_user_policy(
            UserName=user_name,
            PolicyName=policy_name,
            PolicyDocument=policy_document,
        )
        wildcard = [a for a in required_actions if a not in _AWS_KMS_ACTIONS]
        kms = [a for a in required_actions if a in _AWS_KMS_ACTIONS]

        # Pass 1: wildcard-resource actions. Pass 2: KMS against the key ARN.
        # The simulator rejects a mixed list of "*" and specific ARNs, so the
        # split is required, not stylistic.
        sim = iam.simulate_principal_policy(
            PolicySourceArn=principal_arn, ActionNames=wildcard
        )
        simulated: dict[str, str] = {
            e["EvalActionName"]: e["EvalDecision"] for e in sim["EvaluationResults"]
        }
        if kms:
            sim2 = iam.simulate_principal_policy(
                PolicySourceArn=principal_arn,
                ActionNames=kms,
                ResourceArns=[kms_key_arn] if kms_key_arn else ["*"],
            )
            for entry in sim2["EvaluationResults"]:
                simulated[entry["EvalActionName"]] = entry["EvalDecision"]
    finally:
        # A leaked probe user is a standing liability; delete it on every path.
        try:
            iam.delete_user_policy(UserName=user_name, PolicyName=policy_name)
        except Exception:  # noqa: BLE001, S110
            pass
        iam.delete_user(UserName=user_name)

    denied = sorted(a for a, d in simulated.items() if d != "allowed")
    return {
        "exit_code": 1 if denied else 0,
        "denied": denied,
        "simulated": simulated,
    }


def validate_gcp(
    projects_client: Any,  # noqa: ANN401
    *,
    project: str,
    permissions: list[str] | tuple[str, ...] = GCP_REQUIRED_PERMISSIONS,
) -> dict[str, Any]:
    """Test which of *permissions* the calling identity holds on *project*.

    Args:
        projects_client: resourcemanager ProjectsClient (injected).
        project: GCP project id.
        permissions: Permissions to test.

    Returns:
        Dict with `exit_code`, `missing`, and `granted`.
    """
    wanted = list(permissions)
    response = projects_client.test_iam_permissions(
        resource=f"projects/{project}", permissions=wanted
    )
    granted = list(response.permissions)
    missing = [p for p in wanted if p not in granted]
    return {"exit_code": 1 if missing else 0, "missing": missing, "granted": granted}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", choices=("aws", "gcp"), required=True)
    parser.add_argument("--policy-file", default=None, help="rendered AWS policy JSON")
    parser.add_argument("--user-name", default="kinoforge-scope-probe")
    parser.add_argument("--project", default=None, help="GCP project id")
    args = parser.parse_args(argv)

    if args.cloud == "aws":
        import boto3

        from tools.cloud_perms_probe import _load_kms_key_arn
        from tools.render_aws_policy import _POLICY_PATH

        if not args.policy_file:
            parser.error("--policy-file is required for --cloud aws (render it first)")
        result = validate_aws(
            boto3.client("iam"),
            policy_document=open(args.policy_file).read(),  # noqa: SIM115, PTH123
            user_name=args.user_name,
            kms_key_arn=_load_kms_key_arn(),
        )
        print(json.dumps({"policy_template": str(_POLICY_PATH), **result}, indent=2))  # noqa: T201
        return int(result["exit_code"])

    from google.cloud import resourcemanager_v3

    if not args.project:
        parser.error("--project is required for --cloud gcp")
    result = validate_gcp(resourcemanager_v3.ProjectsClient(), project=args.project)
    print(json.dumps({"roles": list(_GCP_REQUIRED_ROLES), **result}, indent=2))  # noqa: T201
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
