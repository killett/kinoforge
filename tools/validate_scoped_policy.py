r"""Validate the scoped AWS policy and GCP role list without launching anything.

Simulation only — `iam:SimulatePrincipalPolicy` on AWS,
`projects.testIamPermissions` on GCP. Both are free API calls. No EC2
instance, no GCE instance, no compute spend.

What this proves: the policy's own logic grants the actions kinoforge asks
for. What it does NOT prove: that a real SkyPilot launch succeeds. Simulation
cannot see an undocumented API call sky makes at launch time — see the
firewall caveat in `.gcp/policies/roles.txt`.

That gap is closed on AWS and open on GCP. `tests/live/test_scoped_policy_aws_live.py`
attached the rendered AWS policy to a throwaway principal holding nothing else
and launched a real instance under it (2026-09-04, no denials); the GCP role
list has still never gated a launch, because this workspace has no working GCP
credential. Read a clean run here as "the logic holds", not as "it will
launch" — the live test is what says the second thing.

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
require `--confirm-live` (or `KINOFORGE_VALIDATE_SCOPED_LIVE=1`) before
they run — this tool creates and deletes a real throwaway IAM user on the
AWS path, and calls `testIamPermissions` under whatever identity this
shell already carries on the GCP path. `validate_aws` carries its own copy
of this gate (a `confirm_live` parameter), so an in-process caller that
imports the function directly — skipping `main()` entirely — cannot reach
`create_user` by accident either.

`validate_aws` also distinguishes three outcome shapes that look identical
in a naive implementation but need different operator responses:

- `denied` — the policy DOES grant the action (it appears in >=1 `Allow`
  statement) but IAM denies it anyway. Almost always a real scoping bug;
  fix the ARN.
- `ungranted` — no `Allow` statement mentions the action at all, and
  nothing in the policy explains why. A human decision either way, so it
  fails the run.
- `not_applicable` — no `Allow` statement mentions the action AND the
  named statement that would have granted it is a documented *optional*
  statement that this render deliberately dropped. Reported, but NOT a
  finding and NOT a failure.

Collapsing these into one `denied` list makes an intentional gap look
like a scoping bug, and the natural "fix" — widening the policy — is the
exact failure this tool exists to prevent.

`not_applicable` exists because the DEFAULT documented onboarding path
(`.env.example`) renders without `--kms-key-id`, which makes
`render_aws_policy.render()` drop the whole `KMSLayerW` statement — a
deliberate choice so a first-time operator is not blocked on provisioning
a KMS key. Before `_CONDITIONAL_ACTIONS_BY_SID` existed, that default
path reported `ungranted: ['kms:Decrypt', 'kms:Encrypt']` and exited 1,
i.e. the documented "confirm the scope is sufficient" step failed on the
documented render. The requirement is derived from the policy under test
— but ONLY for the specific (statement Sid -> actions) pairs listed in
`_CONDITIONAL_ACTIONS_BY_SID`. Deriving it wholesale ("anything the
policy omits was not required") would make the check vacuous: a render
that dropped the S3 or EC2 statement would pass too.

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


# Actions whose REQUIREMENT is conditional on a named statement actually
# being present in the policy under test. Keyed by `Sid` deliberately, and
# hand-maintained: the whole point is that "the policy does not grant this"
# only stops being a finding for the specific, documented, optional
# statements listed here. A generic "whatever the policy omits was not
# required" rule would make this validator vacuous -- it would green-light
# a render that dropped `S3KinoforgeBuckets` or `EC2LifecycleWrite` too.
#
# KMSLayerW is the only member today: `render_aws_policy.render()` drops it
# whenever no KMS key id is available, which is what the default onboarding
# path in `.env.example` does. Its two actions are then genuinely not
# required, because the capability they serve (CMEK / Layer W bucket tests)
# is not part of the render either.
_CONDITIONAL_ACTIONS_BY_SID: dict[str, frozenset[str]] = {
    "KMSLayerW": frozenset({"kms:Encrypt", "kms:Decrypt"}),
}


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


def _lookup_action(
    statements: list[dict[str, Any]], action: str
) -> tuple[bool, tuple[str, ...] | None]:
    """Find whether *action* is granted by *statements*, and by which resources.

    Args:
        statements: A policy's `Statement` array (rendered or raw template
            — only `Action`/`Resource`/`Effect` shape is inspected, never
            ARN string contents, so unrendered `<PLACEHOLDER>` values in
            `Resource` do not affect the result).
        action: A single IAM action string, e.g. `iam:CreateRole`.

    Returns:
        `(granted, resource_key)`. `granted` is False when no `Allow`
        statement's `Action` list matches *action* at all — e.g. the
        `KMSLayerW` statement was dropped from a rendered policy because no
        KMS key id was configured. That is a real, distinct state from "the
        policy grants this but scoped to the wrong resource"; callers must
        not conflate the two (see module docstring). `resource_key` is a
        tuple of resource ARNs when the granting statement scopes the
        action to specific resources, `None` when it grants `"*"` — and
        also `None` (unused) when `granted` is False.
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
            return True, None
        if isinstance(resource, str):
            resource = [resource]
        return True, tuple(resource)
    return False, None


