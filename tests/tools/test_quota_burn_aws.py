# tests/tools/test_quota_burn_aws.py
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest
from botocore.exceptions import ClientError

from tools.quota_burn_lib import Manifest, aws_mtd_spend, aws_spin_up, aws_tear_down


@dataclass
class FakeEC2:
    run_calls: list[dict[str, Any]] = field(default_factory=list)

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.run_calls.append(kwargs)
        return {
            "Instances": [
                {
                    "InstanceId": "i-deadbeef0",
                    "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-deadbeef0"}}],
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
    """Bug catch: returning the wrong shape breaks Manifest construction in the CLI dispatcher."""
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
    """Bug catch: untagged resources are invisible to teardown's tag-filter and would leak budget at live time."""
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


def test_aws_spin_up_omits_create_bucket_config_for_us_east_1() -> None:
    """Bug catch: S3 raises IllegalLocationConstraintException if
    CreateBucketConfiguration is passed for us-east-1 (the global endpoint
    region). The guard prevents a silent live-time failure if the project
    ever flips off the Oregon-default-region policy."""
    clients = _make_aws_clients()
    aws_spin_up(clients, region="us-east-1", tag="kinoforge-quota-burn")
    create_call = clients.s3.create_calls[0]
    assert "CreateBucketConfiguration" not in create_call


# ---------------------------------------------------------------------------
# Task 5: AWS teardown + MTD spend snapshot helpers
# ---------------------------------------------------------------------------


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

    def terminate_instances(self, **kwargs: Any) -> dict:  # type: ignore[type-arg]
        for iid in kwargs["InstanceIds"]:
            if iid in self.not_found_instances:
                raise ClientError(
                    {"Error": {"Code": "InvalidInstanceID.NotFound", "Message": ""}},
                    "TerminateInstances",
                )
        self.terminate_calls.append(kwargs["InstanceIds"])
        return {}

    def delete_volume(self, **kwargs: Any) -> dict:  # type: ignore[type-arg]
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

    def list_objects_v2(self, **kwargs: Any) -> dict:  # type: ignore[type-arg]
        if kwargs["Bucket"] in self.not_found:
            raise ClientError(
                {"Error": {"Code": "NoSuchBucket", "Message": ""}},
                "ListObjectsV2",
            )
        keys = self.objects.get(kwargs["Bucket"], [])
        return {"Contents": [{"Key": k} for k in keys]} if keys else {}

    def delete_object(self, **kwargs: Any) -> dict:  # type: ignore[type-arg]
        self.delete_object_calls.append((kwargs["Bucket"], kwargs["Key"]))
        return {}

    def delete_bucket(self, **kwargs: Any) -> dict:  # type: ignore[type-arg]
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

    def delete_table(self, **kwargs: Any) -> dict:  # type: ignore[type-arg]
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

    def delete_budget(self, **kwargs: Any) -> dict:  # type: ignore[type-arg]
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
    """Bug catch: missing a teardown step leaves live spend; order assertion
    ensures EC2 terminates before EBS orphan-delete (spec §3 teardown order)."""
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
    # EC2 must appear before EBS in the deleted list (ordering check).
    assert deleted.index("i-burn0") < deleted.index("vol-burn0")
    # EBS before S3.
    assert deleted.index("vol-burn0") < deleted.index("kinoforge-quota-burn-aws-xyz")
    # S3 before DynamoDB.
    assert deleted.index("kinoforge-quota-burn-aws-xyz") < deleted.index(
        "kinoforge-quota-burn-xyz"
    )
    # DynamoDB before Budget.
    assert deleted.index("kinoforge-quota-burn-xyz") < deleted.index(
        "kinoforge-quota-burn-20260610-xyz"
    )
    # S3 objects emptied before bucket deleted.
    assert clients.s3.delete_object_calls == [
        ("kinoforge-quota-burn-aws-xyz", "a"),
        ("kinoforge-quota-burn-aws-xyz", "b"),
    ]
    # Waiter was called with the correct instance IDs.
    assert clients.ec2_waiter.waited_for == [["i-burn0"]]


def test_aws_tear_down_is_idempotent_on_all_not_found_codes() -> None:
    """Bug catch: each AWS service uses a different NotFound error code;
    missing any of the five leaves teardown half-finished on a re-run.
    Codes covered: InvalidInstanceID.NotFound, InvalidVolume.NotFound,
    NoSuchBucket, ResourceNotFoundException, NotFoundException."""
    clients = FakeAwsTeardownClients(
        ec2=FakeEC2ForTeardown(
            not_found_instances={"i-burn0"}, not_found_volumes={"vol-burn0"}
        ),
        ec2_waiter=FakeWaiter(),
        s3=FakeS3ForTeardown(not_found={"kinoforge-quota-burn-aws-xyz"}),
        dynamo=FakeDynamoForTeardown(not_found={"kinoforge-quota-burn-xyz"}),
        budgets=FakeBudgetsForTeardown(not_found={"kinoforge-quota-burn-20260610-xyz"}),
    )
    deleted = aws_tear_down(clients, _aws_teardown_manifest())
    assert deleted == []


def test_aws_tear_down_raises_on_unexpected_clienterror() -> None:
    """Bug catch: swallowing AccessDenied would mask expired-creds and falsely
    report teardown success while resources keep running."""

    class _EvilEC2(FakeEC2ForTeardown):
        def terminate_instances(self, **kwargs: Any) -> dict:  # type: ignore[type-arg]
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
    """Bug catch: wrong aggregation logic (e.g. overwriting instead of adding)
    would silently drop costs for services appearing in multiple result windows."""

    class _CE:
        def get_cost_and_usage(self, **_: Any) -> dict:  # type: ignore[type-arg]
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
