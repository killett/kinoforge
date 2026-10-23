"""smoke_leak_sweep — Layer-3 watchdog.

Every instance here comes out of the REAL ``_pod_to_instance`` on a pod dict
shaped like RunPod's ``myself.pods`` rows. The previous fakes carried a
``created_at`` and a ``smoke_tier`` tag the provider never produced, which is
how Finding #0 (docs/hygiene-audit-2026-10-18.md) — every pod aged as
~497,000 h and reaped — stayed green for four months.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import UTC, datetime
from typing import Any

import pytest

import tools.smoke_leak_sweep as sweep
from kinoforge.core.interfaces import Instance
from kinoforge.providers.runpod import _pod_to_instance

_RATE = 1.79


def _pod_row(pod_id: str, *, started_h_ago: float | None) -> dict[str, Any]:
    """A ``myself.pods`` row as RunPod returns it.

    ``started_h_ago=None`` omits ``lastStartedAt`` — the shape the recorded
    fixture ``tests/providers/fixtures/runpod/list_pods.json`` has, and what a
    narrower selection set or a partial response yields.
    """
    row: dict[str, Any] = {
        "id": pod_id,
        "name": f"kf-{pod_id}",
        "desiredStatus": "RUNNING",
        "imageName": "img:1",
        "costPerHr": _RATE,
    }
    if started_h_ago is not None:
        started = datetime.fromtimestamp(time.time() - started_h_ago * 3600.0, tz=UTC)
        row["lastStartedAt"] = started.strftime("%Y-%m-%dT%H:%M:%S.") + (
            f"{started.microsecond // 1000:03d}Z"
        )
    return row


def _instance(
    pod_id: str, *, started_h_ago: float | None, tier: str | None = None
) -> Instance:
    """Run a realistic row through the real converter; optionally tag a tier.

    The tier tag is applied AFTER conversion because RunPod's listing carries
    no tags — ``smoke_tier`` is the tool's contract on ``Instance.tags``, which
    nothing populates on RunPod today. Tests that set it exercise the ceiling
    table, not a path a live listing can reach.
    """
    inst = _pod_to_instance(_pod_row(pod_id, started_h_ago=started_h_ago))
    if tier is None:
        return inst
    return dataclasses.replace(inst, tags={**inst.tags, "smoke_tier": tier})


class _Provider:
    def __init__(self, instances: list[Instance]) -> None:
        self._instances = instances
        self.destroyed: list[str] = []

    def list_instances(self) -> list[Instance]:
        return list(self._instances)

    def destroy_instance(self, pod_id: str) -> None:
        self.destroyed.append(pod_id)


def _install(
    monkeypatch: pytest.MonkeyPatch, instances: list[Instance]
) -> tuple[_Provider, list[dict[str, object]]]:
    provider = _Provider(instances)
    issues: list[dict[str, object]] = []
    monkeypatch.setattr(sweep, "_get_runpod_provider", lambda: provider)
    monkeypatch.setattr(sweep, "_post_issue", lambda **kw: issues.append(kw))
    return provider, issues


def test_unknown_age_pod_is_never_reaped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pod whose row has no ``lastStartedAt`` is kept, and said so at WARNING.

    This is Finding #0: ``_pod_to_instance`` yields ``created_at == 0.0`` for
    such a row, and ``now - 0.0`` is ~497,000 h — past every ceiling — so the
    tool destroyed every pod in the account. An unknown age must never reap.
    """
    provider, issues = _install(monkeypatch, [_instance("boot", started_h_ago=None)])

    with caplog.at_level(logging.WARNING, logger="smoke_leak_sweep"):
        rc = sweep.main([])

    assert rc == 0
    assert provider.destroyed == []
    assert issues == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("boot" in m and "unknown" in m.lower() for m in warnings), warnings