def _not_applicable_actions(
    statements: list[dict[str, Any]], required_actions: tuple[str, ...]
) -> set[str]:
    """Return required actions whose optional granting statement was dropped.

    An action qualifies only when BOTH hold: its `Sid` appears in
    `_CONDITIONAL_ACTIONS_BY_SID` and that `Sid` is absent from
    *statements*, AND no other statement grants the action anyway. The
    second condition matters — if a future template moved `kms:Encrypt`
    into a differently-named statement, the action is genuinely granted
    and must be simulated and judged like any other, not excused.

    Args:
        statements: The policy-under-test's `Statement` array.
        required_actions: The actions the caller asked to verify. Actions
            outside this tuple are ignored, so the result never claims
            something the caller never asked about.

    Returns:
        The subset of *required_actions* whose absence from the policy is
        explained by a deliberately dropped optional statement, and so is
        not a finding.
    """
    present_sids = {
        stmt.get("Sid") for stmt in statements if stmt.get("Effect") == "Allow"
    }
    excused: set[str] = set()
    for sid, actions in _CONDITIONAL_ACTIONS_BY_SID.items():
        if sid in present_sids:
            continue
        for action in actions:
            if action in required_actions and not _lookup_action(statements, action)[0]:
                excused.add(action)
    return excused


