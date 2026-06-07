"""Cloud perms + GPU quota probe for SkyPilot multi-cloud bootstrap.

Layer W+α. Verifies AWS + GCP perms and persists snapshots to
``.aws/perms-snapshot.json`` and ``.gcp/perms-snapshot.json``.

Usage::

    pixi run cloud:perms-probe                # both clouds
    pixi run cloud:perms-probe --cloud aws    # AWS only
    pixi run cloud:perms-probe --cloud gcp    # GCP only

Exit codes:
    0 — all probes green
    1 — auth failure or required action denied
    2 — quota gap (AWS request submitted; GCP needs operator console)

Seams (mirror tools/preflight.py) — every SDK call goes through a
factory callable so unit tests inject fakes; no real cloud in tests.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_log = logging.getLogger(__name__)

_REQUIRED_AWS_ACTIONS: tuple[str, ...] = (
    "ec2:RunInstances",
    "ec2:TerminateInstances",
    "ec2:DescribeInstances",
    "ec2:CreateTags",
    "ec2:CreateSecurityGroup",
    "ec2:CreateVpc",
    "iam:CreateRole",
    "iam:CreateInstanceProfile",
    "iam:PassRole",
    "servicequotas:GetServiceQuota",
    "servicequotas:RequestServiceQuotaIncrease",
    "s3:PutObject",
    "s3:GetObject",
    "kms:Encrypt",
    "kms:Decrypt",
)

# Running On-Demand G and VT instances (vCPU quota).
_AWS_QUOTA_CODE = "L-DB2E81BA"
_AWS_QUOTA_SERVICE = "ec2"
_AWS_TARGET_QUOTA_VCPUS = 4.0
_AWS_REGION = "us-east-1"

_AWS_SNAPSHOT_PATH = Path(_REPO_ROOT) / ".aws" / "perms-snapshot.json"
_GCP_SNAPSHOT_PATH = Path(_REPO_ROOT) / ".gcp" / "perms-snapshot.json"

# KMS key ARN used by kinoforge-ci — policy is resource-scoped, so the IAM
# simulator must be told the target resource or it resolves to implicitDeny.
_AWS_KMS_KEY_ARN_FILE = Path(_REPO_ROOT) / ".aws" / "kms-test-key.arn"

# KMS actions require a resource-specific simulation pass because
# kinoforge-ci's KMS policy is scoped to a single key ARN, not "*".
# IAM simulator rejects a mixed list of "*" + specific ARNs, so we
# must split into two separate calls and merge results.
_AWS_KMS_ACTIONS: frozenset[str] = frozenset({"kms:Encrypt", "kms:Decrypt"})


def _load_kms_key_arn() -> str | None:
    """Return the KMS key ARN from the persisted file, or None if absent."""
    if _AWS_KMS_KEY_ARN_FILE.exists():
        return _AWS_KMS_KEY_ARN_FILE.read_text().strip()
    return None


def _now_local_iso() -> str:
    """Local-timezone ISO timestamp."""
    return datetime.now().astimezone().isoformat()


def _write_snapshot_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write snapshot via temp-file + rename so a crashed probe leaves no half-write.

    Args:
        path: Destination file path. Parent directory is created if absent.
        data: Dict to serialise as JSON.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def probe_aws(
    session: Any,  # noqa: ANN401
    *,
    snapshot_path: Path | None = None,
    region: str = _AWS_REGION,
    required_actions: tuple[str, ...] = _REQUIRED_AWS_ACTIONS,
    quota_code: str = _AWS_QUOTA_CODE,
    quota_service: str = _AWS_QUOTA_SERVICE,
    target_vcpus: float = _AWS_TARGET_QUOTA_VCPUS,
    resource_arns: list[str] | None = None,
) -> dict[str, Any]:
    """Run AWS probes against ``session``; write snapshot; return exit-shaped dict.

    Args:
        session: boto3.Session-like; ``client(name)`` returns a client.
        snapshot_path: Override snapshot location (defaults to .aws/perms-snapshot.json).
        region: AWS region for all SDK clients.
        required_actions: IAM action strings to simulate against kinoforge-ci.
        quota_code: AWS Service Quotas QuotaCode for the GPU vCPU quota.
        quota_service: AWS Service Quotas ServiceCode.
        target_vcpus: Minimum acceptable quota value.
        resource_arns: Resource ARNs to pass to simulate_principal_policy.
            When None, auto-loads the KMS key ARN from .aws/kms-test-key.arn
            (needed because the kinoforge-ci KMS policy is resource-scoped).
            Pass ``["*"]`` to force wildcard-only simulation (e.g. in tests
            that do not need the KMS ARN).

    Returns:
        Dict with keys captured_at, cloud, region, identity (when reached),
        simulated, instance_type, quotas, exit_code, plus optional auth_error /
        denied / quota_gap / *_error details on the unhappy paths.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    snapshot_path = snapshot_path or _AWS_SNAPSHOT_PATH
    out: dict[str, Any] = {
        "captured_at": _now_local_iso(),
        "cloud": "aws",
        "region": region,
    }

    # IAM simulator rejects a mixed list of "*" + specific ARNs, so we split
    # into two passes: wildcard actions against "*" and KMS actions against the
    # specific key ARN (if the ARN file is present).  Results are merged.
    if resource_arns is None:
        kms_arn = _load_kms_key_arn()
        resource_arns = [kms_arn] if kms_arn else None

    try:
        sts = session.client("sts", region_name=region)
        identity = sts.get_caller_identity()
        out["identity"] = identity
    except (ClientError, BotoCoreError) as exc:
        out["exit_code"] = 1
        out["auth_error"] = str(exc)
        return out

    try:
        iam = session.client("iam", region_name=region)
        wildcard_actions = [a for a in required_actions if a not in _AWS_KMS_ACTIONS]
        kms_actions = [a for a in required_actions if a in _AWS_KMS_ACTIONS]

        # Pass 1: wildcard-resource actions against "*" (default when ResourceArns omitted).
        sim1 = iam.simulate_principal_policy(
            PolicySourceArn=identity["Arn"],
            ActionNames=wildcard_actions,
        )
        simulated: dict[str, str] = {
            entry["EvalActionName"]: entry["EvalDecision"]
            for entry in sim1["EvaluationResults"]
        }

        # Pass 2: KMS actions against the specific key ARN (if any).
        if kms_actions:
            kms_resource_arns = resource_arns or ["*"]
            sim2 = iam.simulate_principal_policy(
                PolicySourceArn=identity["Arn"],
                ActionNames=kms_actions,
                ResourceArns=kms_resource_arns,
            )
            for entry in sim2["EvaluationResults"]:
                simulated[entry["EvalActionName"]] = entry["EvalDecision"]

        out["simulated"] = simulated
        denied = [a for a, d in simulated.items() if d != "allowed"]
        if denied:
            out["exit_code"] = 1
            out["denied"] = denied
            _write_snapshot_atomic(snapshot_path, out)
            return out
    except (ClientError, BotoCoreError) as exc:
        out["exit_code"] = 1
        out["auth_error"] = f"simulate_principal_policy: {exc}"
        return out

    try:
        ec2 = session.client("ec2", region_name=region)
        types_resp = ec2.describe_instance_types(InstanceTypes=["g4dn.xlarge"])
        out["instance_type"] = {
            t["InstanceType"]: t for t in types_resp["InstanceTypes"]
        }
    except (ClientError, BotoCoreError) as exc:
        out["exit_code"] = 1
        out["instance_type_error"] = str(exc)
        _write_snapshot_atomic(snapshot_path, out)
        return out

    try:
        sq = session.client("service-quotas", region_name=region)
        q = sq.get_service_quota(ServiceCode=quota_service, QuotaCode=quota_code)
        quota_entry = {
            "code": q["Quota"]["QuotaCode"],
            "name": q["Quota"]["QuotaName"],
            "value": q["Quota"]["Value"],
        }
        out["quotas"] = {quota_code: quota_entry}
        if quota_entry["value"] < target_vcpus:
            out["exit_code"] = 2
            out["quota_gap"] = {
                "code": quota_code,
                "have": quota_entry["value"],
                "want": target_vcpus,
            }
            _write_snapshot_atomic(snapshot_path, out)
            return out
    except (ClientError, BotoCoreError) as exc:
        out["exit_code"] = 1
        out["quota_error"] = str(exc)
        _write_snapshot_atomic(snapshot_path, out)
        return out

    out["exit_code"] = 0
    _write_snapshot_atomic(snapshot_path, out)
    return out


