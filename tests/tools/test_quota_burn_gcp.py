# tests/tools/test_quota_burn_gcp.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tools.quota_burn_lib import Manifest, gcp_mtd_spend, gcp_spin_up, gcp_tear_down


@dataclass
class FakeOperation:
    """Mimics google.api_core.operation.Operation enough for our usage."""

    result_val: Any = None

    def result(self, timeout: int | None = None) -> Any:
        return self.result_val


@dataclass
class FakeInstancesClient:
    insert_calls: list[dict[str, Any]] = field(default_factory=list)

    def insert(
        self, *, project: str, zone: str, instance_resource: Any
    ) -> FakeOperation:
        self.insert_calls.append(
            {"project": project, "zone": zone, "instance": instance_resource}
        )
        return FakeOperation()


@dataclass
class _FakeBucket:
    name: str
    location: str
    labels: dict[str, str] = field(default_factory=dict)
    patched: bool = False

    def patch(self) -> None:
        self.patched = True


@dataclass
class FakeStorageClient:
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    buckets: list[_FakeBucket] = field(default_factory=list)

    def create_bucket(self, name: str, *, location: str) -> _FakeBucket:
        self.create_calls.append({"name": name, "location": location})
        b = _FakeBucket(name=name, location=location)
        self.buckets.append(b)
        return b


@dataclass
class FakeBudgetClient:
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    def create_budget(self, *, parent: str, budget: Any) -> Any:
        self.create_calls.append({"parent": parent, "budget": budget})
        return type("Budget", (), {"name": "billingAccounts/ACME/budgets/burn-7"})()


@dataclass
class FakeGcpClients:
    instances: FakeInstancesClient
    storage: FakeStorageClient
    budgets: FakeBudgetClient
    disks: Any = None  # not used by gcp_spin_up; satisfies _GcpClients Protocol
    billing_account: str = "billingAccounts/ACME"
    notification_channel: str = "projects/proj/notificationChannels/c1"


def _make_clients() -> FakeGcpClients:
    return FakeGcpClients(
        instances=FakeInstancesClient(),
        storage=FakeStorageClient(),
        budgets=FakeBudgetClient(),
    )


def test_gcp_spin_up_returns_resource_ids() -> None:
    """Bug catch: returning the wrong type breaks Manifest.from_dict downstream."""
    clients = _make_clients()
    out = gcp_spin_up(
        clients,
        project_id="kinoforge-prod-0ddb375e",
        region="us-west1",
        zone="us-west1-a",
        tag="kinoforge-quota-burn",
    )
    assert set(out.keys()) == {"vm", "disk", "bucket", "budget_id"}
    assert isinstance(out["vm"], str) and out["vm"].startswith("kinoforge-burn-")
    assert isinstance(out["disk"], str)
    assert out["disk"].startswith("kinoforge-burn-") and out["disk"].endswith("-disk")
    assert out["bucket"].startswith("kinoforge-quota-burn-gcp-")
    assert out["budget_id"] == "billingAccounts/ACME/budgets/burn-7"


def test_gcp_spin_up_tags_every_resource() -> None:
    """Bug catch: untagged resources are invisible to teardown by tag-filter and
    would be missed by `tools/quota_burn.py teardown`."""
    clients = _make_clients()
    gcp_spin_up(
        clients,
        project_id="kinoforge-prod-0ddb375e",
        region="us-west1",
        zone="us-west1-a",
        tag="kinoforge-quota-burn",
    )
    tag = "kinoforge-quota-burn"
    vm_call = clients.instances.insert_calls[0]

    # VM labels
    assert vm_call["instance"].labels == {tag: "true"}

    # Disk labels (embedded in the instance disk initialize_params)
    disk_labels = vm_call["instance"].disks[0]["initialize_params"]["labels"]
    assert disk_labels == {tag: "true"}

    # Bucket labels — set via post-create patch()
    assert clients.storage.buckets[0].labels == {tag: "true"}
    assert clients.storage.buckets[0].patched is True

    # Budget filter uses real Budgets v1 proto shape (no top-level labels)
    budget_arg = clients.budgets.create_calls[0]["budget"]
    assert hasattr(budget_arg, "budget_filter")
    assert budget_arg.budget_filter.labels == {tag: {"values": ["true"]}}


