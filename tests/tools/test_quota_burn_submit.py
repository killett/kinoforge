from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tools.quota_burn_lib import (
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
    for call in client.calls:
        assert call["reason"] == "reason text"


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
    pair = FakeAwsClientPair(quotas=FakeAwsServiceQuotas(), support=FakeAwsSupport())
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


def test_aws_submit_quota_handles_missing_case_id_gracefully() -> None:
    """Bug catch: AWS sometimes doesn't open a Support case for a quota
    increase (or omits the CaseId field). Unconditional ["CaseId"] access
    would raise KeyError AFTER the quota was already submitted — caller
    sees a crash instead of submitted=True. Skip the support call cleanly
    when CaseId is absent."""

    @dataclass
    class _NoCaseQuotas:
        calls: list[dict[str, Any]] = field(default_factory=list)

        def request_service_quota_increase(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"RequestedQuota": {"Id": "req-789"}}  # no CaseId

    pair = FakeAwsClientPair(quotas=_NoCaseQuotas(), support=FakeAwsSupport())  # type: ignore[arg-type]
    result = aws_submit_quota(
        pair,
        region="us-west-2",
        quota_code="L-DB2E81BA",
        desired_value=4,
        justification_text="reason",
    )
    assert result.submitted is True
    assert result.request_ids == ["req-789"]
    # No support call should fire when CaseId is missing.
    assert pair.support.case_comments == []


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
