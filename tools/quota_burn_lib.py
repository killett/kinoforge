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

        def _del_bucket(b: str = bucket) -> None:
            obj = clients.storage.get_bucket(b)
            obj.delete(force=True)

        _try(bucket, _del_bucket)

    if manifest.gcp_budget_id is not None:
        _try(
            manifest.gcp_budget_id,
            lambda: clients.budgets.delete_budget(name=manifest.gcp_budget_id),
        )

    return deleted


def gcp_mtd_spend(client: Any, *, project_id: str) -> dict[str, float]:  # noqa: ANN401
    """Return month-to-date spend grouped by service, in USD.

    `client` must expose `.query(query: str) -> list[dict]` where each row
    contains `service_description` and `cost_usd` keys. Production wires this
    to a BigQuery client running against the billing export dataset; tests
    pass a fake row list.

    Args:
        client: duck-typed query client (BigQuery in production, fake in tests).
        project_id: GCP project ID to filter billing rows by.

    Returns:
        Dict mapping service description to total USD spend for current month.
    """
    sql = (
        "SELECT service.description AS service_description, "  # noqa: S608
        "SUM(cost) AS cost_usd "
        "FROM `kinoforge-prod-0ddb375e.all_billing_data.gcp_billing_export_v1_*` "
        f"WHERE project.id = '{project_id}' "
        "AND DATE(_PARTITIONTIME) >= DATE_TRUNC(CURRENT_DATE(), MONTH) "
        "GROUP BY service_description"
    )
    rows = client.query(query=sql)
    out: dict[str, float] = {}
    for r in rows:
        out[r["service_description"]] = out.get(r["service_description"], 0.0) + float(
            r["cost_usd"]
        )
    return out