def test_gcp_spin_up_arms_kernel_shutdown() -> None:
    """Bug catch: VM without kernel-side shutdown could outlive operator
    attention and blow budget. Spec §3 stack layer 1."""
    clients = _make_clients()
    gcp_spin_up(
        clients,
        project_id="kinoforge-prod-0ddb375e",
        region="us-west1",
        zone="us-west1-a",
        tag="kinoforge-quota-burn",
    )
    vm_call = clients.instances.insert_calls[0]
    items = vm_call["instance"].metadata["items"]
    startup = next(i for i in items if i["key"] == "startup-script")
    assert "shutdown -h +480" in startup["value"]


def test_gcp_spin_up_uses_e2_small_in_zone() -> None:
    """Bug catch: wrong machine type would blow the $2/5-day budget assumption."""
    clients = _make_clients()
    gcp_spin_up(
        clients,
        project_id="kinoforge-prod-0ddb375e",
        region="us-west1",
        zone="us-west1-a",
        tag="kinoforge-quota-burn",
    )
    vm_call = clients.instances.insert_calls[0]
    assert vm_call["zone"] == "us-west1-a"
    assert "e2-small" in vm_call["instance"].machine_type


# ---------------------------------------------------------------------------
# Task 3: GCP teardown + MTD spend snapshot helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeComputeForTeardown:
    delete_calls: list[tuple[str, str, str]] = field(default_factory=list)
    not_found: set[str] = field(default_factory=set)

    def delete(self, *, project: str, zone: str, instance: str) -> FakeOperation:
        if instance in self.not_found:
            from google.api_core import exceptions

            raise exceptions.NotFound(f"instance {instance}")
        self.delete_calls.append(("instance", project, instance))
        return FakeOperation()


@dataclass
class FakeDisksForTeardown:
    delete_calls: list[tuple[str, str, str]] = field(default_factory=list)
    not_found: set[str] = field(default_factory=set)

    def delete(self, *, project: str, zone: str, disk: str) -> FakeOperation:
        if disk in self.not_found:
            from google.api_core import exceptions

            raise exceptions.NotFound(f"disk {disk}")
        self.delete_calls.append(("disk", project, disk))
        return FakeOperation()


@dataclass
class FakeStorageForTeardown:
    delete_calls: list[str] = field(default_factory=list)
    not_found: set[str] = field(default_factory=set)

    def get_bucket(self, name: str) -> Any:
        if name in self.not_found:
            from google.api_core import exceptions

            raise exceptions.NotFound(f"bucket {name}")
        return type(
            "Bucket",
            (),
            {
                "name": name,
                "delete": lambda self_, force=False: None,
            },
        )()


# Helper to attribute the delete back into the spy
def _make_storage_with_spy() -> tuple[FakeStorageForTeardown, list[str]]:
    spy: list[str] = []
    fake = FakeStorageForTeardown()

    def get_bucket(name: str) -> Any:
        if name in fake.not_found:
            from google.api_core import exceptions

            raise exceptions.NotFound(f"bucket {name}")

        class _B:
            def __init__(self_inner) -> None:
                self_inner.name = name

            def delete(self_inner, force: bool = False) -> None:
                spy.append(name)

        return _B()

    fake.get_bucket = get_bucket  # type: ignore[method-assign]
    return fake, spy


@dataclass
class FakeBudgetForTeardown:
    delete_calls: list[str] = field(default_factory=list)
    not_found: set[str] = field(default_factory=set)

    def delete_budget(self, *, name: str) -> None:
        if name in self.not_found:
            from google.api_core import exceptions

            raise exceptions.NotFound(name)
        self.delete_calls.append(name)


@dataclass
class TeardownClients:
    instances: FakeComputeForTeardown
    disks: FakeDisksForTeardown
    storage: FakeStorageForTeardown
    budgets: FakeBudgetForTeardown


