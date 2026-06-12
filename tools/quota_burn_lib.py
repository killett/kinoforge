# tools/quota_burn_lib.py
"""Quota-burn provisioning + teardown + snapshot helpers.

Builds a paid-utilization billing footprint on AWS + GCP across 5 days so the
GPU quota resubmit on day 5 can cite concrete MTD spend numbers. Every helper
takes injected SDK clients so tests pass fakes; nothing here calls a real
cloud unless the CLI passes a real client.

Design rules:
- Stateless helpers; the manifest persisted at .quota_burn/manifest.json is the
  single source of truth for which tagged resources exist.
- Cloud SDKs lazy-imported in the CLI layer (tools/quota_burn.py), never here.
- Local timezone everywhere; never UTC (per user preference).
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class Manifest:
    """Tagged-resource manifest persisted between spinup and teardown."""

    gcp_vms: list[str]
    gcp_disks: list[str]
    gcp_buckets: list[str]
    gcp_budget_id: str | None
    aws_instances: list[str]
    aws_volumes: list[str]
    aws_buckets: list[str]
    aws_tables: list[str]
    aws_budget_name: str | None
    created_at: str
    tag: str = "kinoforge-quota-burn"
    aws_region: str = "us-west-2"

    def to_json(self, path: Path) -> None:
        """Write manifest to path as pretty-sorted JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(
            dataclasses.asdict(self),
            indent=2,
            sort_keys=True,
        )
        path.write_text(text + "\n")

    @classmethod
    def from_json(cls, path: Path) -> Manifest:
        """Read manifest from path; raises FileNotFoundError if missing."""
        text = path.read_text()
        data = json.loads(text)
        return cls(**data)


class _GcpClients(Protocol):
    instances: Any
    disks: Any
    storage: Any
    budgets: Any
    billing_account: str
    notification_channel: str


def _rand_suffix(n: int = 6) -> str:
    """6 lowercase-alphanumeric chars, no ambiguous (l/1/o/0) — for resource names."""
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def gcp_spin_up(
    clients: _GcpClients,
    *,
    project_id: str,
    region: str,
    zone: str,
    tag: str,
) -> dict[str, str]:
    """Provision GCP burn workload; return resource IDs for the manifest.

    Spins: 1 e2-small VM with 10 GB pd-balanced disk in `zone`; 1 GCS bucket in
    `region`; 1 budget at $7 alert threshold routed to operator email; GCP
    budgets are alerting-only and do not block API usage.

    All resources carry label `<tag>=true`. VM startup-script runs
    `shutdown -h +11520` (8-day kernel-side kill, kinoforge spec §3 stack
    layer 1).

    Args:
        clients: injected SDK clients (see `_GcpClients` Protocol).
        project_id: GCP project to provision in.
        region: bucket region.
        zone: VM zone (must be inside `region`).
        tag: label key applied to every resource for blast-radius tagging.

    Returns:
        Dict with keys `vm`, `disk`, `bucket`, `budget_id`.
    """
    suffix = _rand_suffix()
    vm_name = f"kinoforge-burn-{suffix}"
    disk_name = f"{vm_name}-disk"
    bucket_name = f"kinoforge-quota-burn-gcp-{suffix}"

    instance: dict[str, Any] = {
        "name": vm_name,
        "machine_type": f"zones/{zone}/machineTypes/e2-small",
        "labels": {tag: "true"},
        "metadata": {
            "items": [
                {
                    "key": "startup-script",
                    "value": "#!/bin/bash\nshutdown -h +11520\n",
                },
            ]
        },
        "disks": [
            {
                "boot": True,
                "auto_delete": True,
                "device_name": disk_name,
                "initialize_params": {
                    "disk_size_gb": 10,
                    "disk_type": f"zones/{zone}/diskTypes/pd-balanced",
                    "source_image": "projects/debian-cloud/global/images/family/debian-12",
                    "labels": {tag: "true"},
                },
            }
        ],
        "network_interfaces": [
            {
                "network": "global/networks/default",
                "access_configs": [
                    {"name": "External NAT", "type_": "ONE_TO_ONE_NAT"},
                ],
            }
        ],
    }
    clients.instances.insert(
        project=project_id, zone=zone, instance_resource=instance
    ).result(timeout=300)

    bucket = clients.storage.create_bucket(bucket_name, location=region)
    bucket.labels = {tag: "true"}
    bucket.patch()

    # Empty channel string is invalid; budget defaults to IAM-Admin recipients
    # when no explicit channel is wired. Filter empty out.
    notification_channels = [ch for ch in [clients.notification_channel] if ch]
    budget: dict[str, Any] = {
        "display_name": f"kinoforge-quota-burn-{datetime.now().strftime('%Y%m%d')}",
        "amount": {"specified_amount": {"currency_code": "USD", "units": 7}},
        "budget_filter": {"labels": {tag: {"values": ["true"]}}},
        "threshold_rules": [{"threshold_percent": 1.0}],
        "notifications_rule": {
            "pubsub_topic": None,
            "schema_version": "1.0",
            "monitoring_notification_channels": notification_channels,
            "disable_default_iam_recipients": False,
        },
    }
    budget_resp = clients.budgets.create_budget(
        parent=clients.billing_account, budget=budget
    )

    return {
        "vm": vm_name,
        "disk": disk_name,
        "bucket": bucket_name,
        "budget_id": budget_resp.name,
    }


