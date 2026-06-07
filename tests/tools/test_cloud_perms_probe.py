"""Unit tests for cloud_perms_probe — Layer W+α T3 (AWS)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools import cloud_perms_probe as probe


class _FakeBoto3Session:
    """boto3.Session test double — client(name) returns fakes from a map."""

    def __init__(self, clients: dict[str, Any]) -> None:
        self._clients = clients

    def client(self, name: str, **_kwargs: Any) -> Any:
        if name not in self._clients:
            raise KeyError(f"no fake registered for boto3 client {name!r}")
        return self._clients[name]


class _FakeSTSClient:
    def __init__(
        self,
        *,
        identity: dict[str, str] | None = None,
        raise_on_call: BaseException | None = None,
    ) -> None:
        self._identity = identity
        self._raise = raise_on_call

    def get_caller_identity(self) -> dict[str, str]:
        if self._raise is not None:
            raise self._raise
        assert self._identity is not None
        return self._identity


class _FakeIAMClient:
    def __init__(self, results: dict[str, str]) -> None:
        self._results = results

    def simulate_principal_policy(
        self,
        *,
        PolicySourceArn: str,
        ActionNames: list[str],
        ResourceArns: list[str] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "EvaluationResults": [
                {
                    "EvalActionName": a,
                    "EvalDecision": self._results.get(a, "implicitDeny"),
                }
                for a in ActionNames
            ],
        }


class _FakeEC2Client:
    def __init__(self, types: list[dict[str, Any]] | None = None) -> None:
        self._types = types or [
            {"InstanceType": "g4dn.xlarge", "GpuInfo": {"Gpus": [{"Name": "T4"}]}},
        ]

    def describe_instance_types(
        self,
        *,
        InstanceTypes: list[str],
        **_k: Any,
    ) -> dict[str, Any]:
        wanted = set(InstanceTypes)
        return {
            "InstanceTypes": [t for t in self._types if t["InstanceType"] in wanted]
        }


class _FakeServiceQuotasClient:
    def __init__(
        self,
        *,
        value: float,
        name: str = "Running On-Demand G and VT instances",
    ) -> None:
        self._value = value
        self._name = name

    def get_service_quota(
        self,
        *,
        ServiceCode: str,
        QuotaCode: str,
    ) -> dict[str, Any]:
        return {
            "Quota": {
                "QuotaCode": QuotaCode,
                "QuotaName": self._name,
                "Value": self._value,
            }
        }


def _green_aws_session(*, quota_value: float = 8.0) -> _FakeBoto3Session:
    return _FakeBoto3Session(
        {
            "sts": _FakeSTSClient(
                identity={
                    "UserId": "AIDAEXAMPLE",
                    "Account": "<AWS_ACCOUNT>",
                    "Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci",
                }
            ),
            "iam": _FakeIAMClient({a: "allowed" for a in probe._REQUIRED_AWS_ACTIONS}),
            "ec2": _FakeEC2Client(),
            "service-quotas": _FakeServiceQuotasClient(value=quota_value),
        }
    )


def test_probe_aws_exit_1_on_auth_failure(tmp_path: Path) -> None:
    """sts.get_caller_identity raising → exit 1, no snapshot written."""
    from botocore.exceptions import ClientError

    err = ClientError(
        {"Error": {"Code": "InvalidClientTokenId", "Message": "bad key"}},
        "GetCallerIdentity",
    )
    session = _FakeBoto3Session(
        {
            "sts": _FakeSTSClient(raise_on_call=err),
        }
    )
    snapshot_path = tmp_path / "aws_snapshot.json"
    result = probe.probe_aws(session, snapshot_path=snapshot_path)

    assert result["exit_code"] == 1
    assert "auth_error" in result
    assert not snapshot_path.exists()


def test_probe_aws_exit_0_on_all_green(tmp_path: Path) -> None:
    """Green path: identity + simulate allowed + quota >= target → exit 0, snapshot written."""
    snapshot_path = tmp_path / "aws_snapshot.json"
    result = probe.probe_aws(_green_aws_session(), snapshot_path=snapshot_path)

    assert result["exit_code"] == 0
    assert result["identity"]["Arn"].endswith(":user/kinoforge-ci")
    assert result["simulated"][probe._REQUIRED_AWS_ACTIONS[0]] == "allowed"
    assert "g4dn.xlarge" in result["instance_type"]
    assert result["quotas"]["L-DB2E81BA"]["value"] == 8.0
    on_disk = json.loads(snapshot_path.read_text())
    assert on_disk == result


def test_probe_aws_exit_2_on_quota_gap(tmp_path: Path) -> None:
    """Quota below target → exit 2 + quota_gap dict captured."""
    snapshot_path = tmp_path / "aws_snapshot.json"
    result = probe.probe_aws(
        _green_aws_session(quota_value=0.0),
        snapshot_path=snapshot_path,
    )
    assert result["exit_code"] == 2
    assert result["quota_gap"] == {"code": "L-DB2E81BA", "have": 0.0, "want": 4.0}


def test_probe_aws_exit_1_on_action_denied(tmp_path: Path) -> None:
    """Simulate returns implicitDeny on required actions → exit 1, denied list."""
    session = _FakeBoto3Session(
        {
            "sts": _FakeSTSClient(
                identity={"Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci"}
            ),
            "iam": _FakeIAMClient(
                {a: "implicitDeny" for a in probe._REQUIRED_AWS_ACTIONS}
            ),
            "ec2": _FakeEC2Client(),
            "service-quotas": _FakeServiceQuotasClient(value=8.0),
        }
    )
    snapshot_path = tmp_path / "aws_snapshot.json"
    result = probe.probe_aws(session, snapshot_path=snapshot_path)

    assert result["exit_code"] == 1
    assert "denied" in result
    assert result["denied"]


class _FakeGCPRegionsClient:
    def __init__(self, *, quotas: list[dict[str, Any]]) -> None:
        self._quotas = quotas

    def get(self, *, project: str, region: str) -> Any:
        class _Quota:
            def __init__(self, d: dict[str, Any]) -> None:
                self.metric = d["metric"]
                self.limit = d["limit"]
                self.usage = d["usage"]

        class _Region:
            def __init__(
                self, project: str, region: str, quotas: list[dict[str, Any]]
            ) -> None:
                self.name = f"projects/{project}/regions/{region}"
                self.quotas = [_Quota(q) for q in quotas]

        return _Region(project, region, self._quotas)


class _FakeGCPIAMClient:
    def __init__(self, *, sa_roles: dict[str, list[str]]) -> None:
        self._roles = sa_roles

    def get_iam_policy(self, *, resource: str, **_k: Any) -> Any:
        class _Binding:
            def __init__(self, role: str, members: list[str]) -> None:
                self.role = role
                self.members = members

        class _Policy:
            def __init__(self, bindings: list[_Binding]) -> None:
                self.bindings = bindings

        return _Policy(
            [_Binding(role, members) for role, members in self._roles.items()]
        )


def _green_gcp_clients(*, t4_quota: float = 8.0) -> dict[str, Any]:
    sa_member = (
        "serviceAccount:kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com"
    )
    return {
        "regions": _FakeGCPRegionsClient(
            quotas=[
                {"metric": "NVIDIA_T4_GPUS", "limit": t4_quota, "usage": 0.0},
                {"metric": "CPUS", "limit": 24.0, "usage": 0.0},
            ]
        ),
        "iam": _FakeGCPIAMClient(
            sa_roles={
                "roles/compute.instanceAdmin.v1": [sa_member],
                "roles/iam.serviceAccountUser": [sa_member],
                "roles/storage.admin": [sa_member],
            }
        ),
    }


def test_probe_gcp_exit_0_on_all_green(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "gcp_snapshot.json"
    result = probe.probe_gcp(
        clients=_green_gcp_clients(),
        project="<GCP_PROJECT>",
        sa_email="kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com",
        snapshot_path=snapshot_path,
    )
    assert result["exit_code"] == 0
    assert result["quotas"]["NVIDIA_T4_GPUS"]["limit"] == 8.0
    assert "roles/compute.instanceAdmin.v1" in result["sa_roles"]
    assert json.loads(snapshot_path.read_text()) == result


def test_probe_gcp_exit_2_on_quota_zero(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "gcp_snapshot.json"
    result = probe.probe_gcp(
        clients=_green_gcp_clients(t4_quota=0.0),
        project="<GCP_PROJECT>",
        sa_email="kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com",
        snapshot_path=snapshot_path,
    )
    assert result["exit_code"] == 2
    assert result["quota_gap"] == {
        "metric": "NVIDIA_T4_GPUS",
        "have": 0.0,
        "want": 1.0,
        "region": "us-central1",
    }


def test_probe_gcp_exit_1_on_missing_role(tmp_path: Path) -> None:
    clients = _green_gcp_clients()
    sa_member = (
        "serviceAccount:kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com"
    )
    clients["iam"] = _FakeGCPIAMClient(
        sa_roles={
            # No instanceAdmin → required role missing.
            "roles/storage.admin": [sa_member],
        }
    )
    snapshot_path = tmp_path / "gcp_snapshot.json"
    result = probe.probe_gcp(
        clients=clients,
        project="<GCP_PROJECT>",
        sa_email="kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com",
        snapshot_path=snapshot_path,
    )
    assert result["exit_code"] == 1
    assert "roles/compute.instanceAdmin.v1" in result["missing_roles"]


def test_gcp_green_fixture_matches_probe_shape() -> None:
    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "cloud_perms" / "gcp_green.json"
    )
    fixture = json.loads(fixture_path.read_text())
    required_top_keys = {
        "captured_at",
        "cloud",
        "project",
        "region",
        "sa_email",
        "quotas",
        "sa_roles",
        "exit_code",
    }
    assert required_top_keys.issubset(fixture.keys()), (
        f"missing keys: {required_top_keys - fixture.keys()}"
    )
    assert fixture["cloud"] == "gcp"
    assert "NVIDIA_T4_GPUS" in fixture["quotas"]


def test_aws_green_fixture_matches_probe_shape() -> None:
    """Lockdown: live-captured fixture must match the dict shape probe_aws emits."""
    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "cloud_perms" / "aws_green.json"
    )
    fixture = json.loads(fixture_path.read_text())
    required_top_keys = {
        "captured_at",
        "cloud",
        "region",
        "identity",
        "simulated",
        "instance_type",
        "quotas",
        "exit_code",
    }
    assert required_top_keys.issubset(fixture.keys()), (
        f"missing keys: {required_top_keys - fixture.keys()}"
    )
    assert fixture["cloud"] == "aws"
    assert fixture["identity"]["Arn"].endswith(":user/kinoforge-ci")
    assert "L-DB2E81BA" in fixture["quotas"]