def _teardown_manifest() -> Manifest:
    return Manifest(
        gcp_vms=["kinoforge-burn-xyz"],
        gcp_disks=["kinoforge-burn-xyz-disk"],
        gcp_buckets=["kinoforge-quota-burn-gcp-xyz"],
        gcp_budget_id="billingAccounts/ACME/budgets/burn-7",
        aws_instances=[],
        aws_volumes=[],
        aws_buckets=[],
        aws_tables=[],
        aws_budget_name=None,
        created_at="2026-06-10T09:30:00",
        tag="kinoforge-quota-burn",
    )


def test_gcp_tear_down_deletes_every_resource() -> None:
    """Bug catch: a teardown missing any line item leaves live spend; spec §3."""
    storage, storage_spy = _make_storage_with_spy()
    clients = TeardownClients(
        instances=FakeComputeForTeardown(),
        disks=FakeDisksForTeardown(),
        storage=storage,
        budgets=FakeBudgetForTeardown(),
    )
    deleted = gcp_tear_down(
        clients,
        _teardown_manifest(),
        project_id="kinoforge-prod-0ddb375e",
        zone="us-west1-a",
    )
    assert "kinoforge-burn-xyz" in deleted
    assert "kinoforge-burn-xyz-disk" in deleted
    assert "kinoforge-quota-burn-gcp-xyz" in deleted
    assert "billingAccounts/ACME/budgets/burn-7" in deleted
    assert storage_spy == ["kinoforge-quota-burn-gcp-xyz"]


def test_gcp_tear_down_is_idempotent_on_missing() -> None:
    """Bug catch: NotFound on a partially-completed prior teardown must NOT raise;
    re-running teardown must reach 0 resources without failure."""
    storage, _ = _make_storage_with_spy()
    storage.not_found = {"kinoforge-quota-burn-gcp-xyz"}
    clients = TeardownClients(
        instances=FakeComputeForTeardown(not_found={"kinoforge-burn-xyz"}),
        disks=FakeDisksForTeardown(not_found={"kinoforge-burn-xyz-disk"}),
        storage=storage,
        budgets=FakeBudgetForTeardown(
            not_found={"billingAccounts/ACME/budgets/burn-7"}
        ),
    )
    deleted = gcp_tear_down(
        clients,
        _teardown_manifest(),
        project_id="kinoforge-prod-0ddb375e",
        zone="us-west1-a",
    )
    assert deleted == []


def test_gcp_tear_down_raises_on_unexpected_error() -> None:
    """Bug catch: silently swallowing non-NotFound errors would mask real
    failures (auth, project mismatch, region typo). Must propagate."""

    class _Boom:
        def delete(self, **_: Any) -> FakeOperation:
            raise RuntimeError("network is on fire")

    storage, _ = _make_storage_with_spy()
    clients = TeardownClients(
        instances=_Boom(),  # type: ignore[arg-type]
        disks=FakeDisksForTeardown(),
        storage=storage,
        budgets=FakeBudgetForTeardown(),
    )
    with pytest.raises(RuntimeError, match="network is on fire"):
        gcp_tear_down(
            clients,
            _teardown_manifest(),
            project_id="kinoforge-prod-0ddb375e",
            zone="us-west1-a",
        )


@dataclass
class FakeBillingClient:
    """Minimal Cloud Billing service-usage / billing-export query stub."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    def query(self, *, query: str) -> list[dict[str, Any]]:
        return self.rows


def test_gcp_mtd_spend_groups_by_service() -> None:
    """Bug catch: returning ungrouped rows would force the snapshot to aggregate
    again and risk double-counting."""
    client = FakeBillingClient(
        rows=[
            {"service_description": "Compute Engine", "cost_usd": 1.20},
            {"service_description": "Compute Engine", "cost_usd": 0.80},
            {"service_description": "Cloud Storage", "cost_usd": 0.50},
            {"service_description": "BigQuery", "cost_usd": 1.50},
        ]
    )
    spend = gcp_mtd_spend(client, project_id="kinoforge-prod-0ddb375e")
    assert spend == {"Compute Engine": 2.0, "Cloud Storage": 0.5, "BigQuery": 1.5}