def _group_results_by_action(
    results: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group raw EvaluationResults by action, preserving each per-resource decision.

    A single action appears once per resource ARN in `ResourceArns` when
    that list has more than one entry — real IAM returns one
    `EvaluationResult` per (action, resource) pair, not one per action.
    Collapsing immediately to a single verdict per action (as an earlier
    version of this function did) discards which specific ARN denied: with
    a 4-ARN group, `"s3:PutObject"` in `denied` cannot tell an operator
    whether all four resources deny it or just one.

    A wildcard-scoped group is simulated with no `ResourceArns` key at
    all, so IAM returns exactly one result per action there and
    `EvalResourceName` is whatever IAM chose to echo (`"*"` in a live
    call, and `None` from the injected fakes in this repo's tests, which
    pass no resource). Such an action therefore has a single record, not
    one-per-ARN — "one per resource actually evaluated" is only literally
    true of the resource-scoped groups.

    Args:
        results: The `EvaluationResults` list from one simulate call.

    Returns:
        Dict of action name -> list of `{"resource": ..., "decision": ...}`
        records, one per `EvaluationResult` returned for that action.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in results:
        grouped.setdefault(entry["EvalActionName"], []).append(
            {
                "resource": entry.get("EvalResourceName"),
                "decision": entry["EvalDecision"],
            }
        )
    return grouped


def _verdict(records: list[dict[str, Any]]) -> str:
    """Collapse per-resource records into one verdict for an action.

    An action counts as denied here if ANY evaluated resource denies it —
    a plain last-write-wins reduction would let an `allowed` decision
    against one resource silently mask an `implicitDeny` against another.

    Args:
        records: Per-resource `{"resource", "decision"}` records for one
            action, as produced by `_group_results_by_action`.

    Returns:
        `"allowed"` if every record is allowed, else the first non-allowed
        decision seen.
    """
    non_allowed = [r["decision"] for r in records if r["decision"] != "allowed"]
    return non_allowed[0] if non_allowed else "allowed"


def validate_aws(
    iam: Any,  # noqa: ANN401
    *,
    policy_document: str,
    user_name: str,
    required_actions: tuple[str, ...] = _REQUIRED_AWS_ACTIONS,
    confirm_live: bool = False,
) -> dict[str, Any]:
    """Simulate every action against *policy_document* via a throwaway principal.

    The user is created bare — no inline policy, no attachment — and
    *policy_document* is passed to each `simulate_principal_policy` call as
    `PolicyInputList`, so the simulation reflects that policy alone rather
    than the union with whatever the caller already holds. Attaching it
    instead (`put_user_policy`, what shipped first) cannot work: IAM caps a
    user's inline policies at 2048 characters in aggregate and the real
    rendered `skypilot-minimal` policy is 3422, so the attach failed with
    `LimitExceeded` before anything was simulated. A bare principal plus
    `PolicyInputList` is equivalent in effect and has no size ceiling —
    and leaves nothing attached to leak if cleanup goes wrong.

    Required actions are grouped by the resource set of the policy
    statement that grants them (see module docstring) and simulated one
    pass per group.

    Args:
        iam: IAM client (injected so tests need no SDK).
        policy_document: Rendered policy JSON — placeholders must already
            be substituted; the CLI enforces this before calling in.
        user_name: Throwaway IAM user name.
        required_actions: Actions to simulate.
        confirm_live: Must be True (or `KINOFORGE_VALIDATE_SCOPED_LIVE=1`
            set) or this function refuses to call `create_user` at all.
            `main()` sets this after its own `--confirm-live`/env check
            passes; this parameter exists so an in-process caller that
            imports `validate_aws` directly — bypassing `main()` entirely,
            e.g. a future Task 9 test harness — cannot reach a real
            `create_user` by accident either. A fake `iam` in a unit test
            is harmless regardless, but the function cannot tell a fake
            from a real client, so the gate applies uniformly.

    Returns:
        Dict with `exit_code`, `denied`, `ungranted`, `not_applicable`,
        `missing`, `simulated`, and `detail`. `exit_code` is 0 only when
        `denied`, `ungranted`, and `missing` are all empty —
        `not_applicable` is deliberately NOT part of that test.

        - `denied`: actions the policy DOES grant (found in >=1 `Allow`
          statement) but IAM's simulator says no — almost always a real
          scoping bug (wrong ARN, wrong action name).
        - `ungranted`: actions no `Allow` statement mentions at all, with
          no documented reason. Not automatically a bug, but it fails the
          run because the caller asked to verify these actions and the
          policy is silent on them; that is worth a human decision.
        - `not_applicable`: actions no `Allow` statement mentions because
          the optional statement that would have granted them was
          deliberately dropped from this render — see
          `_CONDITIONAL_ACTIONS_BY_SID`. This is the expected steady state
          of the default `.env.example` onboarding render, which passes no
          `--kms-key-id`: `not_applicable: ['kms:Decrypt', 'kms:Encrypt']`
          with `denied: []` and rc=0. It must never be "fixed" by widening
          the policy.
        - `missing`: actions requested but never showing up in `simulated`
          at all, e.g. a response that silently returned fewer results
          than requested (distinct from `IsTruncated`, handled below by
          raising).
        - `simulated`: one collapsed verdict per action that got a result
          at all (`"allowed"` or the first non-allowed decision seen).
        - `detail`: for EVERY action in `simulated` — not just `denied`
          ones — the raw per-resource `{"resource", "decision"}` records
          that produced its verdict, so an operator can tell "all 4
          resources deny it" from "1 of 4 does", and can inspect an
          `ungranted` action's actual `"*"`-scoped decision too.

    Raises:
        Exception: Re-raises any client error after deleting the user.
        PermissionError: *confirm_live* is False and
            `KINOFORGE_VALIDATE_SCOPED_LIVE` is not `"1"`. Raised before
            `create_user` is ever called.
        RuntimeError: A simulate call reports `IsTruncated: True`.
            Pagination is not implemented, so proceeding would silently
            evaluate only part of the action list.
    """
    if not (confirm_live or os.environ.get("KINOFORGE_VALIDATE_SCOPED_LIVE") == "1"):
        raise PermissionError(
            "validate_aws() refuses to create/mutate a real IAM user without "
            "confirm_live=True or KINOFORGE_VALIDATE_SCOPED_LIVE=1 -- an "
            "in-process caller must opt in explicitly, mirroring the CLI's "
            "--confirm-live gate. See the module docstring in "
            "tools/validate_scoped_policy.py."
        )

    statements: list[dict[str, Any]] = json.loads(policy_document).get("Statement", [])
    not_applicable = _not_applicable_actions(statements, tuple(required_actions))
    created = iam.create_user(UserName=user_name)
    principal_arn = created["User"]["Arn"]
    try:
        ungranted: set[str] = set()
        groups: dict[tuple[str, ...] | None, list[str]] = {}
        for action in required_actions:
            granted, resource_key = _lookup_action(statements, action)
            if not granted:
                ungranted.add(action)
            # Actions no statement grants still get simulated (against "*",
            # via the None group) rather than skipped -- keeps `simulated` a
            # complete record of every required action for the "nothing
            # silently dropped" invariant, even though their verdict is
            # reported under `ungranted`/`not_applicable`, not `denied`.
            groups.setdefault(resource_key, []).append(action)

        detail: dict[str, list[dict[str, Any]]] = {}
        for resource_key, actions in groups.items():
            # No `if not actions: continue` guard here -- every group in
            # `groups` was built via `setdefault(...).append(...)` above,
            # so every value already has at least one action; a guard
            # against an empty list is unreachable dead code, not defence.
            call_kwargs: dict[str, Any] = {
                "PolicySourceArn": principal_arn,
                # The policy under test rides the simulate call itself
                # rather than being attached to the probe user first.
                # `put_user_policy` is capped at 2048 characters per user
                # (aggregate, and not an adjustable quota) -- the real
                # rendered skypilot-minimal policy is 3422, so the attach
                # died with `LimitExceeded` before a single action was
                # simulated. `PolicyInputList` has no such ceiling, and
                # because the probe user is created bare the effective
                # permission set is still exactly this document.
                "PolicyInputList": [policy_document],
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
            for action, records in _group_results_by_action(
                sim["EvaluationResults"]
            ).items():
                detail.setdefault(action, []).extend(records)
    finally:
        # A leaked probe user is a standing liability; delete it on every
        # path. The delete is guarded so a cleanup failure is logged, never
        # raised in place of (and masking) whatever exception the `try`
        # block above is already propagating. There is no inline policy to
        # detach first -- the document under test never leaves the simulate
        # call (see `PolicyInputList` above), so the user is bare from
        # creation to deletion and nothing can survive a partial cleanup.
        try:
            iam.delete_user(UserName=user_name)
        except Exception as exc:  # noqa: BLE001
            _log.warning("failed to delete throwaway IAM user %s: %s", user_name, exc)

    simulated: dict[str, str] = {
        action: _verdict(records) for action, records in detail.items()
    }
    denied = sorted(
        action
        for action, decision in simulated.items()
        if decision != "allowed" and action not in ungranted
    )
    # `not_applicable` is a subset of `ungranted` by construction (both
    # require "no Allow statement grants this"), so subtracting is enough
    # to move an excused action out of the failing bucket without it
    # vanishing from the report entirely.
    ungranted_result = sorted(ungranted - not_applicable)
    not_applicable_result = sorted(not_applicable)
    missing = sorted(set(required_actions) - set(simulated))
    return {
        "exit_code": 1 if (denied or ungranted_result or missing) else 0,
        "denied": denied,
        "ungranted": ungranted_result,
        "not_applicable": not_applicable_result,
        "missing": missing,
        "simulated": simulated,
        # Every simulated action's raw per-resource records, not just
        # `denied` ones -- an `ungranted` action (e.g. kms:Encrypt when
        # KMSLayerW was dropped from the render) is still simulated
        # against "*" and its decision is exactly what an operator needs
        # to see in a mixed-render situation. Keying this off `denied`
        # only, as an earlier version did, silently discarded evidence for
        # the class of action most likely to need it.
        "detail": detail,
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
            "shell's ambient credentials. KINOFORGE_VALIDATE_SCOPED_LIVE=1 "
            "has the same effect."
        ),
    )
    args = parser.parse_args(argv)

    confirmed = (
        args.confirm_live or os.environ.get("KINOFORGE_VALIDATE_SCOPED_LIVE") == "1"
    )
    if not confirmed:
        parser.error(
            "refusing to run against a real cloud without --confirm-live or "
            "KINOFORGE_VALIDATE_SCOPED_LIVE=1 -- see the module docstring in "
            "tools/validate_scoped_policy.py"
        )

    if args.cloud == "aws":
        if not args.policy_file:
            parser.error("--policy-file is required for --cloud aws (render it first)")

        policy_text = Path(args.policy_file).read_text()

        from tools.render_aws_policy import _PLACEHOLDER_RE

        # Two-tier check, mirroring tools/render_aws_policy.py's own
        # post-substitution guard: the named-placeholder regex first, then a
        # generic "<" backstop. The named regex alone is not a safe single
        # source of truth here even though render_aws_policy's own copy now
        # includes digits ([A-Z0-9_]+) -- an IAM policy document has no
        # legitimate use for '<' at all, so the backstop stays as the real
        # guarantee regardless of what shape a future placeholder takes.
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
                "hyphen or lowercase letter in the name; digits are matched "
                "by the named pattern and reported above) -- render it first "
                "with tools/render_aws_policy.py; refusing to simulate a "
                "template"
            )

        import boto3

        result = validate_aws(
            boto3.client("iam"),
            policy_document=policy_text,
            user_name=args.user_name,
            confirm_live=True,  # main()'s own gate above already confirmed
        )
        print(json.dumps({"policy_file": args.policy_file, **result}, indent=2))  # noqa: T201
        if result["not_applicable"]:
            # stderr, so the stdout payload stays machine-parseable JSON.
            print(
                "note: "
                + ", ".join(result["not_applicable"])
                + " were not required for this render -- the optional "
                "statement that grants them was deliberately dropped (no "
                "--kms-key-id). This is the expected result for a plain "
                "SkyPilot setup and is NOT a reason to widen the policy.",
                file=sys.stderr,
            )
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
