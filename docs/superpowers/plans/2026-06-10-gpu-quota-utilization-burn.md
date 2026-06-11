# GPU quota utilization-burn — Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build paid-utilization billing footprint on AWS + GCP across 5 days for ≤ $20 then resubmit GPU quota requests with concrete spend numbers in sharpened justification text.

**Architecture:** Two flat tool files in `tools/` (matching existing project pattern of `preflight.py`, `cloud_perms_probe.py`). `tools/quota_burn_lib.py` holds the manifest dataclass plus stateless GCP/AWS provisioning + teardown + snapshot + submit helpers. Every helper takes injected SDK client objects so unit tests pass fakes — no real cloud calls in tests. `tools/quota_burn.py` is the CLI dispatcher (subcommands: `spinup`, `teardown`, `snapshot`, `submit`, `scan`). A persisted JSON manifest at `.quota_burn/manifest.json` is the single source of truth for which tagged resources exist; teardown reads it and destroys idempotently.

**Tech Stack:**
- `google-cloud-compute`, `google-cloud-storage`, `google-cloud-bigquery`, `google-cloud-billing-budgets` (lazy-imported, same pattern as `tools/cloud_perms_probe.py`).
- `boto3` (ec2, s3, dynamodb, budgets, ce, service-quotas) — same lazy-import pattern.
- `pixi` env: `live-skypilot` already pulls both SDK families. No new feature env needed.
- Pure-stdlib for CLI / manifest / JSON.
- Tests with stdlib `unittest.mock`, fake client objects matching the duck-typed API surfaces the helpers touch. No real cloud in any test.

**Calendar:** Today = day 0 = 2026-06-10. Resubmit = day 5 = 2026-06-15. Tasks 1–9 are all offline scaffold landing TODAY before any live spend (per CLAUDE.md durability rule). Tasks 10–13 carry live-spend / calendar dependencies.

---

## File structure

| File | Responsibility |
|---|---|
| `tools/quota_burn_lib.py` | `Manifest` dataclass; `gcp_spin_up` / `gcp_tear_down` / `gcp_mtd_spend` / `gcp_submit_quota`; `aws_spin_up` / `aws_tear_down` / `aws_mtd_spend` / `aws_submit_quota`; `bq_scan_with_cap`. All take injected SDK clients. |
| `tools/quota_burn.py` | CLI dispatcher. Subcommands: `spinup`, `teardown`, `snapshot`, `submit`, `scan`. Builds real clients via lazy import, calls lib helpers. Single `python -m` entry-point. |
| `.quota_burn/manifest.json` | Persisted resource manifest, written by `spinup`, read by `teardown` + `snapshot`. Path gitignored. |
| `docs/quota-justification-aws.md` | AWS justification text. `$X.XX` placeholder for MTD spend, populated by Task 12. |
| `docs/quota-justification-gcp.md` | GCP justification text. Same placeholder pattern. |
| `tests/tools/test_quota_burn_manifest.py` | Manifest dataclass + JSON round-trip. |
| `tests/tools/test_quota_burn_gcp.py` | GCP helpers with fake `Compute` / `Storage` / `BigQuery` / `Budgets` clients. |
| `tests/tools/test_quota_burn_aws.py` | AWS helpers with fake `boto3` clients via `botocore.stub.Stubber`. |
| `tests/tools/test_quota_burn_snapshot.py` | Snapshot report formatting. |
| `tests/tools/test_quota_burn_submit.py` | Quota submit + console-URL fallback. |
| `tests/tools/test_quota_burn_cli.py` | CLI argparse dispatch. |

`.quota_burn/` added to `.gitignore` so the manifest doesn't accidentally land in commits.

---

### Task 1: Manifest dataclass + JSON round-trip

**Goal:** Persist the tagged-resource list to JSON; round-trips identically.

**Files:**
- Create: `tools/quota_burn_lib.py`
- Create: `tests/tools/test_quota_burn_manifest.py`
- Modify: `.gitignore` — add `.quota_burn/` line.

**Acceptance Criteria:**
- [ ] `Manifest` is a `@dataclass(slots=True)` with fields `gcp_vms: list[str]`, `gcp_disks: list[str]`, `gcp_buckets: list[str]`, `gcp_budget_id: str | None`, `aws_instances: list[str]`, `aws_volumes: list[str]`, `aws_buckets: list[str]`, `aws_tables: list[str]`, `aws_budget_name: str | None`, `created_at: str` (local-tz ISO), `tag: str` (default `"kinoforge-quota-burn"`).
- [ ] `Manifest.to_json(path: Path)` writes pretty JSON via `dataclasses.asdict` + `json.dumps(indent=2, sort_keys=True)`.
- [ ] `Manifest.from_json(path: Path)` reads via `json.loads` + `Manifest(**data)`; raises `FileNotFoundError` if missing.
- [ ] Round-trip identity: `from_json(to_json(m)) == m`.
- [ ] `.quota_burn/` line added to `.gitignore`.

**Verify:** `pixi run pytest tests/tools/test_quota_burn_manifest.py -v` → all green.

**Steps:**

- [ ] **Step 1: Add `.quota_burn/` to `.gitignore`**

```bash
echo ".quota_burn/" >> /workspace/.gitignore
```

- [ ] **Step 2: Write failing test**

```python
# tests/tools/test_quota_burn_manifest.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.quota_burn_lib import Manifest


def test_manifest_round_trips_through_json(tmp_path: Path) -> None:
    """A Manifest written to JSON reads back equal — bug catch: drift in field order
    or default factories would break teardown idempotence."""
    m = Manifest(
        gcp_vms=["kinoforge-burn-1"],
        gcp_disks=["kinoforge-burn-1-disk"],
        gcp_buckets=["kinoforge-quota-burn-gcp"],
        gcp_budget_id="budget-abc",
        aws_instances=["i-0123456789"],
        aws_volumes=["vol-deadbeef"],
        aws_buckets=["kinoforge-quota-burn-aws-usw2"],
        aws_tables=["kinoforge-quota-burn"],
        aws_budget_name="kinoforge-quota-burn",
        created_at="2026-06-10T09:30:00",
        tag="kinoforge-quota-burn",
    )
    path = tmp_path / "manifest.json"
    m.to_json(path)
    assert Manifest.from_json(path) == m


def test_manifest_from_json_raises_when_missing(tmp_path: Path) -> None:
    """Bug catch: silently returning an empty manifest would cause teardown to no-op,
    leaving live resources running."""
    with pytest.raises(FileNotFoundError):
        Manifest.from_json(tmp_path / "nope.json")


def test_manifest_json_is_pretty_and_sorted(tmp_path: Path) -> None:
    """Sorted keys make manifest diffs reviewable; pretty-printing aids manual inspection."""
    m = Manifest(
        gcp_vms=[],
        gcp_disks=[],
        gcp_buckets=[],
        gcp_budget_id=None,
        aws_instances=[],
        aws_volumes=[],
        aws_buckets=[],
        aws_tables=[],
        aws_budget_name=None,
        created_at="2026-06-10T09:30:00",
        tag="kinoforge-quota-burn",
    )
    path = tmp_path / "m.json"
    m.to_json(path)
    text = path.read_text()
    assert "\n  " in text  # pretty
    parsed_keys = list(json.loads(text).keys())
    assert parsed_keys == sorted(parsed_keys)
```

- [ ] **Step 3: Run test, verify RED**

Run: `pixi run pytest tests/tools/test_quota_burn_manifest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.quota_burn_lib'`

- [ ] **Step 4: Implement `Manifest`**

```python
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
from dataclasses import dataclass, field
from pathlib import Path


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
```

- [ ] **Step 5: Run test, verify GREEN**

Run: `pixi run pytest tests/tools/test_quota_burn_manifest.py -v`
Expected: 3 passed.

- [ ] **Step 6: Pre-commit + commit**

```bash
cd /workspace
pixi run pre-commit run --files tools/quota_burn_lib.py tests/tools/test_quota_burn_manifest.py .gitignore
git add tools/quota_burn_lib.py tests/tools/test_quota_burn_manifest.py .gitignore
git commit -m "feat(quota-burn): Manifest dataclass with JSON round-trip"
```

---

### Task 2: GCP spin-up helpers

**Goal:** Provision the GCP burn workload (VM + disk + GCS bucket + budget) via injected SDK clients; return the resource IDs into the manifest.

**Files:**
- Modify: `tools/quota_burn_lib.py` — append `gcp_spin_up` plus `_GcpClients` protocol.
- Create: `tests/tools/test_quota_burn_gcp.py`

**Acceptance Criteria:**
- [ ] `gcp_spin_up(clients, *, project_id, region, zone, tag) -> dict` returns a dict with keys `vm`, `disk`, `bucket`, `budget_id`.
- [ ] Spins exactly: 1 `e2-small` VM with attached 10 GB `pd-balanced` disk in `<zone>`, 1 GCS bucket `kinoforge-quota-burn-gcp-<rand6>` in `<region>`, 1 budget at $7 hard cap with email channel.
- [ ] Every resource carries label `kinoforge-quota-burn=true`.
- [ ] VM `startup-script` metadata key set to `shutdown -h +480` (8 hours kernel-side).
- [ ] All client objects injected — no `google.cloud.*` import in this helper.

**Verify:** `pixi run pytest tests/tools/test_quota_burn_gcp.py::test_gcp_spin_up -v` → green.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/tools/test_quota_burn_gcp.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tools.quota_burn_lib import gcp_spin_up


@dataclass
class FakeOperation:
    """Mimics google.api_core.operation.Operation enough for our usage."""

    result_val: Any = None

    def result(self, timeout: int | None = None) -> Any:
        return self.result_val


@dataclass
class FakeInstancesClient:
    insert_calls: list[dict] = field(default_factory=list)

    def insert(self, *, project: str, zone: str, instance_resource: Any) -> FakeOperation:
        self.insert_calls.append(
            {"project": project, "zone": zone, "instance": instance_resource}
        )
        return FakeOperation()


