# tests/tools/test_quota_burn_aws.py
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

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
