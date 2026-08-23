r"""Validate the scoped AWS policy and GCP role list without launching anything.

Simulation only — `iam:SimulatePrincipalPolicy` on AWS,
`projects.testIamPermissions` on GCP. Both are free API calls. No EC2
instance, no GCE instance, no compute spend.

What this proves: the policy's own logic grants the actions kinoforge asks
for. What it does NOT prove: that a real SkyPilot launch succeeds. Simulation
cannot see an undocumented API call sky makes at launch time — see the
firewall caveat in `.gcp/policies/roles.txt`. Both artifacts stay marked
UNVALIDATED for that reason.

The AWS simulation passes are grouped by the `Resource` array of whichever
policy statement actually grants each action, read straight out of the
rendered policy document — not a hardcoded per-service carve-out. The IAM
simulator rejects a mixed list of `*` and specific ARNs in a single call, so
every distinct resource-scope in the policy gets its own pass. KMS is one
instance of this, not a special case: `.aws/policies/skypilot-minimal.
template.json` also scopes IAM role/instance-profile actions and S3 bucket
actions to specific ARNs, and those need the same treatment or they read as
falsely denied against `*` (mirrors, and generalizes, the KMS-only split
documented at `tools/cloud_perms_probe.py:201-241`).

Both cloud paths mutate/query a real API under ambient credentials and
require `--confirm-live` (or `KINOFORGE_LIVE=1`) before they run — this
tool creates and deletes a real throwaway IAM user on the AWS path, and
calls `testIamPermissions` under whatever identity this shell already
carries on the GCP path.

Usage::

    pixi run python tools/validate_scoped_policy.py \\
        --cloud aws --policy-file /tmp/skypilot-minimal.rendered.json \\
        --confirm-live
    pixi run python tools/validate_scoped_policy.py \\
        --cloud gcp --project <project-id> --confirm-live
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# A bare `python tools/validate_scoped_policy.py` invocation (the form this
# module's own docstring documents) puts only tools/ on sys.path, not the
# repo root -- so the `tools.cloud_perms_probe` import below would otherwise
# raise ModuleNotFoundError before argparse ever runs. Mirrors the bootstrap
# in `tools/cloud_perms_probe.py:33-35`.
_REPO_ROOT_PATH: Path = Path(__file__).resolve().parent.parent
_REPO_ROOT: str = str(_REPO_ROOT_PATH)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.cloud_perms_probe import _REQUIRED_AWS_ACTIONS  # noqa: E402

_log = logging.getLogger(__name__)

# Counterpart to .aws/policies/skypilot-minimal.template.json -- the artifact
# this tool's --cloud gcp path actually validates. Parsed at call time
# (_read_gcp_roles) rather than hardcoded, so the printed role set can never
# drift from what the tracked file grants.
_GCP_ROLES_PATH: Path = _REPO_ROOT_PATH / ".gcp" / "policies" / "roles.txt"

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


def _read_gcp_roles(path: Path = _GCP_ROLES_PATH) -> list[str]:
    """Parse the granted role list out of the tracked roles.txt artifact.

    Args:
        path: Path to the newline-delimited role file. Defaults to
            `.gcp/policies/roles.txt`, the artifact this tool validates.

    Returns:
        Role strings (e.g. `roles/compute.instanceAdmin.v1`), with blank
        lines and `#`-comments stripped.
    """
    lines = path.read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _resource_key_for_action(
    statements: list[dict[str, Any]], action: str
) -> tuple[str, ...] | None:
    """Return the resource ARNs the policy scopes *action* to, or None for "*".

    Args:
        statements: The rendered policy's `Statement` array.
        action: A single IAM action string, e.g. `iam:CreateRole`.

    Returns:
        A tuple of resource ARNs if the granting statement scopes the
        action to specific resources, or None if it grants `"*"` — or if
        no `Allow` statement grants the action at all. The latter is
        intentional: simulating a non-granted action against `"*"` still
        correctly reports it as denied, rather than raising and hiding the
        real gap.
    """
    for stmt in statements:
        if stmt.get("Effect") != "Allow":
            continue
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if not any(fnmatch.fnmatchcase(action, pattern) for pattern in actions):
            continue
        resource = stmt.get("Resource", "*")
        if resource == "*":
            return None
        if isinstance(resource, str):
            resource = [resource]
        return tuple(resource)
    return None


def _reduce_decisions(results: list[dict[str, Any]]) -> dict[str, str]:
    """Collapse per-(action, resource) EvaluationResults into one decision per action.

    A single action appears once per resource ARN in `ResourceArns` when
    that list has more than one entry — real IAM returns one
    `EvaluationResult` per (action, resource) pair, not one per action.
    Reducing with a plain last-write-wins dict comprehension would let an
    `allowed` decision against one resource silently mask an
    `implicitDeny` against another. An action counts as denied here if ANY
    evaluated resource denies it.

    Args:
        results: The `EvaluationResults` list from one simulate call.

    Returns:
        Dict of action name -> `"allowed"`, or the first non-allowed
        decision seen for that action.
    """
    decisions: dict[str, list[str]] = {}
    for entry in results:
        decisions.setdefault(entry["EvalActionName"], []).append(entry["EvalDecision"])
    reduced: dict[str, str] = {}
    for action, ds in decisions.items():
        non_allowed = [d for d in ds if d != "allowed"]
        reduced[action] = non_allowed[0] if non_allowed else "allowed"
    return reduced


def validate_aws(
    iam: Any,  # noqa: ANN401
    *,
    policy_document: str,
    user_name: str,
    required_actions: tuple[str, ...] = _REQUIRED_AWS_ACTIONS,
) -> dict[str, Any]:
    """Attach *policy_document* to a throwaway user and simulate every action.

    The user is created bare and carries only the inline policy under test,
    so the simulation reflects that policy alone rather than the union with
    whatever the caller already holds. Required actions are grouped by the
    resource set of the policy statement that grants them (see module
    docstring) and simulated one pass per group.

    Args:
        iam: IAM client (injected so tests need no SDK).
        policy_document: Rendered policy JSON — placeholders must already
            be substituted; the CLI enforces this before calling in.
        user_name: Throwaway IAM user name.
        required_actions: Actions to simulate.

    Returns:
        Dict with `exit_code`, `denied`, `missing`, and `simulated`.
        `exit_code` is 0 only when `denied` and `missing` are both empty.

    Raises:
        Exception: Re-raises any client error after deleting the user.
        RuntimeError: A simulate call reports `IsTruncated: True`.
            Pagination is not implemented, so proceeding would silently
            evaluate only part of the action list.
    """
    statements: list[dict[str, Any]] = json.loads(policy_document).get("Statement", [])
    created = iam.create_user(UserName=user_name)
    principal_arn = created["User"]["Arn"]
    policy_name = "KinoforgeScopeProbe"
    try:
        iam.put_user_policy(
            UserName=user_name,
            PolicyName=policy_name,
            PolicyDocument=policy_document,
        )

        groups: dict[tuple[str, ...] | None, list[str]] = {}
        for action in required_actions:
            key = _resource_key_for_action(statements, action)
            groups.setdefault(key, []).append(action)

        simulated: dict[str, str] = {}
        for resource_key, actions in groups.items():
            if not actions:
                continue
            call_kwargs: dict[str, Any] = {
                "PolicySourceArn": principal_arn,
                "ActionNames": actions,
            }
            if resource_key is not None:
                call_kwargs["ResourceArns"] = list(resource_key)
            sim = iam.simulate_principal_policy(**call_kwargs)
            if sim.get("IsTruncated"):
                raise RuntimeError(
                    "simulate_principal_policy returned IsTruncated=True for "
                    f"actions {actions}; pagination is not implemented here, "
                    "so results would silently be incomplete"
                )
            simulated.update(_reduce_decisions(sim["EvaluationResults"]))
    finally:
        # A leaked probe user is a standing liability; delete it on every
        # path. Both deletes are individually guarded so a cleanup failure
        # is logged, never raised in place of (and masking) whatever
        # exception the `try` block above is already propagating.
        try:
            iam.delete_user_policy(UserName=user_name, PolicyName=policy_name)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "failed to delete inline policy %s from throwaway user %s: %s",
                policy_name,
                user_name,
                exc,
            )
        try:
            iam.delete_user(UserName=user_name)
        except Exception as exc:  # noqa: BLE001
            _log.warning("failed to delete throwaway IAM user %s: %s", user_name, exc)

    denied = sorted(a for a, d in simulated.items() if d != "allowed")
    missing = sorted(set(required_actions) - set(simulated))
    return {
        "exit_code": 1 if (denied or missing) else 0,
        "denied": denied,
        "missing": missing,
        "simulated": simulated,
    }


def validate_gcp(
    projects_client: Any,  # noqa: ANN401
    *,
    project: str,
    permissions: list[str] | tuple[str, ...] = GCP_REQUIRED_PERMISSIONS,
    caller_identity: str | None = None,
) -> dict[str, Any]:
    """Test which of *permissions* the calling identity holds on *project*.

    `testIamPermissions` evaluates the AUTHENTICATED CALLER, not
    necessarily the runner service account `.gcp/policies/roles.txt` is
    meant for — from ambient operator credentials this can false-green
    regardless of what the runner SA actually holds. *caller_identity* is
    carried through into the result precisely so that mismatch is visible
    rather than silent, mirroring the identity capture at
    `tools/cloud_perms_probe.py:209-215`.

    Args:
        projects_client: resourcemanager ProjectsClient (injected).
        project: GCP project id.
        permissions: Permissions to test.
        caller_identity: The resolved identity the test actually ran as
            (e.g. a service-account email), for the caller to record
            alongside the result. Not independently verified here.

    Returns:
        Dict with `exit_code`, `missing`, `granted`, and `caller_identity`.
    """
    wanted = list(permissions)
    response = projects_client.test_iam_permissions(
        resource=f"projects/{project}", permissions=wanted
    )
    granted = list(response.permissions)
    missing = [p for p in wanted if p not in granted]
    return {
        "exit_code": 1 if missing else 0,
        "missing": missing,
        "granted": granted,
        "caller_identity": caller_identity,
    }


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
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help=(
            "required to proceed -- the aws path creates and deletes a real "
            "IAM user, the gcp path calls testIamPermissions under this "
            "shell's ambient credentials. KINOFORGE_LIVE=1 has the same "
            "effect."
        ),
    )
    args = parser.parse_args(argv)

    confirmed = args.confirm_live or os.environ.get("KINOFORGE_LIVE") == "1"
    if not confirmed:
        parser.error(
            "refusing to run against a real cloud without --confirm-live or "
            "KINOFORGE_LIVE=1 -- see the module docstring in "
            "tools/validate_scoped_policy.py"
        )

    if args.cloud == "aws":
        if not args.policy_file:
            parser.error("--policy-file is required for --cloud aws (render it first)")

        policy_text = Path(args.policy_file).read_text()

        from tools.render_aws_policy import _PLACEHOLDER_RE

        # Two-tier check, mirroring tools/render_aws_policy.py's own
        # post-substitution guard: the named-placeholder regex first, then a
        # generic "<" backstop. The regex alone is not enough -- it is
        # [A-Z_]+ only, so it does not match <S3_BUCKET_PREFIX> (the digit
        # in "S3" breaks the class), and an IAM policy document has no
        # legitimate use for '<' regardless of what's inside it.
        named_survivors = sorted(set(_PLACEHOLDER_RE.findall(policy_text)))
        if named_survivors:
            parser.error(
                f"{args.policy_file} still has unrendered placeholder(s): "
                f"{', '.join(named_survivors)} -- render it first with "
                "tools/render_aws_policy.py; refusing to simulate a template"
            )
        if "<" in policy_text:
            parser.error(
                f"{args.policy_file} contains an unrendered '<' placeholder "
                "that the named-placeholder pattern did not match (e.g. a "
                "digit or lowercase letter in the name) -- render it first "
                "with tools/render_aws_policy.py; refusing to simulate a "
                "template"
            )

        import boto3

        result = validate_aws(
            boto3.client("iam"),
            policy_document=policy_text,
            user_name=args.user_name,
        )
        print(json.dumps({"policy_file": args.policy_file, **result}, indent=2))  # noqa: T201
        return int(result["exit_code"])

    if not args.project:
        parser.error("--project is required for --cloud gcp")

    import google.auth
    from google.cloud import resourcemanager_v3

    credentials, _default_project = google.auth.default()
    caller_identity = (
        getattr(credentials, "service_account_email", None)
        or "unknown (user credentials, not a service account)"
    )

    result = validate_gcp(
        resourcemanager_v3.ProjectsClient(),
        project=args.project,
        caller_identity=caller_identity,
    )
    print(json.dumps({"roles": _read_gcp_roles(), **result}, indent=2))  # noqa: T201
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