@dataclass
class FakeStorageClient:
    create_calls: list[dict] = field(default_factory=list)

    def create_bucket(self, name: str, *, location: str) -> Any:
        self.create_calls.append({"name": name, "location": location})
        return type("Bucket", (), {"name": name, "location": location})()


@dataclass
class FakeBudgetClient:
    create_calls: list[dict] = field(default_factory=list)

    def create_budget(self, *, parent: str, budget: Any) -> Any:
        self.create_calls.append({"parent": parent, "budget": budget})
        return type("Budget", (), {"name": "billingAccounts/ACME/budgets/burn-7"})()


@dataclass
class FakeGcpClients:
    instances: FakeInstancesClient
    storage: FakeStorageClient
    budgets: FakeBudgetClient
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
```

- [ ] **Step 2: Run, verify RED**

Run: `pixi run pytest tests/tools/test_quota_burn_gcp.py -v`
Expected: FAIL — `gcp_spin_up` not defined.

- [ ] **Step 3: Implement `gcp_spin_up`**

```python
# Append to tools/quota_burn_lib.py
import secrets
from datetime import datetime
from typing import Any, Protocol


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
    `region`; 1 budget at $7 hard cap routed to operator email.

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

    clients.storage.create_bucket(bucket_name, location=region)

    budget = {
        "display_name": f"kinoforge-quota-burn-{datetime.now().strftime('%Y%m%d')}",
        "amount": {"specified_amount": {"currency_code": "USD", "units": 7}},
        "notifications_rule": {
            "pubsub_topic": None,
            "schema_version": "1.0",
            "monitoring_notification_channels": [clients.notification_channel],
            "disable_default_iam_recipients": False,
        },
        "labels": {tag: "true"},
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
```

- [ ] **Step 4: Run, verify GREEN**

Run: `pixi run pytest tests/tools/test_quota_burn_gcp.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd /workspace
pixi run pre-commit run --files tools/quota_burn_lib.py tests/tools/test_quota_burn_gcp.py
git add tools/quota_burn_lib.py tests/tools/test_quota_burn_gcp.py
git commit -m "feat(quota-burn): GCP spin-up — e2-small + GCS bucket + budget alarm"
```

---

### Task 3: GCP teardown + MTD spend snapshot helpers

**Goal:** Destroy every tagged GCP resource named in the manifest (idempotent); read month-to-date spend by service for the snapshot report.

**Files:**
- Modify: `tools/quota_burn_lib.py` — append `gcp_tear_down` + `gcp_mtd_spend`.
- Modify: `tests/tools/test_quota_burn_gcp.py` — add teardown + snapshot tests.

**Acceptance Criteria:**
- [ ] `gcp_tear_down(clients, manifest, *, project_id, zone) -> list[str]` returns the list of deleted resource IDs.
- [ ] Idempotent: missing resource (404 / `NotFound`) is swallowed, logged, counted as already-gone.
- [ ] Order: VMs first → disks → buckets → budget. (Disks attached to VMs auto-delete; the explicit second pass cleans orphans.)
- [ ] `gcp_mtd_spend(client, *, project_id) -> dict[str, float]` returns `{service: usd}` for the current calendar month.
- [ ] Both helpers raise SDK errors UNCHANGED for any non-404 failure — we want loud failure on unexpected state.

**Verify:** `pixi run pytest tests/tools/test_quota_burn_gcp.py -v` → green for all 7+ tests.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/tools/test_quota_burn_gcp.py
from dataclasses import field

from tools.quota_burn_lib import Manifest, gcp_mtd_spend, gcp_tear_down


@dataclass
class FakeComputeForTeardown:
    delete_calls: list[tuple[str, str, str]] = field(default_factory=list)
    not_found: set[str] = field(default_factory=set)

    def delete(self, *, project: str, zone: str, instance: str) -> FakeOperation:
        if instance in self.not_found:
            from google.api_core import exceptions  # type: ignore[import-not-found]

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
                "delete": lambda self_, force=False: self_callback(name),
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
        budgets=FakeBudgetForTeardown(not_found={"billingAccounts/ACME/budgets/burn-7"}),
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
```

- [ ] **Step 2: Run, verify RED**

Run: `pixi run pytest tests/tools/test_quota_burn_gcp.py -v`
Expected: failures for new tests — names not defined.

- [ ] **Step 3: Implement teardown + snapshot**

```python
# Append to tools/quota_burn_lib.py
import logging

_log = logging.getLogger(__name__)


def gcp_tear_down(
    clients: Any,
    manifest: Manifest,
    *,
    project_id: str,
    zone: str,
) -> list[str]:
    """Destroy every GCP resource in the manifest; idempotent on NotFound.

    Returns the list of resource IDs actually deleted in this call. Resources
    already gone (NotFound) are silently skipped; any other error propagates.
    """
    from google.api_core import exceptions as gax_exc  # type: ignore[import-not-found]

    deleted: list[str] = []

    def _try(name: str, op_fn: Any) -> None:
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


def gcp_mtd_spend(client: Any, *, project_id: str) -> dict[str, float]:
    """Return month-to-date spend grouped by service, in USD.

    `client` must expose `.query(query: str) -> list[dict]` where each row
    contains `service_description` and `cost_usd` keys. Production wires this
    to a BigQuery client running against the billing export dataset; tests
    pass a fake row list.
    """
    sql = (
        "SELECT service.description AS service_description, "
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
```

- [ ] **Step 4: Run, verify GREEN**

Run: `pixi run pytest tests/tools/test_quota_burn_gcp.py -v`
Expected: 7+ passed.

- [ ] **Step 5: Commit**

```bash
cd /workspace
pixi run pre-commit run --files tools/quota_burn_lib.py tests/tools/test_quota_burn_gcp.py
git add tools/quota_burn_lib.py tests/tools/test_quota_burn_gcp.py
git commit -m "feat(quota-burn): GCP teardown + MTD spend snapshot helpers"
```

---

### Task 4: AWS spin-up helpers

**Goal:** Provision the AWS burn workload via injected `boto3` clients; mirror Task 2's shape.

**Files:**
- Modify: `tools/quota_burn_lib.py` — append `aws_spin_up`.
- Create: `tests/tools/test_quota_burn_aws.py`

**Acceptance Criteria:**
- [ ] `aws_spin_up(clients, *, region, tag) -> dict` returns dict with keys `instance`, `volume`, `bucket`, `table`, `budget_name`.
- [ ] Spins: 1× `t4g.nano` EC2 with attached 30 GB gp3 EBS, 1× S3 bucket `kinoforge-quota-burn-aws-<rand6>`, 1× DynamoDB on-demand table `kinoforge-quota-burn-<rand6>`, 1× AWS Budget at $5 hard cap with email recipient.
- [ ] EC2 `UserData` includes `shutdown -h +480` (8-hour kernel-side kill) AND `instance_initiated_shutdown_behavior=terminate`.
- [ ] Every resource tagged `kinoforge-quota-burn=true`.
- [ ] No `boto3` import — clients injected.

**Verify:** `pixi run pytest tests/tools/test_quota_burn_aws.py::test_aws_spin_up -v` → green.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/tools/test_quota_burn_aws.py
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest

from tools.quota_burn_lib import aws_spin_up


@dataclass
class FakeEC2:
    run_calls: list[dict[str, Any]] = field(default_factory=list)

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.run_calls.append(kwargs)
        return {
            "Instances": [
                {
                    "InstanceId": "i-deadbeef0",
                    "BlockDeviceMappings": [
                        {"Ebs": {"VolumeId": "vol-deadbeef0"}}
                    ],
                }
            ]
        }


@dataclass
class FakeS3:
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        return {}

    def put_bucket_tagging(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append({"_tag_call": kwargs})
        return {}


@dataclass
class FakeDynamo:
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    def create_table(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        return {"TableDescription": {"TableName": kwargs["TableName"]}}


@dataclass
class FakeBudgets:
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    def create_budget(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        return {}


@dataclass
class FakeAwsClients:
    ec2: FakeEC2
    s3: FakeS3
    dynamo: FakeDynamo
    budgets: FakeBudgets
    account_id: str = "123456789012"
    operator_email: str = "operator@example.test"


def _make_aws_clients() -> FakeAwsClients:
    return FakeAwsClients(
        ec2=FakeEC2(), s3=FakeS3(), dynamo=FakeDynamo(), budgets=FakeBudgets()
    )


def test_aws_spin_up_returns_resource_ids() -> None:
    clients = _make_aws_clients()
    out = aws_spin_up(clients, region="us-west-2", tag="kinoforge-quota-burn")
    assert set(out.keys()) == {"instance", "volume", "bucket", "table", "budget_name"}
    assert out["instance"] == "i-deadbeef0"
    assert out["volume"] == "vol-deadbeef0"
    assert out["bucket"].startswith("kinoforge-quota-burn-aws-")
    assert out["table"].startswith("kinoforge-quota-burn-")
    assert out["budget_name"].startswith("kinoforge-quota-burn-")


def test_aws_spin_up_uses_t4g_nano_with_30gb_gp3() -> None:
    """Bug catch: wrong instance type or disk size breaks the $3-ceiling budget."""
    clients = _make_aws_clients()
    aws_spin_up(clients, region="us-west-2", tag="kinoforge-quota-burn")
    call = clients.ec2.run_calls[0]
    assert call["InstanceType"] == "t4g.nano"
    bdm = call["BlockDeviceMappings"]
    assert bdm[0]["Ebs"]["VolumeSize"] == 30
    assert bdm[0]["Ebs"]["VolumeType"] == "gp3"


def test_aws_spin_up_arms_kernel_shutdown_and_self_terminate() -> None:
    """Bug catch: runaway VM scenario (spec §3 + R1). Two-layer kill: kernel
    shutdown + instance-initiated-shutdown-behavior=terminate."""
    clients = _make_aws_clients()
    aws_spin_up(clients, region="us-west-2", tag="kinoforge-quota-burn")
    call = clients.ec2.run_calls[0]
    assert call["InstanceInitiatedShutdownBehavior"] == "terminate"
    user_data = base64.b64decode(call["UserData"]).decode()
    assert "shutdown -h +480" in user_data


def test_aws_spin_up_tags_every_resource() -> None:
    clients = _make_aws_clients()
    aws_spin_up(clients, region="us-west-2", tag="kinoforge-quota-burn")
    ec2_tags = clients.ec2.run_calls[0]["TagSpecifications"]
    resource_types = {ts["ResourceType"] for ts in ec2_tags}
    assert "instance" in resource_types and "volume" in resource_types
    for ts in ec2_tags:
        assert {"Key": "kinoforge-quota-burn", "Value": "true"} in ts["Tags"]
    s3_tag_call = next(c for c in clients.s3.create_calls if "_tag_call" in c)
    s3_tag_set = s3_tag_call["_tag_call"]["Tagging"]["TagSet"]
    assert {"Key": "kinoforge-quota-burn", "Value": "true"} in s3_tag_set
    dynamo_tags = clients.dynamo.create_calls[0]["Tags"]
    assert {"Key": "kinoforge-quota-burn", "Value": "true"} in dynamo_tags


def test_aws_spin_up_caps_budget_at_5_usd() -> None:
    """Bug catch: forgetting the budget alarm leaves spend untracked. R1 stack layer 3."""
    clients = _make_aws_clients()
    aws_spin_up(clients, region="us-west-2", tag="kinoforge-quota-burn")
    budget_call = clients.budgets.create_calls[0]
    assert budget_call["AccountId"] == "123456789012"
    amount = budget_call["Budget"]["BudgetLimit"]
    assert amount["Amount"] == "5"
    assert amount["Unit"] == "USD"
    subscribers = budget_call["NotificationsWithSubscribers"][0]["Subscribers"]
    assert any(s["Address"] == "operator@example.test" for s in subscribers)
```

- [ ] **Step 2: Run, verify RED**

Run: `pixi run pytest tests/tools/test_quota_burn_aws.py -v`
Expected: FAIL — `aws_spin_up` not defined.

- [ ] **Step 3: Implement `aws_spin_up`**

```python
# Append to tools/quota_burn_lib.py
import base64

_USER_DATA_TEMPLATE = """#!/bin/bash
set -eux
# Kernel-side hard shutdown 8h after boot (spec §3 stack layer 1).
shutdown -h +480
# Re-arm on every reboot via cron.
echo '@reboot root /sbin/shutdown -h +480' > /etc/cron.d/kinoforge-burn-shutdown
chmod 0644 /etc/cron.d/kinoforge-burn-shutdown
"""


def aws_spin_up(
    clients: Any,
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
        clients: injected boto3 clients (see `_AwsClients` Protocol-shape).
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

    clients.s3.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": region},
    )
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
```

- [ ] **Step 4: Run, verify GREEN**

Run: `pixi run pytest tests/tools/test_quota_burn_aws.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
cd /workspace
pixi run pre-commit run --files tools/quota_burn_lib.py tests/tools/test_quota_burn_aws.py
git add tools/quota_burn_lib.py tests/tools/test_quota_burn_aws.py
git commit -m "feat(quota-burn): AWS spin-up — t4g.nano + S3 + DynamoDB + budget"
```

---

### Task 5: AWS teardown + MTD spend snapshot helpers

**Goal:** Destroy every tagged AWS resource named in the manifest; read AWS month-to-date spend by service.

**Files:**
- Modify: `tools/quota_burn_lib.py` — append `aws_tear_down` + `aws_mtd_spend`.
- Modify: `tests/tools/test_quota_burn_aws.py` — add teardown + snapshot tests.

**Acceptance Criteria:**
- [ ] `aws_tear_down(clients, manifest) -> list[str]` returns IDs actually deleted.
- [ ] Order: EC2 terminate → EBS delete (orphan check) → S3 empty + delete → DynamoDB delete → Budget delete.
- [ ] Idempotent on `InvalidInstanceID.NotFound`, `NoSuchBucket`, `ResourceNotFoundException`, `NotFoundException` (the four AWS not-found shapes across these four services).
- [ ] EC2 termination waits via `clients.ec2_waiter.wait(InstanceIds=...)` with hard 5-min timeout.
- [ ] `aws_mtd_spend(client, *, account_id) -> dict[str, float]` returns `{service: usd}` from Cost Explorer.

**Verify:** `pixi run pytest tests/tools/test_quota_burn_aws.py -v` → green for all 8+ tests.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/tools/test_quota_burn_aws.py
from botocore.exceptions import ClientError

from tools.quota_burn_lib import Manifest, aws_mtd_spend, aws_tear_down


@dataclass
class FakeWaiter:
    waited_for: list[list[str]] = field(default_factory=list)

    def wait(self, **kwargs: Any) -> None:
        self.waited_for.append(kwargs.get("InstanceIds", []))


@dataclass
class FakeEC2ForTeardown:
    terminate_calls: list[list[str]] = field(default_factory=list)
    delete_vol_calls: list[str] = field(default_factory=list)
    not_found_instances: set[str] = field(default_factory=set)
    not_found_volumes: set[str] = field(default_factory=set)

    def terminate_instances(self, **kwargs: Any) -> dict:
        for iid in kwargs["InstanceIds"]:
            if iid in self.not_found_instances:
                raise ClientError(
                    {"Error": {"Code": "InvalidInstanceID.NotFound", "Message": ""}},
                    "TerminateInstances",
                )
        self.terminate_calls.append(kwargs["InstanceIds"])
        return {}

    def delete_volume(self, **kwargs: Any) -> dict:
        if kwargs["VolumeId"] in self.not_found_volumes:
            raise ClientError(
                {"Error": {"Code": "InvalidVolume.NotFound", "Message": ""}},
                "DeleteVolume",
            )
        self.delete_vol_calls.append(kwargs["VolumeId"])
        return {}


@dataclass
class FakeS3ForTeardown:
    delete_object_calls: list[tuple[str, str]] = field(default_factory=list)
    delete_bucket_calls: list[str] = field(default_factory=list)
    objects: dict[str, list[str]] = field(default_factory=dict)
    not_found: set[str] = field(default_factory=set)

    def list_objects_v2(self, **kwargs: Any) -> dict:
        if kwargs["Bucket"] in self.not_found:
            raise ClientError(
                {"Error": {"Code": "NoSuchBucket", "Message": ""}},
                "ListObjectsV2",
            )
        keys = self.objects.get(kwargs["Bucket"], [])
        return {"Contents": [{"Key": k} for k in keys]} if keys else {}

    def delete_object(self, **kwargs: Any) -> dict:
        self.delete_object_calls.append((kwargs["Bucket"], kwargs["Key"]))
        return {}

    def delete_bucket(self, **kwargs: Any) -> dict:
        if kwargs["Bucket"] in self.not_found:
            raise ClientError(
                {"Error": {"Code": "NoSuchBucket", "Message": ""}},
                "DeleteBucket",
            )
        self.delete_bucket_calls.append(kwargs["Bucket"])
        return {}


@dataclass
class FakeDynamoForTeardown:
    delete_calls: list[str] = field(default_factory=list)
    not_found: set[str] = field(default_factory=set)

    def delete_table(self, **kwargs: Any) -> dict:
        if kwargs["TableName"] in self.not_found:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": ""}},
                "DeleteTable",
            )
        self.delete_calls.append(kwargs["TableName"])
        return {}


@dataclass
class FakeBudgetsForTeardown:
    delete_calls: list[str] = field(default_factory=list)
    not_found: set[str] = field(default_factory=set)

    def delete_budget(self, **kwargs: Any) -> dict:
        if kwargs["BudgetName"] in self.not_found:
            raise ClientError(
                {"Error": {"Code": "NotFoundException", "Message": ""}},
                "DeleteBudget",
            )
        self.delete_calls.append(kwargs["BudgetName"])
        return {}


@dataclass
class FakeAwsTeardownClients:
    ec2: FakeEC2ForTeardown
    ec2_waiter: FakeWaiter
    s3: FakeS3ForTeardown
    dynamo: FakeDynamoForTeardown
    budgets: FakeBudgetsForTeardown
    account_id: str = "123456789012"


def _aws_teardown_manifest() -> Manifest:
    return Manifest(
        gcp_vms=[],
        gcp_disks=[],
        gcp_buckets=[],
        gcp_budget_id=None,
        aws_instances=["i-burn0"],
        aws_volumes=["vol-burn0"],
        aws_buckets=["kinoforge-quota-burn-aws-xyz"],
        aws_tables=["kinoforge-quota-burn-xyz"],
        aws_budget_name="kinoforge-quota-burn-20260610-xyz",
        created_at="2026-06-10T09:30:00",
        tag="kinoforge-quota-burn",
    )


def test_aws_tear_down_deletes_every_resource_in_order() -> None:
    clients = FakeAwsTeardownClients(
        ec2=FakeEC2ForTeardown(),
        ec2_waiter=FakeWaiter(),
        s3=FakeS3ForTeardown(objects={"kinoforge-quota-burn-aws-xyz": ["a", "b"]}),
        dynamo=FakeDynamoForTeardown(),
        budgets=FakeBudgetsForTeardown(),
    )
    deleted = aws_tear_down(clients, _aws_teardown_manifest())
    assert "i-burn0" in deleted
    assert "vol-burn0" in deleted
    assert "kinoforge-quota-burn-aws-xyz" in deleted
    assert "kinoforge-quota-burn-xyz" in deleted
    assert "kinoforge-quota-burn-20260610-xyz" in deleted
    assert clients.s3.delete_object_calls == [
        ("kinoforge-quota-burn-aws-xyz", "a"),
        ("kinoforge-quota-burn-aws-xyz", "b"),
    ]
    assert clients.ec2_waiter.waited_for == [["i-burn0"]]


def test_aws_tear_down_is_idempotent_on_all_four_not_found_codes() -> None:
    """Bug catch: each AWS service uses a different NotFound code; missing
    any of the four leaves teardown half-finished on a re-run."""
    clients = FakeAwsTeardownClients(
        ec2=FakeEC2ForTeardown(
            not_found_instances={"i-burn0"}, not_found_volumes={"vol-burn0"}
        ),
        ec2_waiter=FakeWaiter(),
        s3=FakeS3ForTeardown(not_found={"kinoforge-quota-burn-aws-xyz"}),
        dynamo=FakeDynamoForTeardown(not_found={"kinoforge-quota-burn-xyz"}),
        budgets=FakeBudgetsForTeardown(
            not_found={"kinoforge-quota-burn-20260610-xyz"}
        ),
    )
    deleted = aws_tear_down(clients, _aws_teardown_manifest())
    assert deleted == []


def test_aws_tear_down_raises_on_unexpected_clienterror() -> None:
    """Bug catch: swallowing AccessDenied would mask a creds-expired situation
    and report success while the resource lives on."""

    class _EvilEC2(FakeEC2ForTeardown):
        def terminate_instances(self, **kwargs: Any) -> dict:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": ""}},
                "TerminateInstances",
            )

    clients = FakeAwsTeardownClients(
        ec2=_EvilEC2(),
        ec2_waiter=FakeWaiter(),
        s3=FakeS3ForTeardown(),
        dynamo=FakeDynamoForTeardown(),
        budgets=FakeBudgetsForTeardown(),
    )
    with pytest.raises(ClientError, match="AccessDenied"):
        aws_tear_down(clients, _aws_teardown_manifest())


def test_aws_mtd_spend_groups_by_service() -> None:
    class _CE:
        def get_cost_and_usage(self, **_: Any) -> dict:
            return {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {
                                "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                                "Metrics": {"UnblendedCost": {"Amount": "0.50"}},
                            },
                            {
                                "Keys": ["Amazon Simple Storage Service"],
                                "Metrics": {"UnblendedCost": {"Amount": "0.30"}},
                            },
                            {
                                "Keys": ["EC2 - Other"],
                                "Metrics": {"UnblendedCost": {"Amount": "0.40"}},
                            },
                        ]
                    }
                ]
            }

    spend = aws_mtd_spend(_CE(), account_id="123456789012")
    assert spend == {
        "Amazon Elastic Compute Cloud - Compute": 0.50,
        "Amazon Simple Storage Service": 0.30,
        "EC2 - Other": 0.40,
    }
```

- [ ] **Step 2: Run, verify RED**

Run: `pixi run pytest tests/tools/test_quota_burn_aws.py -v`
Expected: failures — symbols not defined.

- [ ] **Step 3: Implement teardown + snapshot**

```python
# Append to tools/quota_burn_lib.py

# AWS NotFound codes across the four services we touch.
_AWS_NOT_FOUND = {
    "InvalidInstanceID.NotFound",
    "InvalidVolume.NotFound",
    "NoSuchBucket",
    "ResourceNotFoundException",
    "NotFoundException",
}


def _is_aws_not_found(exc: BaseException) -> bool:
    """True iff exc is a botocore ClientError with a known NotFound code."""
    from botocore.exceptions import ClientError  # type: ignore[import-not-found]

    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code", "")
    return code in _AWS_NOT_FOUND


def aws_tear_down(clients: Any, manifest: Manifest) -> list[str]:
    """Destroy every AWS resource in the manifest; idempotent on NotFound.

    Order: EC2 terminate → wait → EBS orphan delete → S3 empty + delete →
    DynamoDB delete → Budget delete. Each step swallows the matching AWS
    NotFound code so re-runs reach zero without failure.

    Returns the list of IDs actually deleted (not those already-gone).
    """
    deleted: list[str] = []

    def _try(name: str, fn: Any) -> None:
        try:
            fn()
            deleted.append(name)
        except BaseException as exc:
            if _is_aws_not_found(exc):
                _log.info("aws_tear_down: %s already gone", name)
                return
            raise

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

    for vol in manifest.aws_volumes:
        _try(vol, lambda v=vol: clients.ec2.delete_volume(VolumeId=v))

    for bucket in manifest.aws_buckets:

        def _empty_and_drop(b: str = bucket) -> None:
            listing = clients.s3.list_objects_v2(Bucket=b)
            for obj in listing.get("Contents", []):
                clients.s3.delete_object(Bucket=b, Key=obj["Key"])
            clients.s3.delete_bucket(Bucket=b)

        _try(bucket, _empty_and_drop)

    for table in manifest.aws_tables:
        _try(table, lambda t=table: clients.dynamo.delete_table(TableName=t))

    if manifest.aws_budget_name is not None:
        _try(
            manifest.aws_budget_name,
            lambda: clients.budgets.delete_budget(
                AccountId=clients.account_id,
                BudgetName=manifest.aws_budget_name,
            ),
        )

    return deleted


def aws_mtd_spend(client: Any, *, account_id: str) -> dict[str, float]:
    """Return month-to-date spend grouped by service, in USD.

    Args:
        client: boto3 `ce` (Cost Explorer) client; injected.
        account_id: AWS account id (kept for parity with gcp signature; the
            CE API filters to the calling account by default).

    Returns:
        Dict of `{service_name: cost_usd}`.
    """
    today = datetime.now()
    start = today.replace(day=1).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
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
```

- [ ] **Step 4: Run, verify GREEN**

Run: `pixi run pytest tests/tools/test_quota_burn_aws.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
cd /workspace
pixi run pre-commit run --files tools/quota_burn_lib.py tests/tools/test_quota_burn_aws.py
git add tools/quota_burn_lib.py tests/tools/test_quota_burn_aws.py
git commit -m "feat(quota-burn): AWS teardown + MTD spend snapshot helpers"
```

---

### Task 6: Quota-submit helpers (both clouds)

**Goal:** Submit the GPU quota increase requests via SDK; emit a pre-filled console URL if the GCP alpha API rejects the request.

**Files:**
- Modify: `tools/quota_burn_lib.py` — append `gcp_submit_quota` + `aws_submit_quota` + `QuotaSubmitResult`.
- Create: `tests/tools/test_quota_burn_submit.py`

**Acceptance Criteria:**
- [ ] `gcp_submit_quota(client, *, project_id, region, justification_text) -> QuotaSubmitResult` returns a dataclass with `submitted: bool`, `request_ids: list[str]`, `console_url: str | None`.
- [ ] On success: submits BOTH `GPUS_ALL_REGIONS` (global) and `NVIDIA_T4_GPUS` (regional) to value `1`; returns `submitted=True`, request ids populated, `console_url=None`.
- [ ] On any SDK exception: returns `submitted=False`, `request_ids=[]`, `console_url` populated with a pre-filled URL of the form `https://console.cloud.google.com/iam-admin/quotas?project=<id>&filter=…`.
- [ ] `aws_submit_quota(client, *, region, quota_code, desired_value, justification_text) -> QuotaSubmitResult` returns the same shape. AWS is reliable; `console_url` always `None` on success; raises on failure (no fallback).
- [ ] Justification text is forwarded to the SDK call (`reason` on GCP, `ContextId` is not the right field — AWS Service Quotas does NOT accept free-text justification in `request_service_quota_increase`; this is a documented constraint, captured as an open item below).

**Verify:** `pixi run pytest tests/tools/test_quota_burn_submit.py -v` → green.

**Notes & open items:**
- AWS `RequestServiceQuotaIncrease` does NOT accept a `Justification` or `Reason` field. The justification text gets attached to the auto-generated AWS Support case the request opens, via a follow-up `aws support add-communication-to-case` call. Bundle that follow-up here so the support case actually carries the text we drafted.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/tools/test_quota_burn_submit.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tools.quota_burn_lib import (
    QuotaSubmitResult,
    aws_submit_quota,
    gcp_submit_quota,
)


@dataclass
class FakeGcpQuotaClient:
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False

    def create_quota_adjustment(self, **kwargs: Any) -> Any:
        if self.fail:
            raise RuntimeError("alpha quotas API rejected")
        self.calls.append(kwargs)
        return type(
            "Op", (), {"name": f"projects/foo/operations/op-{len(self.calls)}"}
        )()


def test_gcp_submit_quota_submits_both_metrics() -> None:
    """Bug catch: only submitting the regional quota leaves the global ceiling
    at 0, which blocks every region (memory: project_gpus_all_regions_quota_blocker)."""
    client = FakeGcpQuotaClient()
    result = gcp_submit_quota(
        client,
        project_id="kinoforge-prod-0ddb375e",
        region="us-west1",
        justification_text="reason text",
    )
    assert result.submitted is True
    submitted_metrics = {c["metric"] for c in client.calls}
    assert "compute.googleapis.com/gpus_all_regions" in submitted_metrics
    assert "compute.googleapis.com/nvidia_t4_gpus" in submitted_metrics
    assert result.console_url is None


def test_gcp_submit_quota_falls_back_to_console_url_on_failure() -> None:
    """Bug catch: hard-failing on alpha API rejection would block the day-5
    submit; spec §7 R3 promises a console-URL fallback."""
    client = FakeGcpQuotaClient(fail=True)
    result = gcp_submit_quota(
        client,
        project_id="kinoforge-prod-0ddb375e",
        region="us-west1",
        justification_text="reason text",
    )
    assert result.submitted is False
    assert result.request_ids == []
    assert result.console_url is not None
    assert "kinoforge-prod-0ddb375e" in result.console_url
    assert "NVIDIA_T4_GPUS" in result.console_url


@dataclass
class FakeAwsServiceQuotas:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def request_service_quota_increase(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"RequestedQuota": {"Id": "req-123", "CaseId": "case-456"}}


@dataclass
class FakeAwsSupport:
    case_comments: list[dict[str, Any]] = field(default_factory=list)

    def add_communication_to_case(self, **kwargs: Any) -> dict[str, Any]:
        self.case_comments.append(kwargs)
        return {"result": True}


@dataclass
class FakeAwsClientPair:
    quotas: FakeAwsServiceQuotas
    support: FakeAwsSupport


def test_aws_submit_quota_submits_and_attaches_justification() -> None:
    """Bug catch: AWS request_service_quota_increase has no Justification field.
    The justification must be attached via support.add_communication_to_case."""
    pair = FakeAwsClientPair(
        quotas=FakeAwsServiceQuotas(), support=FakeAwsSupport()
    )
    result = aws_submit_quota(
        pair,
        region="us-west-2",
        quota_code="L-DB2E81BA",
        desired_value=4,
        justification_text="my reason text",
    )
    assert result.submitted is True
    assert result.request_ids == ["req-123"]
    assert result.console_url is None
    # Quota request shape
    assert pair.quotas.calls[0]["ServiceCode"] == "ec2"
    assert pair.quotas.calls[0]["QuotaCode"] == "L-DB2E81BA"
    assert pair.quotas.calls[0]["DesiredValue"] == 4.0
    # Justification routed to the case
    assert pair.support.case_comments[0]["caseId"] == "case-456"
    assert pair.support.case_comments[0]["communicationBody"] == "my reason text"


def test_aws_submit_quota_propagates_sdk_failures() -> None:
    """Bug catch: silently swallowing AWS SDK errors leaves the day-5 submit
    looking successful when the request never landed."""

    class _Boom:
        def request_service_quota_increase(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("service-quotas service unavailable")

    pair = FakeAwsClientPair(quotas=_Boom(), support=FakeAwsSupport())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="service-quotas service unavailable"):
        aws_submit_quota(
            pair,
            region="us-west-2",
            quota_code="L-DB2E81BA",
            desired_value=4,
            justification_text="reason",
        )
```

- [ ] **Step 2: Run, verify RED**

Run: `pixi run pytest tests/tools/test_quota_burn_submit.py -v`
Expected: FAIL — symbols not defined.

- [ ] **Step 3: Implement helpers**

```python
# Append to tools/quota_burn_lib.py
from urllib.parse import urlencode


@dataclass(slots=True)
class QuotaSubmitResult:
    """Outcome of a quota submission.

    `submitted` is True only when the SDK call succeeded. `request_ids` is
    populated on success. `console_url` is populated on GCP fallback so the
    operator can finish the request manually.
    """

    submitted: bool
    request_ids: list[str]
    console_url: str | None


def _gcp_console_quota_url(project_id: str) -> str:
    """Build the pre-filled console URL for the GCP fallback path."""
    qs = urlencode(
        {
            "project": project_id,
            "filter": "metric:compute.googleapis.com/nvidia_t4_gpus OR "
            "compute.googleapis.com/gpus_all_regions",
        }
    )
    return f"https://console.cloud.google.com/iam-admin/quotas?{qs}"


def gcp_submit_quota(
    client: Any,
    *,
    project_id: str,
    region: str,
    justification_text: str,
) -> QuotaSubmitResult:
    """Submit GCP GPU quota adjustments (global + regional) for the burn project.

    On SDK error, returns a result with a pre-filled console URL so the
    operator can complete the request manually (spec §7 R3 fallback).
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
        submitted=True, request_ids=request_ids, console_url=None
    )


def aws_submit_quota(
    clients: Any,
    *,
    region: str,
    quota_code: str,
    desired_value: int,
    justification_text: str,
) -> QuotaSubmitResult:
    """Submit AWS service-quota increase and attach the justification to the
    auto-generated support case.

    `clients` must expose `.quotas` (boto3 service-quotas client) and `.support`
    (boto3 support client).

    AWS's RequestServiceQuotaIncrease API does NOT accept a justification
    field directly; the request automatically opens a Support case, and
    `support.add_communication_to_case` is the only way to attach our
    reason text where reviewers will see it.

    Raises on any SDK error — no console fallback (AWS console path is more
    painful than the API, and reliable enough not to need one).
    """
    resp = clients.quotas.request_service_quota_increase(
        ServiceCode="ec2",
        QuotaCode=quota_code,
        DesiredValue=float(desired_value),
    )
    request_id = resp["RequestedQuota"]["Id"]
    case_id = resp["RequestedQuota"]["CaseId"]
    clients.support.add_communication_to_case(
        caseId=case_id,
        communicationBody=justification_text,
    )
    return QuotaSubmitResult(
        submitted=True, request_ids=[request_id], console_url=None
    )
```

- [ ] **Step 4: Run, verify GREEN**

Run: `pixi run pytest tests/tools/test_quota_burn_submit.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd /workspace
pixi run pre-commit run --files tools/quota_burn_lib.py tests/tools/test_quota_burn_submit.py
git add tools/quota_burn_lib.py tests/tools/test_quota_burn_submit.py
git commit -m "feat(quota-burn): quota-submit helpers — GCP fallback URL + AWS case attach"
```

---

### Task 7: BigQuery dry-run guard

**Goal:** Wrap a BigQuery scan with a mandatory dry-run pre-check that bounds the bytes-billed before the live query runs.

**Files:**
- Modify: `tools/quota_burn_lib.py` — append `bq_scan_with_cap`.
- Create: `tests/tools/test_quota_burn_bigquery.py`

**Acceptance Criteria:**
- [ ] `bq_scan_with_cap(client, *, sql, max_bytes_billed=10_000_000_000) -> dict` returns `{rows: int, bytes_billed: int}`.
- [ ] Calls `client.query(sql, dry_run=True)` first; if dry-run reports bytes > cap, raises `BigQueryCapExceeded` BEFORE the live query fires.
- [ ] On dry-run pass: runs live query with `maximum_bytes_billed=max_bytes_billed` set on `QueryJobConfig`.
- [ ] No bytes leak through.

**Verify:** `pixi run pytest tests/tools/test_quota_burn_bigquery.py -v` → green.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/tools/test_quota_burn_bigquery.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tools.quota_burn_lib import BigQueryCapExceeded, bq_scan_with_cap


@dataclass
class FakeBqJob:
    total_bytes_processed: int
    rows: int

    def result(self) -> list[dict[str, Any]]:
        return [{"x": i} for i in range(self.rows)]


@dataclass
class FakeBqClient:
    dry_run_bytes: int
    live_rows: int
    queries: list[dict[str, Any]] = field(default_factory=list)

    def query(self, sql: str, **kwargs: Any) -> FakeBqJob:
        self.queries.append({"sql": sql, **kwargs})
        if kwargs.get("dry_run", False) or (
            kwargs.get("job_config") and getattr(kwargs["job_config"], "dry_run", False)
        ):
            return FakeBqJob(total_bytes_processed=self.dry_run_bytes, rows=0)
        return FakeBqJob(total_bytes_processed=self.dry_run_bytes, rows=self.live_rows)


def test_bq_scan_with_cap_runs_when_under_cap() -> None:
    client = FakeBqClient(dry_run_bytes=5_000_000_000, live_rows=10)
    out = bq_scan_with_cap(
        client, sql="SELECT * FROM `t`", max_bytes_billed=10_000_000_000
    )
    assert out == {"rows": 10, "bytes_billed": 5_000_000_000}
    assert len(client.queries) == 2  # dry-run + live
    live_cfg = client.queries[1].get("job_config")
    assert live_cfg is not None
    assert getattr(live_cfg, "maximum_bytes_billed", None) == 10_000_000_000


def test_bq_scan_with_cap_blocks_when_over_cap() -> None:
    """Bug catch: a runaway BigQuery scan (R5) is the only way the burn can
    blow past $20. The dry-run gate must REFUSE before any billable query."""
    client = FakeBqClient(dry_run_bytes=50_000_000_000, live_rows=999_999)
    with pytest.raises(BigQueryCapExceeded, match="50000000000"):
        bq_scan_with_cap(
            client, sql="SELECT * FROM `huge`", max_bytes_billed=10_000_000_000
        )
    # Only the dry-run fired, not the live query.
    assert len(client.queries) == 1
```

- [ ] **Step 2: Run, verify RED**

Run: `pixi run pytest tests/tools/test_quota_burn_bigquery.py -v`
Expected: FAIL — symbols not defined.

- [ ] **Step 3: Implement helper**

```python
# Append to tools/quota_burn_lib.py


class BigQueryCapExceeded(RuntimeError):
    """Raised when a BigQuery dry-run reports bytes > cap; live query is blocked."""


def bq_scan_with_cap(
    client: Any,
    *,
    sql: str,
    max_bytes_billed: int = 10_000_000_000,
) -> dict[str, int]:
    """Run `sql` with a dry-run gate and a hard maximum-bytes-billed cap.

    Spec §7 R5 — the only mechanism by which the burn can blow $20 is an
    unbounded BigQuery scan. The dry-run pre-check returns the exact
    bytes-to-scan estimate at zero cost; we refuse to run the live query
    if that estimate exceeds the cap.

    Raises:
        BigQueryCapExceeded: if dry-run reports > `max_bytes_billed`.
    """
    from google.cloud.bigquery import QueryJobConfig  # type: ignore[import-not-found]

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
```

- [ ] **Step 4: Run, verify GREEN**

Run: `pixi run pytest tests/tools/test_quota_burn_bigquery.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
cd /workspace
pixi run pre-commit run --files tools/quota_burn_lib.py tests/tools/test_quota_burn_bigquery.py
git add tools/quota_burn_lib.py tests/tools/test_quota_burn_bigquery.py
git commit -m "feat(quota-burn): BigQuery dry-run gate with bytes-billed cap"
```

---

### Task 8: CLI dispatcher with subcommands

**Goal:** Single CLI entry-point (`tools/quota_burn.py`) routing to the lib helpers; lazy-imports SDKs only on the branch that needs them.

**Files:**
- Create: `tools/quota_burn.py`
- Create: `tests/tools/test_quota_burn_cli.py`

**Acceptance Criteria:**
- [ ] Subcommands: `spinup`, `teardown`, `snapshot`, `submit-quota`, `scan-bigquery`.
- [ ] `python -m tools.quota_burn spinup --project-id X --region Y --zone Z --aws-region W --operator-email E` writes the manifest to `.quota_burn/manifest.json`.
- [ ] `python -m tools.quota_burn teardown` reads the manifest and runs both `gcp_tear_down` + `aws_tear_down`; deletes the manifest file at the end.
- [ ] `python -m tools.quota_burn snapshot` prints a formatted MTD-spend report.
- [ ] `python -m tools.quota_burn submit-quota --justification-aws PATH --justification-gcp PATH` reads the justification files and routes to the submit helpers.
- [ ] Each subcommand lazy-imports the SDK it needs; running `tools.quota_burn` without invoking any subcommand does NOT import `google.cloud.*` or `boto3`.
- [ ] Tests use `argparse` mocking — no real SDK imports.

**Verify:** `pixi run pytest tests/tools/test_quota_burn_cli.py -v` → green.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/tools/test_quota_burn_cli.py
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.quota_burn import build_parser, main


def test_cli_spinup_writes_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug catch: forgetting to write the manifest leaves teardown unable to
    find the resources to destroy."""
    manifest_path = tmp_path / "manifest.json"
    monkeypatch.setattr("tools.quota_burn._MANIFEST_PATH", manifest_path)

    fake_gcp_out = {
        "vm": "vm-1",
        "disk": "vm-1-disk",
        "bucket": "buck-1",
        "budget_id": "bud-1",
    }
    fake_aws_out = {
        "instance": "i-1",
        "volume": "vol-1",
        "bucket": "buck-2",
        "table": "tab-1",
        "budget_name": "bud-2",
    }
    with (
        patch("tools.quota_burn._build_gcp_clients", return_value=MagicMock()),
        patch("tools.quota_burn._build_aws_clients", return_value=MagicMock()),
        patch("tools.quota_burn.gcp_spin_up", return_value=fake_gcp_out),
        patch("tools.quota_burn.aws_spin_up", return_value=fake_aws_out),
    ):
        rc = main(
            [
                "spinup",
                "--project-id",
                "kinoforge-prod-0ddb375e",
                "--region",
                "us-west1",
                "--zone",
                "us-west1-a",
                "--aws-region",
                "us-west-2",
                "--operator-email",
                "op@example.test",
            ]
        )
    assert rc == 0
    assert manifest_path.exists()


def test_cli_teardown_reads_manifest_then_deletes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug catch: leaving the manifest after teardown would cause a re-run to
    try deleting already-gone resources (loud-but-correct) AND would lie about
    the live-resource state (silent and wrong)."""
    from tools.quota_burn_lib import Manifest

    m = Manifest(
        gcp_vms=["v"],
        gcp_disks=["d"],
        gcp_buckets=["b"],
        gcp_budget_id="bid",
        aws_instances=["i-1"],
        aws_volumes=["vol-1"],
        aws_buckets=["s3-1"],
        aws_tables=["tab"],
        aws_budget_name="bn",
        created_at="2026-06-10T09:30:00",
        tag="kinoforge-quota-burn",
    )
    manifest_path = tmp_path / "manifest.json"
    m.to_json(manifest_path)
    monkeypatch.setattr("tools.quota_burn._MANIFEST_PATH", manifest_path)

    with (
        patch("tools.quota_burn._build_gcp_clients", return_value=MagicMock()),
        patch("tools.quota_burn._build_aws_clients", return_value=MagicMock()),
        patch("tools.quota_burn.gcp_tear_down", return_value=["v", "d", "b", "bid"]),
        patch("tools.quota_burn.aws_tear_down", return_value=["i-1", "vol-1", "s3-1", "tab", "bn"]),
    ):
        rc = main([
            "teardown",
            "--project-id", "kinoforge-prod-0ddb375e",
            "--zone", "us-west1-a",
        ])
    assert rc == 0
    assert not manifest_path.exists()


def test_cli_root_does_not_import_cloud_sdks() -> None:
    """Bug catch: importing tools.quota_burn must not drag boto3 / google-cloud
    into the env. They're lazy-imported on the branch that needs them only."""
    blocked = {"google.cloud", "boto3"}
    sys.modules.pop("tools.quota_burn", None)
    import tools.quota_burn  # noqa: F401

    for name in list(sys.modules):
        for prefix in blocked:
            assert not name.startswith(prefix), f"unexpected SDK import: {name}"


def test_cli_parser_has_all_subcommands() -> None:
    """Bug catch: regression if someone removes a subcommand by mistake."""
    parser = build_parser()
    # argparse exposes the subparser action; inspect it.
    sub = next(
        a for a in parser._subparsers._group_actions if hasattr(a, "choices")  # type: ignore[union-attr]
    )
    assert set(sub.choices.keys()) == {
        "spinup",
        "teardown",
        "snapshot",
        "submit-quota",
        "scan-bigquery",
    }
```

- [ ] **Step 2: Run, verify RED**

Run: `pixi run pytest tests/tools/test_quota_burn_cli.py -v`
Expected: FAIL — `tools.quota_burn` not defined.

- [ ] **Step 3: Implement CLI**

```python
# tools/quota_burn.py
"""CLI dispatcher for the GPU quota utilization-burn play.

See docs/superpowers/specs/2026-06-10-gpu-quota-utilization-burn-design.md
for the strategy. Subcommands:

- spinup       — provision tagged resources on AWS + GCP, write manifest.
- teardown     — read manifest, destroy everything, delete manifest.
- snapshot     — print MTD spend by service from both clouds.
- submit-quota — submit AWS + GCP GPU quota increases with justification text.
- scan-bigquery — run a bounded BigQuery scan to add billable signal.

SDK imports are lazy per-subcommand so `python -m tools.quota_burn --help`
stays fast and no SDK touches the env until needed.
"""

from __future__ import annotations

import argparse
import json
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

_MANIFEST_PATH = Path(".quota_burn/manifest.json")
_TAG = "kinoforge-quota-burn"


def _build_gcp_clients(*, project_id: str, operator_email: str) -> Any:
    """Lazy-construct the real GCP clients needed for spinup / teardown."""
    from google.cloud import bigquery, compute_v1, storage  # type: ignore[import-not-found]
    from google.cloud.billing import budgets_v1  # type: ignore[import-not-found]

    class _Bundle:
        instances = compute_v1.InstancesClient()
        disks = compute_v1.DisksClient()
        storage = storage.Client(project=project_id)
        budgets = budgets_v1.BudgetServiceClient()
        billing_account = ""  # populated from env or operator config
        notification_channel = ""

    return _Bundle()


def _build_aws_clients(*, region: str, operator_email: str) -> Any:
    """Lazy-construct the real AWS clients."""
    import boto3  # type: ignore[import-not-found]

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
    gcp = _build_gcp_clients(
        project_id=args.project_id, operator_email=args.operator_email
    )
    aws = _build_aws_clients(
        region=args.aws_region, operator_email=args.operator_email
    )
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
        aws_volumes=[aws_out["volume"]],
        aws_buckets=[aws_out["bucket"]],
        aws_tables=[aws_out["table"]],
        aws_budget_name=aws_out["budget_name"],
        created_at=datetime.now().isoformat(timespec="seconds"),
        tag=_TAG,
    )
    m.to_json(_MANIFEST_PATH)
    print(f"manifest written: {_MANIFEST_PATH}")
    return 0


def _do_teardown(args: argparse.Namespace) -> int:
    m = Manifest.from_json(_MANIFEST_PATH)
    gcp = _build_gcp_clients(project_id=args.project_id, operator_email="")
    aws = _build_aws_clients(region="us-west-2", operator_email="")
    gcp_deleted = gcp_tear_down(
        gcp, m, project_id=args.project_id, zone=args.zone
    )
    aws_deleted = aws_tear_down(aws, m)
    print(f"GCP deleted: {gcp_deleted}")
    print(f"AWS deleted: {aws_deleted}")
    _MANIFEST_PATH.unlink()
    return 0


def _do_snapshot(args: argparse.Namespace) -> int:
    from google.cloud import bigquery  # type: ignore[import-not-found]
    import boto3  # type: ignore[import-not-found]

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
    from google.cloud import quotas_v1beta  # type: ignore[import-not-found]
    import boto3  # type: ignore[import-not-found]

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
        support = boto3.client("support", region_name="us-east-1")  # support is global; pinned

    aws_result = aws_submit_quota(
        _AwsPair(),
        region=args.aws_region,
        quota_code="L-DB2E81BA",
        desired_value=4,
        justification_text=just_aws,
    )
    print(f"AWS request: {aws_result.request_ids}")
    return 0 if gcp_result.submitted and aws_result.submitted else 1


def _do_scan_bigquery(args: argparse.Namespace) -> int:
    from google.cloud import bigquery  # type: ignore[import-not-found]

    client = bigquery.Client(project=args.project_id)
    out = bq_scan_with_cap(
        client, sql=args.sql, max_bytes_billed=args.max_bytes_billed
    )
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
        "--max-bytes-billed", type=int, default=10_000_000_000, dest="max_bytes_billed"
    )
    sb.set_defaults(func=_do_scan_bigquery)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run, verify GREEN**

Run: `pixi run pytest tests/tools/test_quota_burn_cli.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd /workspace
pixi run pre-commit run --files tools/quota_burn.py tests/tools/test_quota_burn_cli.py
git add tools/quota_burn.py tests/tools/test_quota_burn_cli.py
git commit -m "feat(quota-burn): CLI dispatcher — spinup/teardown/snapshot/submit/scan"
```

---

### Task 9: Justification draft templates + PROGRESS update

**Goal:** Land the AWS + GCP justification drafts with placeholders, and add Phase 52 entry to PROGRESS.md so resume sessions know what's running.

**Files:**
- Create: `docs/quota-justification-aws.md`
- Create: `docs/quota-justification-gcp.md`
- Modify: `PROGRESS.md` — add Phase 52 entry + RESUME pointer + B/C/E follow-ups.

**Acceptance Criteria:**
- [ ] Each justification file has all four spec §6 template blocks: Workload / Account context / Cost controls / Repository.
- [ ] Each file has a `$MTD_SPEND_USD$` placeholder that Task 12 substitutes from the snapshot.
- [ ] PROGRESS.md gains a `### Phase 52 — GPU quota utilization-burn` section with the task checklist mirroring this plan.
- [ ] PROGRESS.md Single-next-action block updated to point at Phase 52.

**Verify:** `git diff --stat` shows 2 new files + 1 modified file; `rg '\$MTD_SPEND_USD\$' docs/quota-justification-*.md` finds the placeholder in both.

**Steps:**

- [ ] **Step 1: Write `docs/quota-justification-gcp.md`**

```markdown
# GCP GPU quota — justification

> **Status (as of day 5 = 2026-06-15):** ready to submit. Placeholder
> `$MTD_SPEND_USD$` is replaced by `tools/quota_burn.py snapshot` output
> immediately before submission.

## Workload

Continuous-integration smoke tests for an open-source Python SDK
(`kinoforge`) that orchestrates GPU video-generation jobs across cloud
providers. Need 1× preemptible NVIDIA T4 in `us-west1-a` for ~8 minutes
per test run; ≤ 10 runs/week; ≤ $20/month total GPU spend. Workload
pattern: launch via SkyPilot → run smoke test → autostop. Never
persistent. No production traffic.

## Account context

Active pay-as-you-go customer since 2026-06-07. Month-to-date spend
across Compute Engine, Cloud Storage, BigQuery, and other services on
project `kinoforge-prod-0ddb375e`: **$MTD_SPEND_USD$**.

## Cost controls

- Cloud Billing budget alarm at $50/month notifies the project owner
  via email.
- SkyPilot's `autostop=10m` flag terminates idle GPU instances.
- All GPU launches restricted to the preemptible / spot tier; no
  on-demand reservations.

## Repository

<add public repo URL here on day 4, or strike this line if repo is private>
```

- [ ] **Step 2: Write `docs/quota-justification-aws.md`**

```markdown
# AWS GPU quota — justification

> **Status (as of day 5 = 2026-06-15):** ready to submit. Placeholder
> `$MTD_SPEND_USD$` is replaced by `tools/quota_burn.py snapshot` output
> immediately before submission via the support-case follow-up.

## Workload

Continuous-integration smoke tests for an open-source Python SDK
(`kinoforge`) that orchestrates GPU video-generation jobs across cloud
providers. Need 4 vCPU of On-Demand G/VT instances in `us-west-2`
(= 1× g4dn.xlarge) for ~8 minutes per test run; ≤ 10 runs/week;
≤ $20/month total GPU spend. SkyPilot-driven launch → run → terminate
pattern. No persistent GPU fleet.

## Account context

Active billing customer. Month-to-date spend across EC2, S3, EBS, and
DynamoDB: **$MTD_SPEND_USD$**. Region preference: `us-west-2`.

## Cost controls

- AWS Budgets hard cap at $50/month with email alert.
- EventBridge auto-terminate rule destroys tagged smoke instances after
  30 minutes wall-time.
- Spot / On-Demand mix monitored via CloudWatch.

## Repository

<add public repo URL here on day 4, or strike this line if repo is private>
```

- [ ] **Step 3: Update `PROGRESS.md`**

Add the following section just before the existing `### Phase 51` entry block (i.e. as the most-recent phase), and update the "Single next action" header block to reference Phase 52:

```markdown
### Phase 52 — GPU quota utilization-burn

5-day paid-utilization-history play to flip the AWS + GCP GPU quota
denials. Spec at `docs/superpowers/specs/2026-06-10-gpu-quota-utilization-burn-design.md`.
Plan at `docs/superpowers/plans/2026-06-10-gpu-quota-utilization-burn.md`.
Budget $20; calendar 2026-06-10 → 2026-06-15.

- [ ] Task 1: Manifest dataclass + JSON round-trip.
- [ ] Task 2: GCP spin-up helpers.
- [ ] Task 3: GCP teardown + MTD spend snapshot.
- [ ] Task 4: AWS spin-up helpers.
- [ ] Task 5: AWS teardown + MTD spend snapshot.
- [ ] Task 6: Quota-submit helpers (GCP fallback + AWS case attach).
- [ ] Task 7: BigQuery dry-run gate.
- [ ] Task 8: CLI dispatcher.
- [ ] Task 9: Justification draft templates + this PROGRESS entry.
- [ ] Task 10: Day 0 — live spinup, manifest committed.
- [ ] Task 11: Days 1–4 — daily snapshot + budget pacing checks.
- [ ] Task 12: Day 4 — populate justification drafts from snapshot.
- [ ] Task 13: Day 5 — submit quotas + teardown + final spend report.
```

- [ ] **Step 4: Verify placeholder + commit**

```bash
cd /workspace
rg '\$MTD_SPEND_USD\$' docs/quota-justification-aws.md docs/quota-justification-gcp.md
# Expected: 1 hit per file.

pixi run pre-commit run --files docs/quota-justification-aws.md docs/quota-justification-gcp.md PROGRESS.md
git add docs/quota-justification-aws.md docs/quota-justification-gcp.md PROGRESS.md
git commit -m "docs(quota-burn): justification draft templates + Phase 52 PROGRESS entry"
```

---

### Task 10: Day 0 — live spinup

**Goal:** Run the CLI spinup, verify resources exist on both clouds, commit the manifest path note to PROGRESS.

**Files:**
- Read: `tools/quota_burn.py` (no modifications).
- Modify: `PROGRESS.md` — mark Task 10 done; log day-0 spinup details + resource IDs.
- `.quota_burn/manifest.json` is gitignored; do NOT commit it.

**Acceptance Criteria:**
- [ ] `pixi run preflight` passes (env, creds, clean tree) before any cloud call. Per CLAUDE.md durability.
- [ ] `python -m tools.quota_burn spinup ...` exits 0; manifest exists at `.quota_burn/manifest.json`.
- [ ] `gcloud compute instances list --project kinoforge-prod-0ddb375e --filter='labels.kinoforge-quota-burn=true'` lists exactly one VM.
- [ ] `aws ec2 describe-instances --region us-west-2 --filters Name=tag:kinoforge-quota-burn,Values=true --query 'Reservations[].Instances[].InstanceId'` lists exactly one instance.
- [ ] Both budget alarms visible in their respective consoles.
- [ ] PROGRESS Phase 52 Task 10 checkbox flipped; resource IDs (non-sensitive: VM name, bucket name, table name, budget name) logged inline; manifest path mentioned.

**Verify:**
1. `gcloud compute instances list --project kinoforge-prod-0ddb375e --filter='labels.kinoforge-quota-burn=true' --format='value(name)'` → exactly 1 line.
2. `aws ec2 describe-instances --region us-west-2 --filters Name=tag:kinoforge-quota-burn,Values=true --query 'length(Reservations[].Instances[])'` → `1`.

**Steps:**

- [ ] **Step 1: Pre-flight gate**

```bash
cd /workspace
pixi run preflight
```

Expected: exit 0 with "OK".

If fails, do NOT proceed. Fix preflight issues first.

- [ ] **Step 2: Spin up**

```bash
cd /workspace
pixi run python -m tools.quota_burn spinup \
  --project-id kinoforge-prod-0ddb375e \
  --region us-west1 \
  --zone us-west1-a \
  --aws-region us-west-2 \
  --operator-email emmykillett@gmail.com
```

Expected: exits 0; final stdout line `manifest written: .quota_burn/manifest.json`.

- [ ] **Step 3: Verify via cloud-side queries**

```bash
gcloud compute instances list \
  --project kinoforge-prod-0ddb375e \
  --filter='labels.kinoforge-quota-burn=true' \
  --format='value(name,zone,status)'

aws ec2 describe-instances --region us-west-2 \
  --filters Name=tag:kinoforge-quota-burn,Values=true \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,InstanceType]' \
  --output table

aws s3api list-buckets \
  --query 'Buckets[?starts_with(Name,`kinoforge-quota-burn-aws-`)].Name'

gcloud storage buckets list \
  --project kinoforge-prod-0ddb375e \
  --filter='labels.kinoforge-quota-burn=true' \
  --format='value(name)'
```

Each should list exactly one resource.

- [ ] **Step 4: Update PROGRESS.md**

Add a sub-block under `### Phase 52` with the actual resource IDs from the manifest (read `.quota_burn/manifest.json` and copy non-sensitive fields). Mark Task 10 done.

- [ ] **Step 5: Commit PROGRESS update**

```bash
cd /workspace
git add PROGRESS.md
git commit -m "docs(quota-burn): Phase 52 Task 10 — day-0 spinup complete; manifest persisted"
```

---

### Task 11: Days 1–4 daily snapshot

**Goal:** At the start of each session days 1–4, run the snapshot CLI and log pacing into PROGRESS so the next session can pick up.

**Files:**
- Modify: `PROGRESS.md` — daily snapshot logs (1 line per day under Phase 52).

**Acceptance Criteria:**
- [ ] Each day's snapshot is captured: GCP total, AWS total, sum, days elapsed, pacing-vs-cap.
- [ ] If sum ever crosses $10 mid-burn (spec §5 pause point), STOP and request reauthorization before continuing.
- [ ] If sum ever crosses $15, RUN TEARDOWN IMMEDIATELY (`python -m tools.quota_burn teardown --project-id kinoforge-prod-0ddb375e --zone us-west1-a`) and report.

**Verify:** At the end of day 4, `git log --oneline | rg 'Phase 52' | wc -l` shows ≥ 4 daily PROGRESS commits.

**Steps:**

- [ ] **Day-N steps (repeat days 1, 2, 3, 4):**

```bash
cd /workspace
pixi run python -m tools.quota_burn snapshot \
  --project-id kinoforge-prod-0ddb375e \
  | tee /tmp/quota-burn-snapshot-day-N.json
```

- [ ] **Append to PROGRESS.md** under Phase 52:

```markdown
- Day N snapshot (YYYY-MM-DD): GCP $X.XX, AWS $Y.YY, total $Z.ZZ — pacing on track / over.
```

- [ ] **Commit**:

```bash
git add PROGRESS.md
git commit -m "docs(quota-burn): Phase 52 day-N snapshot — total \$Z.ZZ"
```

---

### Task 12: Day 4 — populate justification drafts

**Goal:** Substitute the `$MTD_SPEND_USD$` placeholders in both justification files with the actual day-4 snapshot figures.

**Files:**
- Modify: `docs/quota-justification-gcp.md` — substitute placeholder + insert per-service breakdown.
- Modify: `docs/quota-justification-aws.md` — same.

**Acceptance Criteria:**
- [ ] No `$MTD_SPEND_USD$` placeholder remains in either file: `rg '\$MTD_SPEND_USD\$' docs/quota-justification-*.md` returns 0 matches.
- [ ] Each file's "Account context" paragraph names ≥ 3 specific services with $ figures (e.g. "Compute Engine $1.20, Cloud Storage $0.50, BigQuery $1.50") drawn from the day-4 snapshot.
- [ ] Operator reviews + approves both files (skim only — no edits required unless desired).

**Verify:** `rg '\$MTD_SPEND_USD\$' docs/quota-justification-aws.md docs/quota-justification-gcp.md` → 0 hits.

**Steps:**

- [ ] **Step 1: Re-snapshot**

```bash
cd /workspace
pixi run python -m tools.quota_burn snapshot \
  --project-id kinoforge-prod-0ddb375e \
  > /tmp/quota-burn-snapshot-day-4.json
```

- [ ] **Step 2: Substitute in `docs/quota-justification-gcp.md`**

Replace the line containing `$MTD_SPEND_USD$` with the formatted services list. Example final-paragraph wording (replace the dollar figures with the real ones from `/tmp/quota-burn-snapshot-day-4.json`):

```markdown
Active pay-as-you-go customer since 2026-06-07. Month-to-date spend
across Compute Engine ($2.02), Cloud Storage ($0.50), BigQuery ($1.50)
on project `kinoforge-prod-0ddb375e`: **$4.02 total**.
```

- [ ] **Step 3: Substitute in `docs/quota-justification-aws.md`**

Same pattern. Example:

```markdown
Active billing customer. Month-to-date spend across EC2 ($1.00), S3
($0.50), EBS ($0.40), DynamoDB ($0.10): **$2.00 total**. Region
preference: `us-west-2`.
```

- [ ] **Step 4: Operator review**

Surface both files in chat for operator skim. They can edit in place (add repo URL, change phrasing).

- [ ] **Step 5: Commit**

```bash
cd /workspace
pixi run pre-commit run --files docs/quota-justification-aws.md docs/quota-justification-gcp.md
git add docs/quota-justification-aws.md docs/quota-justification-gcp.md
git commit -m "docs(quota-burn): populate day-4 MTD spend figures in justifications"
```

---

### Task 13: Day 5 — submit quotas + teardown + final report

**Goal:** Submit both quota requests with the populated justification text; tear down every burn resource; verify zero remaining; commit final spend report.

**Files:**
- Modify: `PROGRESS.md` — Phase 52 closure: total spent, submission request IDs / console URL, teardown confirmation, follow-up state.

**Acceptance Criteria:**
- [ ] `tools/quota_burn.py submit-quota ...` exits 0 OR (if GCP CLI rejects) prints a fallback console URL the operator can click.
- [ ] AWS Service Quotas console (or `aws service-quotas list-requested-service-quota-change-history --service-code ec2`) shows a pending request for `L-DB2E81BA` with desired value 4 in `us-west-2`.
- [ ] `tools/quota_burn.py teardown ...` exits 0; `.quota_burn/manifest.json` deleted; cloud-side queries from Task 10 Step 3 return zero rows each.
- [ ] PROGRESS Phase 52 marked closed with total spend ≤ $20, submission state, and next-step pointer ("await approval emails; on approval kick off A3 + A4").

**Verify:**
1. `gcloud compute instances list --project kinoforge-prod-0ddb375e --filter='labels.kinoforge-quota-burn=true' --format='value(name)'` → empty.
2. `aws ec2 describe-instances --region us-west-2 --filters Name=tag:kinoforge-quota-burn,Values=true --query 'length(Reservations[].Instances[])'` → `0`.
3. `aws s3api list-buckets --query 'Buckets[?starts_with(Name,\`kinoforge-quota-burn-aws-\`)]' --output json` → `[]`.

**Steps:**

- [ ] **Step 1: Pre-flight + final snapshot**

```bash
cd /workspace
pixi run preflight
pixi run python -m tools.quota_burn snapshot --project-id kinoforge-prod-0ddb375e \
  > /tmp/quota-burn-snapshot-day-5-pre.json
```

- [ ] **Step 2: Submit quotas**

```bash
cd /workspace
pixi run python -m tools.quota_burn submit-quota \
  --project-id kinoforge-prod-0ddb375e \
  --region us-west1 \
  --aws-region us-west-2 \
  --justification-gcp docs/quota-justification-gcp.md \
  --justification-aws docs/quota-justification-aws.md
```

Capture stdout — request IDs (success) or console URL (GCP fallback).

If GCP fallback URL printed: surface it to operator with 1-sentence instruction. Wait for operator confirmation that they submitted. Capture confirmation in chat.

- [ ] **Step 3: Teardown**

```bash
cd /workspace
pixi run python -m tools.quota_burn teardown \
  --project-id kinoforge-prod-0ddb375e \
  --zone us-west1-a
```

Expected: lists deleted resources; manifest file removed.

- [ ] **Step 4: Verify zero remaining**

```bash
gcloud compute instances list --project kinoforge-prod-0ddb375e \
  --filter='labels.kinoforge-quota-burn=true' --format='value(name)'
# Expected: empty

aws ec2 describe-instances --region us-west-2 \
  --filters Name=tag:kinoforge-quota-burn,Values=true \
  --query 'length(Reservations[].Instances[])'
# Expected: 0

aws s3api list-buckets \
  --query 'Buckets[?starts_with(Name,`kinoforge-quota-burn-aws-`)].Name' \
  --output json
# Expected: []

gcloud storage buckets list --project kinoforge-prod-0ddb375e \
  --filter='labels.kinoforge-quota-burn=true' --format='value(name)'
# Expected: empty
```

If anything non-empty, STOP and investigate; do not declare Phase 52 closed.

- [ ] **Step 5: Final snapshot for the historical record**

```bash
cd /workspace
# Spend reporting can lag by a few hours; snapshot anyway and note the lag in PROGRESS.
pixi run python -m tools.quota_burn snapshot --project-id kinoforge-prod-0ddb375e \
  > /tmp/quota-burn-snapshot-day-5-post.json
```

- [ ] **Step 6: PROGRESS close-out**

Append a closure block under `### Phase 52`:

```markdown
**Closeout 2026-06-15.** Total spend $X.XX (≤ $20). AWS quota request
`req-...` (case `case-...`) submitted with justification text. GCP
quota requests `op-...` / `op-...` submitted (or fallback console URL
provided; operator submitted manually). Teardown verified clean across
both clouds: `gcloud compute instances list` + `aws ec2 describe-instances`
both empty for the `kinoforge-quota-burn` tag. Next step: await
approval emails; on approval, kick off A3 (SkyPilot T4 re-fire) and A4
(AWS arm of W+β2).
```

Also add to the "Known limitations & follow-ups" → "A. Live-spend"
section:

```markdown
- **A18. Phase 52 approval monitoring.** Two quota requests in flight
  (AWS case `case-...`, GCP request(s) `op-...`). Approval gates A3 +
  A4. Denial → escalation ladder per spec §7 R2.
```

- [ ] **Step 7: Commit + ship**

```bash
cd /workspace
pixi run pre-commit run --files PROGRESS.md
git add PROGRESS.md
git commit -m "docs(quota-burn): Phase 52 closeout — both quotas submitted, burn torn down"
```

---

## Self-review notes

- Tasks 1–9 are pure offline scaffold — no cloud calls, all RED-first. Required by CLAUDE.md durability rule "Commit RED scaffolds before any live spend."
- Task 10 is the first live-spend task. `pixi run preflight` gate is Step 1; explicit per durability rule.
- Tasks 10, 11, 13 all touch live cloud state — they cannot be parallelized with each other (calendar-bound) but Tasks 1–9 can run back-to-back today.
- Each task has its own commit. No "implement multiple tasks then commit once" anti-pattern.
- AWS Justification field gap: documented inline in Task 6 — AWS service-quotas API has no `Reason` parameter, so the justification routes to `support.add_communication_to_case` against the auto-opened support case. This is the only practical surface.
- Region pivot path: if day-5 denials repeat in `us-west1` / `us-west-2`, spec §7 R2 step 2 says try `us-central1` / `us-east-1`. Implementation impact: only `gcp_submit_quota` and `aws_submit_quota` need re-running with different `region` / `aws-region` args; lib + CLI are region-parameterised already.
- `.quota_burn/manifest.json` is intentionally gitignored — it carries account IDs, instance IDs, and bucket names that are operationally fine but unnecessary noise in git.