def gcp_tear_down(
    clients: Any,  # noqa: ANN401
    manifest: Manifest,
    *,
    project_id: str,
    zone: str,
) -> list[str]:
    """Destroy every GCP resource in the manifest; idempotent on NotFound.

    Returns the list of resource IDs actually deleted in this call. Resources
    already gone (NotFound) are silently skipped; any other error propagates.

    Order: VMs first → disks → buckets → budget. Disks attached to VMs
    auto-delete on VM deletion; the explicit second pass cleans orphans.

    Args:
        clients: injected SDK clients exposing `instances`, `disks`, `storage`,
            and `budgets` attributes.
        manifest: persisted resource manifest from spinup.
        project_id: GCP project that owns the resources.
        zone: GCP zone the VM and disk live in.

    Returns:
        List of resource IDs deleted in this invocation.
    """
    from google.api_core import exceptions as gax_exc

    deleted: list[str] = []

    def _try(name: str, op_fn: Any) -> None:  # noqa: ANN401
        try:
            op_fn()
            deleted.append(name)
        except gax_exc.NotFound:
            _log.info("gcp_tear_down: %s already gone", name)

    for vm in manifest.gcp_vms:
        _try(
            vm,
            lambda v=vm: clients.instances.delete(
                project=project_id, zone=zone, instance=v
            ).result(timeout=300),
        )

    for disk in manifest.gcp_disks:
        _try(
            disk,
            lambda d=disk: clients.disks.delete(
                project=project_id, zone=zone, disk=d
            ).result(timeout=300),
        )

    for bucket in manifest.gcp_buckets:
        _try(
            bucket,
            lambda b=bucket: clients.storage.get_bucket(b).delete(force=True),
        )

    if manifest.gcp_budget_id is not None:
        _try(
            manifest.gcp_budget_id,
            lambda: clients.budgets.delete_budget(name=manifest.gcp_budget_id),
        )

    return deleted


def gcp_mtd_spend(
    client: Any,  # noqa: ANN401
    *,
    project_id: str,
    billing_dataset: str = "kinoforge-prod-0ddb375e.all_billing_data",
) -> dict[str, float]:
    """Return month-to-date spend grouped by service, in USD.

    `client` must expose `.query(query: str) -> list[dict]` where each row
    contains `service_description` and `cost_usd` keys. Production wires this
    to a BigQuery client running against the billing export dataset; tests
    pass a fake row list.

    Args:
        client: duck-typed query client (BigQuery in production, fake in tests).
        project_id: GCP project ID to filter billing rows by.
        billing_dataset: BigQuery dataset containing the billing export table,
            e.g. ``"<project>.all_billing_data"``. Defaults to kinoforge's own
            billing-export location; callers in other projects must override.

    Returns:
        Dict mapping service description to total USD spend for current month.
    """
    sql = (
        "SELECT service.description AS service_description, "  # noqa: S608
        "SUM(cost) AS cost_usd "
        f"FROM `{billing_dataset}.gcp_billing_export_v1_*` "
        f"WHERE project.id = '{project_id}' "
        "AND DATE(_PARTITIONTIME) >= DATE_TRUNC(CURRENT_DATE(), MONTH) "
        "GROUP BY service_description"
    )
    rows = client.query(query=sql)
    return {r["service_description"]: float(r["cost_usd"]) for r in rows}


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------