def test_recent_untagged_pod_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Six minutes old, no tier tag: inside the 4 h untagged ceiling."""
    provider, issues = _install(monkeypatch, [_instance("fresh", started_h_ago=0.1)])

    assert sweep.main([]) == 0
    assert provider.destroyed == []
    assert issues == []


def test_pod_past_the_untagged_ceiling_is_reaped_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five hours old, no tier tag: past the 4 h ceiling → destroyed + issue.

    Together with the test above this pins the age SOURCE: if ``created_at``
    stayed at the sentinel the unknown-age guard would keep this pod too, and
    if the stamp were read in the wrong unit or timezone the reported age
    would not be five hours.
    """
    provider, issues = _install(monkeypatch, [_instance("old", started_h_ago=5.0)])

    assert sweep.main([]) == 0
    assert provider.destroyed == ["old"]
    assert len(issues) == 1
    assert issues[0]["pod_id"] == "old"
    assert issues[0]["tag"] is None
    assert issues[0]["age_h"] == pytest.approx(5.0, abs=0.01)
    assert issues[0]["spend"] == pytest.approx(5.0 * _RATE, abs=0.02)


@pytest.mark.parametrize(
    ("age_h", "expected_destroyed"),
    [(4.0, []), (4.0 + 1 / 3600.0, ["edge"])],
    ids=["at-ceiling-kept", "one-second-over-reaped"],
)
def test_untagged_ceiling_is_exclusive(
    monkeypatch: pytest.MonkeyPatch, age_h: float, expected_destroyed: list[str]
) -> None:
    """ "Exceeds the ceiling" means strictly greater; exactly 4 h is kept.

    The clock is pinned so the computed age is exactly the parametrised value
    rather than that plus however long the test took to reach the comparison.
    """
    now = time.time()
    inst = _instance("edge", started_h_ago=age_h)
    inst = dataclasses.replace(inst, created_at=now - age_h * 3600.0)
    provider, _issues = _install(monkeypatch, [inst])
    monkeypatch.setattr(time, "time", lambda: now)

    assert sweep.main([]) == 0
    assert provider.destroyed == expected_destroyed


def test_tier3_ceiling_applies_when_the_tag_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tier-3 tagged pod at 1.5 h is past its 45 min ceiling.

    The tag cannot arrive from a RunPod listing today (see ``_instance``); this
    pins the ceiling table for whichever provider or naming scheme does
    populate it, so a dropped or mis-keyed row is caught here, not live.
    """
    provider, issues = _install(
        monkeypatch,
        [_instance("t3", started_h_ago=1.5, tier="kinoforge-smoke-tier-3")],
    )

    assert sweep.main([]) == 0
    assert provider.destroyed == ["t3"]
    assert issues and issues[0]["tag"] == "kinoforge-smoke-tier-3"


def test_dry_run_reports_but_destroys_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, issues = _install(
        monkeypatch, [_instance("would-reap", started_h_ago=5.0)]
    )

    assert sweep.main(["--dry-run"]) == 0
    assert provider.destroyed == []
    assert issues == []


def test_destroy_failure_posts_no_issue_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A destroy that raises must not file an issue claiming a reap happened.

    The issue is the audit record; one for a pod that is still billing would
    send the operator looking for a leak that was never closed.
    """
    provider, issues = _install(
        monkeypatch,
        [_instance("stuck", started_h_ago=5.0), _instance("old", started_h_ago=5.0)],
    )

    def _destroy(pod_id: str) -> None:
        if pod_id == "stuck":
            raise RuntimeError("transient RunPod 502")
        provider.destroyed.append(pod_id)

    monkeypatch.setattr(provider, "destroy_instance", _destroy)

    assert sweep.main([]) == 0
    assert provider.destroyed == ["old"]
    assert [i["pod_id"] for i in issues] == ["old"]


def test_list_failure_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug: tool swallows list_instances failure and exits 0 silently."""

    def _boom() -> object:
        raise RuntimeError("RunPod GraphQL unreachable")

    monkeypatch.setattr(sweep, "_get_runpod_provider", _boom)
    assert sweep.main([]) == 1
