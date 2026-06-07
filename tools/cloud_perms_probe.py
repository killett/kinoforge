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


def _aws_existing_quota_case(sq_client: Any, service: str, code: str) -> str | None:  # noqa: ANN401
    """Return the case id of an open quota-increase request for ``code``, if any.

    Calls ``ListRequestedServiceQuotaChangeHistory`` and filters for entries
    matching ``code`` whose ``Status`` is one of PENDING / CASE_OPENED /
    APPROVED. Returns None on any error (so the gap path falls through to
    submission, which AWS itself dedupes server-side if a case truly exists).
    """
    try:
        resp = sq_client.list_requested_service_quota_change_history(
            ServiceCode=service,
        )
    except Exception:  # noqa: BLE001
        return None
    for entry in resp.get("RequestedQuotas", []):
        if entry.get("QuotaCode") == code and entry.get("Status") in (
            "PENDING",
            "CASE_OPENED",
            "APPROVED",
        ):
            return str(entry["Id"])
    return None


def _aws_submit_quota_request(
    sq_client: Any,  # noqa: ANN401
    service: str,
    code: str,
    target: float,
) -> str:
    """Submit a quota increase via service-quotas; return case id.

    Idempotent: if an open request for ``code`` already exists, returns
    its CaseId without submitting a new one.
    """
    existing = _aws_existing_quota_case(sq_client, service, code)
    if existing is not None:
        return existing
    resp = sq_client.request_service_quota_increase(
        ServiceCode=service,
        QuotaCode=code,
        DesiredValue=target,
    )
    return str(resp["RequestedQuota"]["Id"])


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
            case_id = _aws_submit_quota_request(
                sq,
                quota_service,
                quota_code,
                target_vcpus,
            )
            out["exit_code"] = 2
            out["quota_gap"] = {
                "code": quota_code,
                "have": quota_entry["value"],
                "want": target_vcpus,
            }
            out["quota_request"] = {
                "case_id": case_id,
                "submitted_at": _now_local_iso(),
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


_GCP_REGION = "us-central1"
_GCP_TARGET_T4_QUOTA = 1.0
_GCP_REQUIRED_ROLES: tuple[str, ...] = (
    "roles/compute.instanceAdmin.v1",
    "roles/iam.serviceAccountUser",
)


def probe_gcp(
    *,
    clients: dict[str, Any],
    project: str,
    sa_email: str,
    snapshot_path: Path | None = None,
    region: str = _GCP_REGION,
    target_t4_quota: float = _GCP_TARGET_T4_QUOTA,
    required_roles: tuple[str, ...] = _GCP_REQUIRED_ROLES,
) -> dict[str, Any]:
    """Run GCP probes; write snapshot; return exit-shaped dict.

    ``clients`` is a dict keyed by service name (``regions``, ``iam``) so
    callers can inject fakes. ``_real_gcp_clients()`` wires the real SDK.

    Args:
        clients: Dict of service-name → client object (regions, iam).
        project: GCP project ID.
        sa_email: Service account email to audit.
        snapshot_path: Override snapshot location (defaults to .gcp/perms-snapshot.json).
        region: GCP region for quota lookup.
        target_t4_quota: Minimum acceptable NVIDIA_T4_GPUS quota.
        required_roles: Roles that must be bound to the SA.

    Returns:
        Dict with captured_at, cloud, project, region, sa_email, quotas,
        sa_roles, exit_code, plus optional error/gap details on unhappy paths.
    """
    snapshot_path = snapshot_path or _GCP_SNAPSHOT_PATH
    out: dict[str, Any] = {
        "captured_at": _now_local_iso(),
        "cloud": "gcp",
        "project": project,
        "region": region,
        "sa_email": sa_email,
    }

    # Region + T4 quota lookup.
    try:
        regions_client = clients["regions"]
        region_obj = regions_client.get(project=project, region=region)
        quotas: dict[str, dict[str, Any]] = {}
        for q in region_obj.quotas:
            quotas[q.metric] = {
                "metric": q.metric,
                "limit": float(q.limit),
                "usage": float(q.usage),
            }
        out["quotas"] = quotas
    except Exception as exc:  # noqa: BLE001 — boundary
        out["exit_code"] = 1
        out["region_error"] = str(exc)
        _write_snapshot_atomic(snapshot_path, out)
        return out

    # SA role audit.
    try:
        iam_client = clients["iam"]
        policy = iam_client.get_iam_policy(resource=f"projects/{project}")
        sa_member = f"serviceAccount:{sa_email}"
        sa_roles = []
        for binding in policy.bindings:
            if sa_member in binding.members:
                sa_roles.append(binding.role)
        out["sa_roles"] = sa_roles
        missing = [r for r in required_roles if r not in sa_roles]
        if missing:
            out["exit_code"] = 1
            out["missing_roles"] = missing
            _write_snapshot_atomic(snapshot_path, out)
            return out
    except Exception as exc:  # noqa: BLE001
        out["exit_code"] = 1
        out["iam_error"] = str(exc)
        _write_snapshot_atomic(snapshot_path, out)
        return out

    # T4 quota gap?
    t4 = out["quotas"].get("NVIDIA_T4_GPUS")
    if t4 is None or t4["limit"] < target_t4_quota:
        console_url = (
            f"https://console.cloud.google.com/iam-admin/quotas"
            f"?project={project}&filter=Quota%3A%20NVIDIA_T4_GPUS"
        )
        out["exit_code"] = 2
        out["quota_gap"] = {
            "metric": "NVIDIA_T4_GPUS",
            "have": t4["limit"] if t4 else 0.0,
            "want": target_t4_quota,
            "region": region,
        }
        out["quota_request"] = {
            "console_url": console_url,
            "instructions": (
                "GCP has no SDK surface for compute-quota increases. "
                "Open the URL above, click 'EDIT QUOTAS' for NVIDIA T4 GPUs "
                f"in {region}, request limit >= {target_t4_quota:.0f}, submit. "
                "Re-run `pixi run cloud:perms-probe --cloud gcp` after approval lands."
            ),
        }
        _write_snapshot_atomic(snapshot_path, out)
        return out

    out["exit_code"] = 0
    _write_snapshot_atomic(snapshot_path, out)
    return out


def _real_gcp_clients() -> dict[str, Any]:
    """Return a {regions, iam} dict of real google.cloud clients."""
    from google.cloud import compute_v1, resourcemanager_v3

    return {
        "regions": compute_v1.RegionsClient(),
        "iam": resourcemanager_v3.ProjectsClient(),
    }


def _resolve_gcp_project_and_sa() -> tuple[str, str]:
    """Read project + SA email from .gcp/kinoforge-sa.json (default chain)."""
    sa_path = Path(_REPO_ROOT) / ".gcp" / "kinoforge-sa.json"
    sa_doc = json.loads(sa_path.read_text())
    return sa_doc["project_id"], sa_doc["client_email"]


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
        try:
            project, sa_email = _resolve_gcp_project_and_sa()
            clients = _real_gcp_clients()
            result = probe_gcp(clients=clients, project=project, sa_email=sa_email)
        except Exception as exc:  # noqa: BLE001 — boundary
            print(f"[gcp] bootstrap error: {exc}")
            exit_codes.append(1)
        else:
            print(f"[gcp] exit={result['exit_code']}")
            if "missing_roles" in result:
                print(f"[gcp] missing roles: {result['missing_roles']}")
            if "region_error" in result:
                print(f"[gcp] region error: {result['region_error']}")
            if "iam_error" in result:
                print(f"[gcp] iam error: {result['iam_error']}")
            if "quota_gap" in result:
                print(f"[gcp] quota gap: {result['quota_gap']}")
            exit_codes.append(result["exit_code"])

    return max(exit_codes) if exit_codes else 0


if __name__ == "__main__":
    raise SystemExit(main())
