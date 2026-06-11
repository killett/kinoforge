# tests/tools/test_quota_burn_gcp.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tools.quota_burn_lib import gcp_spin_up


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
class FakeStorageClient:
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    def create_bucket(self, name: str, *, location: str) -> Any:
        self.create_calls.append({"name": name, "location": location})
        return type("Bucket", (), {"name": name, "location": location})()


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
    vm_call = clients.instances.insert_calls[0]
    assert vm_call["instance"].labels == {"kinoforge-quota-burn": "true"}


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