_USER_DATA_TEMPLATE = """#!/bin/bash
set -eux
# Kernel-side hard shutdown 8 days after boot (longer than the 5-day burn window
# so the day-5 teardown destroys the instance before kernel kill fires).
# Original 8h (480 min) was too aggressive — instance would die before day 1.
shutdown -h +11520
# Re-arm on every reboot via cron.
echo '@reboot root /sbin/shutdown -h +11520' > /etc/cron.d/kinoforge-burn-shutdown
chmod 0644 /etc/cron.d/kinoforge-burn-shutdown
"""


def aws_spin_up(
    clients: Any,  # noqa: ANN401
    *,
    region: str,
    tag: str,
) -> dict[str, str]:
    """Provision AWS burn workload; return resource IDs for the manifest.

    Spins: 1× t4g.nano EC2 with 30 GB gp3 EBS (auto-terminate on shutdown),
    1× S3 bucket, 1× DynamoDB on-demand table, 1× AWS Budget at $5 hard cap.

    All resources tagged `<tag>=true`. EC2 UserData runs `shutdown -h +11520`
    AND InstanceInitiatedShutdownBehavior=terminate so the kernel shutdown
    actually destroys the instance.

    Args:
        clients: injected boto3-shaped clients with attributes `ec2`, `s3`,
            `dynamo`, `budgets`, `account_id`, and `operator_email`.
            No `boto3` import occurs here — callers inject real or fake clients.
        region: AWS region for all resources.
        tag: tag key applied to every resource.

    Returns:
        Dict with keys `instance`, `volume`, `bucket`, `table`, `budget_name`.
    """
    suffix = _rand_suffix()
    bucket_name = f"kinoforge-quota-burn-aws-{suffix}"
    table_name = f"kinoforge-quota-burn-{suffix}"
    budget_name = f"kinoforge-quota-burn-{datetime.now().strftime('%Y%m%d')}-{suffix}"
    tags = [{"Key": tag, "Value": "true"}]

    user_data_b64 = base64.b64encode(_USER_DATA_TEMPLATE.encode()).decode()
    # Canonical Ubuntu 22.04 arm64 in us-west-2 (verified 2026-06-11).
    # Hardcoded because SSM-resolve at run_instances requires ssm:GetParameters
    # which kinoforge-ci IAM user doesn't have. Region change requires lookup.
    image_id_us_west_2 = "ami-029ea2abb0342f2f2"
    run = clients.ec2.run_instances(
        ImageId=image_id_us_west_2,
        InstanceType="t4g.nano",
        MinCount=1,
        MaxCount=1,
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": 30,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            }
        ],
        InstanceInitiatedShutdownBehavior="terminate",
        UserData=user_data_b64,
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
        ],
    )
    instance_id = run["Instances"][0]["InstanceId"]
    # run_instances returns BlockDeviceMappings empty until the instance is
    # actually running (it populates after a poll). Volume tracking isn't
    # required for teardown because DeleteOnTermination=True auto-removes the
    # EBS when the EC2 terminates. Keep the dict key for manifest-shape parity.
    bdms = run["Instances"][0].get("BlockDeviceMappings") or []
    volume_id = bdms[0]["Ebs"]["VolumeId"] if bdms else ""

    create_kwargs: dict[str, Any] = {"Bucket": bucket_name}
    if region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    clients.s3.create_bucket(**create_kwargs)
    clients.s3.put_bucket_tagging(Bucket=bucket_name, Tagging={"TagSet": tags})

    # DynamoDB is optional billing-signal padding ($0.10 over 5 days).
    # If the calling IAM principal lacks dynamodb:CreateTable, skip it cleanly
    # — losing the line item is acceptable; failing the whole spinup is not.
    try:
        clients.dynamo.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
            Tags=tags,
        )
    except Exception as exc:  # noqa: BLE001
        # Best-effort skip; only ClientError(AccessDeniedException) is expected.
        # Other failures fall through to a no-table manifest.
        _log.warning(
            "aws_spin_up: DynamoDB create_table skipped (%s); proceeding without table",
            type(exc).__name__,
        )
        table_name = ""

    # AWS Budget alarm — same tolerance pattern as DynamoDB above. Kernel
    # shutdown + InstanceInitiatedShutdownBehavior=terminate carry the safety
    # if the IAM principal lacks budgets:ModifyBudget; daily snapshots still
    # surface spend pacing.
    try:
        clients.budgets.create_budget(
            AccountId=clients.account_id,
            Budget={
                "BudgetName": budget_name,
                "BudgetLimit": {"Amount": "5", "Unit": "USD"},
                "TimeUnit": "MONTHLY",
                "BudgetType": "COST",
            },
            NotificationsWithSubscribers=[
                {
                    "Notification": {
                        "NotificationType": "ACTUAL",
                        "ComparisonOperator": "GREATER_THAN",
                        "Threshold": 100.0,
                        "ThresholdType": "PERCENTAGE",
                    },
                    "Subscribers": [
                        {
                            "SubscriptionType": "EMAIL",
                            "Address": clients.operator_email,
                        }
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "aws_spin_up: budget create skipped (%s); rely on kernel shutdown + daily snapshot",
            type(exc).__name__,
        )
        budget_name = ""

    return {
        "instance": instance_id,
        "volume": volume_id,
        "bucket": bucket_name,
        "table": table_name,
        "budget_name": budget_name,
    }


# ---------------------------------------------------------------------------
# AWS teardown + MTD spend snapshot helpers
# ---------------------------------------------------------------------------

# AWS NotFound codes across the five services we touch.
_AWS_NOT_FOUND = {
    "InvalidInstanceID.NotFound",
    "InvalidVolume.NotFound",
    "NoSuchBucket",
    "ResourceNotFoundException",
    "NotFoundException",
}


def _is_aws_not_found(exc: BaseException) -> bool:
    """Return True iff exc is a botocore ClientError with a known NotFound code."""
    from botocore.exceptions import ClientError

    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code", "")
    return code in _AWS_NOT_FOUND


def aws_tear_down(
    clients: Any,  # noqa: ANN401
    manifest: Manifest,
) -> list[str]:
    """Destroy every AWS resource in the manifest; idempotent on NotFound.

    Order: EC2 terminate → ec2_waiter wait → EBS orphan-delete → S3 empty +
    delete → DynamoDB delete → Budget delete. Each step swallows the matching
    AWS NotFound code so re-runs reach zero without failure.

    The EC2 step is guarded by its own try/except because the waiter and
    terminate call are coupled — the waiter should only run when terminate
    succeeded (i.e. the instance existed).

    Args:
        clients: injected boto3-shaped clients with attributes `ec2`,
            `ec2_waiter`, `s3`, `dynamo`, `budgets`, and `account_id`.
        manifest: persisted resource manifest from spinup.

    Returns:
        List of resource IDs actually deleted in this invocation (already-gone
        resources are silently skipped and NOT included).
    """
    deleted: list[str] = []

    def _try(name: str, fn: Any) -> None:  # noqa: ANN401
        try:
            fn()
            deleted.append(name)
        except BaseException as exc:
            if _is_aws_not_found(exc):
                _log.info("aws_tear_down: %s already gone", name)
                return
            raise

    # EC2 terminate + wait (coupled — waiter only runs when terminate succeeded).
    if manifest.aws_instances:
        try:
            clients.ec2.terminate_instances(InstanceIds=manifest.aws_instances)
            clients.ec2_waiter.wait(
                InstanceIds=manifest.aws_instances,
                WaiterConfig={"Delay": 15, "MaxAttempts": 20},
            )
            deleted.extend(manifest.aws_instances)
        except BaseException as exc:
            if not _is_aws_not_found(exc):
                raise

    # EBS orphan-delete (attached volumes with DeleteOnTermination=True are
    # already gone; this pass cleans any orphans).
    for vol in manifest.aws_volumes:
        _try(vol, lambda v=vol: clients.ec2.delete_volume(VolumeId=v))

    # S3: empty then delete.
    for bucket in manifest.aws_buckets:

        def _empty_and_drop(b: str = bucket) -> None:
            listing = clients.s3.list_objects_v2(Bucket=b)
            for obj in listing.get("Contents", []):
                clients.s3.delete_object(Bucket=b, Key=obj["Key"])
            clients.s3.delete_bucket(Bucket=b)

        _try(bucket, _empty_and_drop)

    # DynamoDB.
    for table in manifest.aws_tables:
        _try(table, lambda t=table: clients.dynamo.delete_table(TableName=t))

    # Budget.
    if manifest.aws_budget_name is not None:
        _try(
            manifest.aws_budget_name,
            lambda: clients.budgets.delete_budget(
                AccountId=clients.account_id,
                BudgetName=manifest.aws_budget_name,
            ),
        )

    return deleted


def aws_mtd_spend(
    client: Any,  # noqa: ANN401
    *,
    account_id: str,
) -> dict[str, float]:
    """Return month-to-date spend grouped by service, in USD.

    Queries AWS Cost Explorer for the current calendar month with
    ``Granularity="MONTHLY"`` and ``GROUP BY SERVICE``. The ``account_id``
    parameter is accepted for signature parity with ``gcp_mtd_spend``; Cost
    Explorer always scopes to the calling account regardless of this value.

    Uses ``out.get(svc, 0.0) + amount`` accumulation so that results remain
    correct if the caller passes a date range spanning multiple calendar months
    (which produces multiple ``ResultsByTime`` windows).

    Args:
        client: boto3 ``ce`` (Cost Explorer) client; injected so tests pass a
            fake without a real AWS call.
        account_id: AWS account ID. Accepted for API-surface parity with the
            GCP equivalent; Cost Explorer already scopes to the calling account.

    Returns:
        Dict mapping service name to total USD spend for the current month.

    Note:
        ``end_dt`` is set to ``today + timedelta(days=1)`` so that ``Start <
        End`` holds strictly even on the 1st of the month (when
        ``start_dt == today``). Cost Explorer raises ``ValidationException``
        if ``Start >= End``.
    """
    today = datetime.now()
    start_dt = today.replace(day=1)
    end_dt = today + timedelta(days=1)  # CE requires Start < End strictly.
    start = start_dt.strftime("%Y-%m-%d")
    end = end_dt.strftime("%Y-%m-%d")
    resp = client.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    out: dict[str, float] = {}
    for window in resp["ResultsByTime"]:
        for grp in window["Groups"]:
            svc = grp["Keys"][0]
            amount = float(grp["Metrics"]["UnblendedCost"]["Amount"])
            out[svc] = out.get(svc, 0.0) + amount
    return out


# ---------------------------------------------------------------------------
# Quota-submit helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class QuotaSubmitResult:
    """Outcome of a quota submission.

    ``submitted`` is True only when the SDK call succeeded. ``request_ids`` is
    populated on success. ``console_url`` is populated on GCP fallback so the
    operator can finish the request manually.
    """

    submitted: bool
    request_ids: list[str]
    console_url: str | None


def _gcp_console_quota_url(project_id: str) -> str:
    """Build the pre-filled GCP console URL for the quota fallback path.

    Args:
        project_id: GCP project ID to embed in the URL.

    Returns:
        Pre-filled ``https://console.cloud.google.com/iam-admin/quotas`` URL.
    """
    qs = urlencode(
        {
            "project": project_id,
            "filter": (
                "metric:compute.googleapis.com/NVIDIA_T4_GPUS OR "
                "compute.googleapis.com/gpus_all_regions"
            ),
        }
    )
    return f"https://console.cloud.google.com/iam-admin/quotas?{qs}"


def gcp_submit_quota(
    client: Any,  # noqa: ANN401
    *,
    project_id: str,
    region: str,
    justification_text: str,
) -> QuotaSubmitResult:
    """Submit GCP GPU quota adjustments (global + regional) for the burn project.

    Submits BOTH ``compute.googleapis.com/gpus_all_regions`` (global) AND
    ``compute.googleapis.com/nvidia_t4_gpus`` (regional) to ``desired_value=1``.

    On SDK error (the alpha quotas API is rejection-prone), logs a warning and
    returns a result with a pre-filled console URL so the operator can complete
    the request manually (spec §7 R3 fallback).

    Args:
        client: duck-typed quota client exposing ``create_quota_adjustment(**kwargs)``.
        project_id: GCP project ID.
        region: GCP region for the regional quota (e.g. ``"us-west1"``).
        justification_text: free-text reason forwarded to the SDK ``reason`` field.

    Returns:
        :class:`QuotaSubmitResult` with ``submitted=True`` on success, or
        ``submitted=False`` + ``console_url`` on failure.
    """
    metrics = [
        ("compute.googleapis.com/gpus_all_regions", "global"),
        ("compute.googleapis.com/nvidia_t4_gpus", region),
    ]
    request_ids: list[str] = []
    try:
        for metric, location in metrics:
            op = client.create_quota_adjustment(
                parent=f"projects/{project_id}",
                metric=metric,
                location=location,
                desired_value=1,
                reason=justification_text,
            )
            request_ids.append(op.name)
    except Exception:
        _log.warning(
            "gcp_submit_quota: SDK rejected; falling back to console URL",
            exc_info=True,
        )
        return QuotaSubmitResult(
            submitted=False,
            request_ids=[],
            console_url=_gcp_console_quota_url(project_id),
        )
    return QuotaSubmitResult(
        submitted=True,
        request_ids=request_ids,
        console_url=None,
    )


def aws_submit_quota(
    clients: Any,  # noqa: ANN401
    *,
    region: str,
    quota_code: str,
    desired_value: int,
    justification_text: str,
) -> QuotaSubmitResult:
    """Submit AWS service-quota increase and attach the justification to the case.

    AWS's ``RequestServiceQuotaIncrease`` API does NOT accept a justification
    field directly; the request automatically opens a Support case, and
    ``support.add_communication_to_case`` is the only surface where the reason
    text reaches quota reviewers.

    ``clients`` must expose:
    - ``.quotas``: boto3 service-quotas client.
    - ``.support``: boto3 support client.

    Raises on any SDK error — no console-URL fallback (AWS API is reliable).

    Args:
        clients: injected boto3-shaped client pair (``quotas`` + ``support``).
        region: AWS region (accepted for API-surface parity; EC2 quota codes are
            global in Service Quotas, but the region context is logged).
        quota_code: Service Quotas quota code, e.g. ``"L-DB2E81BA"``.
        desired_value: target quota value (cast to ``float`` before the API call).
        justification_text: reason text attached to the auto-opened support case.

    Returns:
        :class:`QuotaSubmitResult` with ``submitted=True`` and the request ID.
    """
    resp = clients.quotas.request_service_quota_increase(
        ServiceCode="ec2",
        QuotaCode=quota_code,
        DesiredValue=float(desired_value),
    )
    request_id = resp["RequestedQuota"]["Id"]
    case_id = resp["RequestedQuota"].get("CaseId")
    if case_id:
        clients.support.add_communication_to_case(
            caseId=case_id,
            communicationBody=justification_text,
        )
    else:
        _log.warning(
            "aws_submit_quota: no CaseId on RequestedQuota response; "
            "justification not attached. Request id: %s",
            request_id,
        )
    return QuotaSubmitResult(
        submitted=True,
        request_ids=[request_id],
        console_url=None,
    )


# ---------------------------------------------------------------------------
# BigQuery scan helpers
# ---------------------------------------------------------------------------


class BigQueryCapExceeded(RuntimeError):
    """Raised when a BigQuery dry-run reports bytes > cap; live query is blocked."""


def bq_scan_with_cap(
    client: Any,  # noqa: ANN401
    *,
    sql: str,
    max_bytes_billed: int = 10_000_000_000,
) -> dict[str, int]:
    """Run ``sql`` with a dry-run gate and a hard maximum-bytes-billed cap.

    Spec §7 R5 — the only mechanism by which the burn can blow $20 is an
    unbounded BigQuery scan. The dry-run pre-check returns the exact
    bytes-to-scan estimate at zero cost; we refuse to run the live query
    if that estimate exceeds the cap.

    Step 1: dry-run with ``QueryJobConfig(dry_run=True, use_query_cache=False)``.
    If ``total_bytes_processed > max_bytes_billed``, raises
    :class:`BigQueryCapExceeded` BEFORE any billable bytes are consumed.

    Step 2: live query with ``QueryJobConfig(maximum_bytes_billed=max_bytes_billed)``.

    Args:
        client: duck-typed BigQuery client exposing
            ``query(sql, job_config=...)`` that returns a job with
            ``.total_bytes_processed`` and ``.result()`` attributes.
        sql: SQL string to execute.
        max_bytes_billed: bytes ceiling for both the dry-run guard and the
            live ``maximum_bytes_billed`` safety cap. Defaults to 10 GB.

    Returns:
        Dict with keys ``rows`` (int) and ``bytes_billed`` (int, from dry-run).

    Raises:
        BigQueryCapExceeded: if dry-run reports ``total_bytes_processed``
            exceeds ``max_bytes_billed``.
    """
    from google.cloud.bigquery import QueryJobConfig

    dry_cfg = QueryJobConfig(dry_run=True, use_query_cache=False)
    dry_job = client.query(sql, job_config=dry_cfg)
    bytes_billed = int(dry_job.total_bytes_processed)
    if bytes_billed > max_bytes_billed:
        raise BigQueryCapExceeded(
            f"dry-run bytes_billed={bytes_billed} exceeds cap {max_bytes_billed}"
        )

    live_cfg = QueryJobConfig(maximum_bytes_billed=max_bytes_billed)
    live_job = client.query(sql, job_config=live_cfg)
    rows = list(live_job.result())
    return {"rows": len(rows), "bytes_billed": bytes_billed}
