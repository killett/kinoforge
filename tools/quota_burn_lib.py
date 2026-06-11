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
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

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


class _GcpInstanceResource:
    """Mirror of google.cloud.compute_v1.Instance just enough for tests + production.

    Production code constructs the real type via the SDK; the duck-typed shape
    here exists so tests can build fakes without importing google.cloud.
    """

    def __init__(
        self,
        *,
        name: str,
        machine_type: str,
        labels: dict[str, str],
        metadata: dict[str, Any],
        disks: list[dict[str, Any]],
    ) -> None:
        self.name = name
        self.machine_type = machine_type
        self.labels = labels
        self.metadata = metadata
        self.disks = disks


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
    `shutdown -h +480` (8-hour kernel-side kill, kinoforge spec §3 stack
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

    instance = _GcpInstanceResource(
        name=vm_name,
        machine_type=f"zones/{zone}/machineTypes/e2-small",
        labels={tag: "true"},
        metadata={
            "items": [
                {
                    "key": "startup-script",
                    "value": "#!/bin/bash\nshutdown -h +480\n",
                },
            ]
        },
        disks=[
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
    )
    clients.instances.insert(
        project=project_id, zone=zone, instance_resource=instance
    ).result(timeout=300)

    bucket = clients.storage.create_bucket(bucket_name, location=region)
    bucket.labels = {tag: "true"}
    bucket.patch()

    @dataclass
    class _BudgetFilter:
        labels: dict[str, dict[str, list[str]]]

    @dataclass
    class _BudgetAmount:
        specified_amount: dict[str, object]

    @dataclass
    class _Budget:
        display_name: str
        amount: _BudgetAmount
        budget_filter: _BudgetFilter
        threshold_rules: list[dict[str, float]]
        notifications_rule: dict[str, object]

    budget = _Budget(
        display_name=f"kinoforge-quota-burn-{datetime.now().strftime('%Y%m%d')}",
        amount=_BudgetAmount(specified_amount={"currency_code": "USD", "units": 7}),
        budget_filter=_BudgetFilter(labels={tag: {"values": ["true"]}}),
        threshold_rules=[{"threshold_percent": 1.0}],
        notifications_rule={
            "pubsub_topic": None,
            "schema_version": "1.0",
            "monitoring_notification_channels": [clients.notification_channel],
            "disable_default_iam_recipients": False,
        },
    )
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
# Kernel-side hard shutdown 8h after boot (spec §3 stack layer 1).
shutdown -h +480
# Re-arm on every reboot via cron.
echo '@reboot root /sbin/shutdown -h +480' > /etc/cron.d/kinoforge-burn-shutdown
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

    All resources tagged `<tag>=true`. EC2 UserData runs `shutdown -h +480`
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
    run = clients.ec2.run_instances(
        # Note: 'ebs-gp2' in the SSM path is the Canonical AMI family label, NOT the
        # launch-time volume type. The BlockDeviceMappings override below gets gp3.
        ImageId="resolve:ssm:/aws/service/canonical/ubuntu/server/22.04/stable/current/arm64/hvm/ebs-gp2/ami-id",
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
    volume_id = run["Instances"][0]["BlockDeviceMappings"][0]["Ebs"]["VolumeId"]

    create_kwargs: dict[str, Any] = {"Bucket": bucket_name}
    if region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    clients.s3.create_bucket(**create_kwargs)
    clients.s3.put_bucket_tagging(Bucket=bucket_name, Tagging={"TagSet": tags})

    clients.dynamo.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
        Tags=tags,
    )

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

    return {
        "instance": instance_id,
        "volume": volume_id,
        "bucket": bucket_name,
        "table": table_name,
        "budget_name": budget_name,
    }
