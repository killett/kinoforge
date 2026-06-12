"""CLI dispatcher for the GPU quota utilization-burn play.

See docs/superpowers/specs/2026-06-10-gpu-quota-utilization-burn-design.md
for the strategy. Subcommands:

- spinup        — provision tagged resources on AWS + GCP, write manifest.
- teardown      — read manifest, destroy everything, delete manifest.
- snapshot      — print MTD spend by service from both clouds.
- submit-quota  — submit AWS + GCP GPU quota increases with justification text.
- scan-bigquery — run a bounded BigQuery scan to add billable signal.

SDK imports are lazy per-subcommand so ``python -m tools.quota_burn --help``
stays fast and no SDK touches the env until needed. The
``test_cli_root_does_not_import_cloud_sdks`` regression test enforces that
invariant.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.quota_burn_lib import (
    Manifest,
    aws_mtd_spend,
    aws_spin_up,
    aws_submit_quota,
    aws_tear_down,
    bq_scan_with_cap,
    gcp_mtd_spend,
    gcp_spin_up,
    gcp_submit_quota,
    gcp_tear_down,
)

# Transparent .env shim — matches the kinoforge CLI convention so operators
# don't have to remember to `export` every var before invoking the tool.
# .env never overrides shell env (override=False by design).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

_MANIFEST_PATH = Path(".quota_burn/manifest.json")
_TAG = "kinoforge-quota-burn"
# AWS Service Quotas: "Running On-Demand G/VT instance vCPUs" — gates GPU launches.
_AWS_GPU_QUOTA_CODE = "L-DB2E81BA"
# Minimum to launch 1× g4dn.xlarge (= 1 GPU + 4 vCPU).
_AWS_GPU_DESIRED_VALUE = 4


def _build_gcp_clients(*, project_id: str, operator_email: str) -> Any:  # noqa: ANN401
    """Lazy-construct the real GCP clients needed for spinup / teardown.

    Reads two env vars:
    - GCP_BILLING_ACCOUNT_ID: required for spinup (budget parent). Format
      ``billingAccounts/<ID>`` or just ``<ID>`` (we prefix if missing).
    - GCP_NOTIFICATION_CHANNEL_ID: optional. Full resource name
      ``projects/<proj>/notificationChannels/<id>`` or empty string.

    Args:
        project_id: Target GCP project ID.
        operator_email: Email used for budget notifications. Currently
            attached for parity with the AWS bundle; budget plumbing reads
            ``notification_channel`` separately.

    Returns:
        An object exposing the attributes the lib helpers consume
        (``instances``, ``disks``, ``storage``, ``budgets``,
        ``billing_account``, ``notification_channel``).
    """
    from google.cloud import compute_v1
    from google.cloud import storage as _storage
    from google.cloud.billing import budgets_v1

    billing_id_raw = os.environ.get("GCP_BILLING_ACCOUNT_ID", "").strip()
    if not billing_id_raw:
        raise RuntimeError(
            "GCP_BILLING_ACCOUNT_ID env var is required for quota_burn spinup "
            "(GCP budget parent). Set it in .env to your billing account ID "
            "(format: billingAccounts/<ID> or just <ID>)."
        )
    billing_id = (
        billing_id_raw
        if billing_id_raw.startswith("billingAccounts/")
        else f"billingAccounts/{billing_id_raw}"
    )
    notification_channel_raw = os.environ.get("GCP_NOTIFICATION_CHANNEL_ID", "").strip()

    class _Bundle:
        instances = compute_v1.InstancesClient()
        disks = compute_v1.DisksClient()
        storage = _storage.Client(project=project_id)
        budgets = budgets_v1.BudgetServiceClient()
        billing_account = billing_id
        notification_channel = notification_channel_raw

    return _Bundle()


def _build_aws_clients(*, region: str, operator_email: str) -> Any:  # noqa: ANN401
    """Lazy-construct the real AWS clients.

    Args:
        region: AWS region for regional clients (ec2, s3, dynamodb).
        operator_email: Attached dynamically so ``aws_spin_up`` can populate
            the budget notification subscriber.

    Returns:
        An object exposing the attributes the lib helpers consume.
    """
    import boto3

    class _Bundle:
        ec2 = boto3.client("ec2", region_name=region)
        ec2_waiter = ec2.get_waiter("instance_terminated")
        s3 = boto3.client("s3", region_name=region)
        dynamo = boto3.client("dynamodb", region_name=region)
        budgets = boto3.client("budgets")
        account_id = boto3.client("sts").get_caller_identity()["Account"]

    bundle = _Bundle()
    bundle.operator_email = operator_email  # type: ignore[attr-defined]
    return bundle


def _do_spinup(args: argparse.Namespace) -> int:
    """Provision tagged resources on both clouds; persist the manifest."""
    gcp = _build_gcp_clients(
        project_id=args.project_id, operator_email=args.operator_email
    )
    aws = _build_aws_clients(region=args.aws_region, operator_email=args.operator_email)
    gcp_out = gcp_spin_up(
        gcp,
        project_id=args.project_id,
        region=args.region,
        zone=args.zone,
        tag=_TAG,
    )
    aws_out = aws_spin_up(aws, region=args.aws_region, tag=_TAG)
    m = Manifest(
        gcp_vms=[gcp_out["vm"]],
        gcp_disks=[gcp_out["disk"]],
        gcp_buckets=[gcp_out["bucket"]],
        gcp_budget_id=gcp_out["budget_id"],
        aws_instances=[aws_out["instance"]],
        aws_volumes=[aws_out["volume"]] if aws_out["volume"] else [],
        aws_buckets=[aws_out["bucket"]],
        aws_tables=[aws_out["table"]] if aws_out["table"] else [],
        aws_budget_name=aws_out["budget_name"],
        created_at=datetime.now().isoformat(timespec="seconds"),
        tag=_TAG,
        aws_region=args.aws_region,
    )
    m.to_json(_MANIFEST_PATH)
    print(f"manifest written: {_MANIFEST_PATH}")
    return 0


def _do_teardown(args: argparse.Namespace) -> int:
    """Read the manifest, destroy every resource it lists, delete manifest."""
    m = Manifest.from_json(_MANIFEST_PATH)
    gcp = _build_gcp_clients(project_id=args.project_id, operator_email="")
    aws = _build_aws_clients(region=m.aws_region, operator_email="")
    gcp_deleted = gcp_tear_down(gcp, m, project_id=args.project_id, zone=args.zone)
    aws_deleted = aws_tear_down(aws, m)
    print(f"GCP deleted: {gcp_deleted}")
    print(f"AWS deleted: {aws_deleted}")
    _MANIFEST_PATH.unlink(missing_ok=True)
    return 0


def _do_snapshot(args: argparse.Namespace) -> int:
    """Print a formatted month-to-date spend report from BigQuery + CE."""
    import boto3
    from google.cloud import bigquery

    bq = bigquery.Client(project=args.project_id)
    ce = boto3.client("ce")
    gcp = gcp_mtd_spend(bq, project_id=args.project_id)
    aws = aws_mtd_spend(ce, account_id="")
    report = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "gcp_mtd_usd_by_service": gcp,
        "aws_mtd_usd_by_service": aws,
        "gcp_total": round(sum(gcp.values()), 2),
        "aws_total": round(sum(aws.values()), 2),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _do_submit_quota(args: argparse.Namespace) -> int:
    """Submit GPU quota increases on both clouds; return non-zero on fallback."""
    import boto3
    from google.cloud import quotas_v1beta  # type: ignore[attr-defined]

    just_gcp = Path(args.justification_gcp).read_text()
    just_aws = Path(args.justification_aws).read_text()

    gcp_client = quotas_v1beta.QuotaAdjusterClient()
    gcp_result = gcp_submit_quota(
        gcp_client,
        project_id=args.project_id,
        region=args.region,
        justification_text=just_gcp,
    )
    if gcp_result.submitted:
        print(f"GCP requests: {gcp_result.request_ids}")
    else:
        print(f"GCP fallback console URL: {gcp_result.console_url}")

    class _AwsPair:
        quotas = boto3.client("service-quotas", region_name=args.aws_region)
        # AWS Support API is global; pin to us-east-1 per AWS docs.
        support = boto3.client("support", region_name="us-east-1")

    aws_result = aws_submit_quota(
        _AwsPair(),
        region=args.aws_region,
        quota_code=_AWS_GPU_QUOTA_CODE,
        desired_value=_AWS_GPU_DESIRED_VALUE,
        justification_text=just_aws,
    )
    print(f"AWS request: {aws_result.request_ids}")
    return 0 if gcp_result.submitted and aws_result.submitted else 1


def _do_scan_bigquery(args: argparse.Namespace) -> int:
    """Run a bounded BigQuery scan with the cap enforced by the lib."""
    from google.cloud import bigquery

    client = bigquery.Client(project=args.project_id)
    out = bq_scan_with_cap(client, sql=args.sql, max_bytes_billed=args.max_bytes_billed)
    print(json.dumps(out, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level CLI parser. Exposed for testing."""
    parser = argparse.ArgumentParser(prog="quota_burn")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spinup", help="Provision tagged resources on both clouds")
    sp.add_argument("--project-id", required=True)
    sp.add_argument("--region", required=True, help="GCP region (e.g. us-west1)")
    sp.add_argument("--zone", required=True, help="GCP zone (e.g. us-west1-a)")
    sp.add_argument("--aws-region", required=True, help="AWS region (e.g. us-west-2)")
    sp.add_argument("--operator-email", required=True)
    sp.set_defaults(func=_do_spinup)

    td = sub.add_parser("teardown", help="Destroy everything in the manifest")
    td.add_argument("--project-id", required=True)
    td.add_argument("--zone", required=True)
    td.set_defaults(func=_do_teardown)

    sn = sub.add_parser("snapshot", help="Print MTD spend by service")
    sn.add_argument("--project-id", required=True)
    sn.set_defaults(func=_do_snapshot)

    sq = sub.add_parser("submit-quota", help="Submit GPU quota increases")
    sq.add_argument("--project-id", required=True)
    sq.add_argument("--region", required=True)
    sq.add_argument("--aws-region", required=True)
    sq.add_argument("--justification-gcp", required=True)
    sq.add_argument("--justification-aws", required=True)
    sq.set_defaults(func=_do_submit_quota)

    sb = sub.add_parser("scan-bigquery", help="Run a bounded BigQuery scan")
    sb.add_argument("--project-id", required=True)
    sb.add_argument("--sql", required=True)
    sb.add_argument(
        "--max-bytes-billed",
        type=int,
        default=10_000_000_000,
        dest="max_bytes_billed",
    )
    sb.set_defaults(func=_do_scan_bigquery)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
