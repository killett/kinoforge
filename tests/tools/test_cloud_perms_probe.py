"""Unit tests for cloud_perms_probe — Layer W+α T3 (AWS)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

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

    def list_requested_service_quota_change_history(
        self,
        *,
        ServiceCode: str,
        **_k: Any,
    ) -> dict[str, Any]:
        return {"RequestedQuotas": []}

    def request_service_quota_increase(
        self,
        *,
        ServiceCode: str,
        QuotaCode: str,
        DesiredValue: float,
    ) -> dict[str, Any]:
        return {"RequestedQuota": {"Id": "case-stub", "Status": "PENDING"}}


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
    sa_member = "serviceAccount:kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com"
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
    sa_member = "serviceAccount:kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com"
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


class _FakeServiceQuotasWithRequest(_FakeServiceQuotasClient):
    def __init__(self, *, value: float, existing_case: str | None = None) -> None:
        super().__init__(value=value)
        self._existing_case = existing_case
        self.requests_made: list[dict[str, Any]] = []

    def list_requested_service_quota_change_history(
        self,
        *,
        ServiceCode: str,
        **_k: Any,
    ) -> dict[str, Any]:
        items = []
        if self._existing_case is not None:
            items.append(
                {
                    "Id": self._existing_case,
                    "QuotaCode": "L-DB2E81BA",
                    "Status": "PENDING",
                }
            )
        return {"RequestedQuotas": items}

    def request_service_quota_increase(
        self,
        *,
        ServiceCode: str,
        QuotaCode: str,
        DesiredValue: float,
    ) -> dict[str, Any]:
        case_id = self._existing_case or f"case-{len(self.requests_made):04d}"
        self.requests_made.append(
            {
                "ServiceCode": ServiceCode,
                "QuotaCode": QuotaCode,
                "DesiredValue": DesiredValue,
            }
        )
        return {"RequestedQuota": {"Id": case_id, "Status": "PENDING"}}


def test_probe_aws_does_not_open_a_support_case_without_an_opt_in(
    tmp_path: Path,
) -> None:
    """A quota gap must not silently open a real AWS support case.

    `RequestServiceQuotaIncrease` files a case against the caller's
    account. `.aws/README.md` used to route operators to this probe from a
    line that reads as a read-only "confirm the scope is sufficient"
    check, so the default had to become report-only. A bug that would fail
    this: submitting whenever the gap is seen (what shipped first), or
    defaulting `submit_quota_request` to True -- either way an operator
    running a documented verification step files a support case they never
    asked for.
    """
    fake_sq = _FakeServiceQuotasWithRequest(value=0.0)
    session = _FakeBoto3Session(
        {
            "sts": _FakeSTSClient(
                identity={
                    "UserId": "AIDA",
                    "Account": "<AWS_ACCOUNT>",
                    "Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci",
                }
            ),
            "iam": _FakeIAMClient({a: "allowed" for a in probe._REQUIRED_AWS_ACTIONS}),
            "ec2": _FakeEC2Client(),
            "service-quotas": fake_sq,
        }
    )
    result = probe.probe_aws(session, snapshot_path=tmp_path / "aws.json")

    assert result["exit_code"] == 2
    assert fake_sq.requests_made == []
    assert "quota_request" not in result
    assert "--submit-quota-increase" in result["quota_request_skipped"]["how_to_submit"]


def test_probe_aws_submits_quota_request_on_gap(tmp_path: Path) -> None:
    fake_sq = _FakeServiceQuotasWithRequest(value=0.0)
    session = _FakeBoto3Session(
        {
            "sts": _FakeSTSClient(
                identity={
                    "UserId": "AIDA",
                    "Account": "<AWS_ACCOUNT>",
                    "Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci",
                }
            ),
            "iam": _FakeIAMClient({a: "allowed" for a in probe._REQUIRED_AWS_ACTIONS}),
            "ec2": _FakeEC2Client(),
            "service-quotas": fake_sq,
        }
    )
    snapshot_path = tmp_path / "aws.json"
    result = probe.probe_aws(
        session, snapshot_path=snapshot_path, submit_quota_request=True
    )

    assert result["exit_code"] == 2
    assert result["quota_request"]["case_id"].startswith("case-")
    assert len(fake_sq.requests_made) == 1
    assert fake_sq.requests_made[0]["DesiredValue"] == 4.0


def test_probe_aws_idempotent_quota_request(tmp_path: Path) -> None:
    """Re-run with an existing open case → no duplicate submission."""
    fake_sq = _FakeServiceQuotasWithRequest(value=0.0, existing_case="case-EXISTING")
    session = _FakeBoto3Session(
        {
            "sts": _FakeSTSClient(
                identity={
                    "UserId": "AIDA",
                    "Account": "<AWS_ACCOUNT>",
                    "Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci",
                }
            ),
            "iam": _FakeIAMClient({a: "allowed" for a in probe._REQUIRED_AWS_ACTIONS}),
            "ec2": _FakeEC2Client(),
            "service-quotas": fake_sq,
        }
    )
    snapshot_path = tmp_path / "aws.json"
    result = probe.probe_aws(
        session, snapshot_path=snapshot_path, submit_quota_request=True
    )

    assert result["exit_code"] == 2
    assert result["quota_request"]["case_id"] == "case-EXISTING"
    assert len(fake_sq.requests_made) == 0, "must not duplicate existing open case"


def test_probe_gcp_quota_gap_emits_console_url(tmp_path: Path) -> None:
    result = probe.probe_gcp(
        clients=_green_gcp_clients(t4_quota=0.0),
        project="<GCP_PROJECT>",
        sa_email="kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com",
        snapshot_path=tmp_path / "gcp.json",
    )
    assert result["exit_code"] == 2
    url = result["quota_request"]["console_url"]
    assert "<GCP_PROJECT>" in url
    assert "NVIDIA_T4_GPUS" in url


class _RefusingBoto3Module:
    """Stand-in for the `boto3` module installed into `sys.modules`.

    `main()`'s only sanctioned path to a real AWS session is
    `_real_aws_session_factory()`, which this test monkeypatches out. This
    fake is a belt-and-suspenders backstop, not the primary seam: if a
    future refactor ever made `main()` (or something it calls) reach
    `import boto3` / `boto3.client(...)` directly -- bypassing the
    monkeypatched factory -- this raises immediately with a loud
    AssertionError instead of constructing a real client under this
    container's live ambient AWS credentials. Mirrors
    `tests/tools/test_validate_scoped_policy.py::_RefusingBoto3Module`.
    """

    @staticmethod
    def client(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError(
            "boto3.client() was called directly -- main() should only reach "
            "AWS through the monkeypatched _real_aws_session_factory seam"
        )

    @staticmethod
    def Session(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401,N802
        raise AssertionError(
            "boto3.Session() was called directly -- main() should only reach "
            "AWS through the monkeypatched _real_aws_session_factory seam"
        )


def _fake_quota_gap_session_factory(fake_sq: _FakeServiceQuotasWithRequest) -> Any:
    """Build a `_real_aws_session_factory`-shaped callable around a quota-gap session.

    The session is fully green up through the quota check, then reports a
    quota below target so `probe_aws` reaches the
    `submit_quota_request` branch -- the only branch where
    `args.submit_quota_increase` has any observable effect.
    """

    def _factory() -> _FakeBoto3Session:
        return _FakeBoto3Session(
            {
                "sts": _FakeSTSClient(
                    identity={
                        "UserId": "AIDA",
                        "Account": "<AWS_ACCOUNT>",
                        "Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci",
                    }
                ),
                "iam": _FakeIAMClient(
                    {a: "allowed" for a in probe._REQUIRED_AWS_ACTIONS}
                ),
                "ec2": _FakeEC2Client(),
                "service-quotas": fake_sq,
            }
        )

    return _factory


def test_main_does_not_submit_quota_increase_when_flag_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pixi run cloud:perms-probe` (no flags) must not file an AWS support case.

    Pins the CLI wiring at `tools/cloud_perms_probe.py:518`. A bug that
    would fail this: `probe_aws(session,
    submit_quota_request=args.submit_quota_increase)` regressing to
    `submit_quota_request=True` (or any other way of losing the argv
    plumbing) -- library-level test coverage
    (`test_probe_aws_does_not_open_a_support_case_without_an_opt_in`) pins
    only `probe_aws`'s own default and cannot see this: `main()` always
    passes an explicit keyword, so the library default is never even
    consulted on the CLI path. `_RefusingBoto3Module` guarantees a
    regression fails at a loud AssertionError-free real-AWS boundary
    rather than actually filing a case, since ambient credentials are live
    in this container.
    """
    monkeypatch.setitem(sys.modules, "boto3", _RefusingBoto3Module())
    fake_sq = _FakeServiceQuotasWithRequest(value=0.0)
    monkeypatch.setattr(
        probe, "_real_aws_session_factory", _fake_quota_gap_session_factory(fake_sq)
    )
    monkeypatch.setattr(probe, "_AWS_SNAPSHOT_PATH", tmp_path / "aws.json")

    exit_code = probe.main(["--cloud", "aws"])

    assert exit_code == 2
    assert fake_sq.requests_made == []


def test_main_submits_quota_increase_when_flag_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--submit-quota-increase` on the CLI must actually reach `probe_aws`.

    Counterpart to `test_main_does_not_submit_quota_increase_when_flag_is_absent`
    -- pins the other direction of the same wiring at
    `tools/cloud_perms_probe.py:518` so a future refactor cannot satisfy
    the absent-flag test by hardcoding `submit_quota_request=False`
    instead of actually reading `args.submit_quota_increase`.
    `_RefusingBoto3Module` again keeps a regression that bypasses the
    monkeypatched factory from ever reaching a real AWS client.
    """
    monkeypatch.setitem(sys.modules, "boto3", _RefusingBoto3Module())
    fake_sq = _FakeServiceQuotasWithRequest(value=0.0)
    monkeypatch.setattr(
        probe, "_real_aws_session_factory", _fake_quota_gap_session_factory(fake_sq)
    )
    monkeypatch.setattr(probe, "_AWS_SNAPSHOT_PATH", tmp_path / "aws.json")

    exit_code = probe.main(["--cloud", "aws", "--submit-quota-increase"])

    assert exit_code == 2
    assert len(fake_sq.requests_made) == 1