def _real_aws_session_factory() -> Any:  # noqa: ANN401
    """Return a real boto3.Session — local import to keep tests SDK-free."""
    import boto3

    return boto3.Session()


def main(argv: list[str] | None = None) -> int:
    """CLI entry — dispatch per --cloud arg.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code: 0 green, 1 auth/denial, 2 quota gap.
    """
    parser = argparse.ArgumentParser(description="Cloud perms probe (Layer W+α).")
    parser.add_argument(
        "--cloud",
        choices=("aws", "gcp", "both"),
        default="both",
        help="Which cloud to probe (default: both).",
    )
    args = parser.parse_args(argv)

    exit_codes: list[int] = []

    if args.cloud in ("aws", "both"):
        session = _real_aws_session_factory()
        result = probe_aws(session)
        print(f"[aws] exit={result['exit_code']}")
        if "auth_error" in result:
            print(f"[aws] auth_error: {result['auth_error']}")
        if "denied" in result:
            print(f"[aws] denied: {result['denied']}")
        if "quota_gap" in result:
            print(f"[aws] quota gap: {result['quota_gap']}")
        exit_codes.append(result["exit_code"])

    if args.cloud in ("gcp", "both"):
        print("[gcp] not yet implemented (Task 4)")

    return max(exit_codes) if exit_codes else 0


if __name__ == "__main__":
    raise SystemExit(main())
