"""Reaper recognizes status='degraded' alongside heartbeat-stale criteria.

Adapts the plan's behavior contract (a degraded pod is reap-eligible
regardless of heartbeat freshness) to the project's actual reaper
surface: ``classify(entry, live_pod_ids, now, *, ...)`` returning a
``Verdict`` enum, and a ``DEFAULT_APPLY_POLICY`` set of verdicts the
sweeper acts on by default.
"""

from __future__ import annotations

import time
from typing import Any

from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict, classify


def _entry(
    *,
    id: str,
    status: str | None = None,
    last_heartbeat: float | None = None,
    created_at: float | None = None,
) -> dict[str, Any]:
    now = time.time()
    e: dict[str, Any] = {
        "id": id,
        "provider": "runpod",
        "tags": {},
        "created_at": created_at if created_at is not None else now,
        "cost_rate_usd_per_hr": 1.0,
    }
    if last_heartbeat is not None:
        e["last_heartbeat"] = last_heartbeat
    if status is not None:
        e["status"] = status
    return e


def _classify_default(entry: dict[str, Any], *, now: float) -> Verdict:
    return classify(
        entry,
        live_pod_ids={str(entry["id"])},
        now=now,
        idle_timeout_s=600.0,
        max_lifetime_s=3_600.0,
        heartbeat_interval_s=10.0,
        grace_after_session_s=60.0,
    )


def test_degraded_pod_is_reap_eligible_even_with_fresh_heartbeat() -> None:
    """Bug: reaper only consults heartbeat, ignoring the new status field,
    so swap-degraded pods stay alive indefinitely until heartbeat goes
    stale."""
    now = time.time()
    entry = _entry(
        id="pod-degraded", status="degraded", last_heartbeat=now, created_at=now
    )
    verdict = _classify_default(entry, now=now)
    assert verdict == Verdict.DEGRADED_REAP
    assert verdict in DEFAULT_APPLY_POLICY.act_verdicts


def test_alive_pod_with_fresh_heartbeat_not_reap_eligible() -> None:
    """Bug: classifier accidentally fires DEGRADED_REAP for any non-degraded
    status string, sweeping healthy pods."""
    now = time.time()
    entry = _entry(id="pod-alive", status="alive", last_heartbeat=now, created_at=now)
    verdict = _classify_default(entry, now=now)
    assert verdict not in DEFAULT_APPLY_POLICY.act_verdicts


def test_status_absent_falls_back_to_existing_verdicts() -> None:
    """Pre-feature entries (no status field) follow existing rules.

    Bug: missing-status branch defaults to DEGRADED_REAP, retroactively
    reaping every pre-feature pod the moment the new reaper deploys.
    """
    now = time.time()
    fresh = _entry(id="pod-fresh", last_heartbeat=now, created_at=now)
    verdict_fresh = _classify_default(fresh, now=now)
    assert verdict_fresh != Verdict.DEGRADED_REAP


def test_degraded_short_circuits_overage_path() -> None:
    """Bug: classifier checks max-age before status, so a degraded pod that
    has also exceeded max_lifetime is reported as OVERAGE_REAP, losing
    the actionable 'degraded' reason."""
    now = time.time()
    entry = _entry(
        id="pod-old-degraded",
        status="degraded",
        last_heartbeat=now,
        created_at=now - 10_000,
    )
    verdict = _classify_default(entry, now=now)
    assert verdict == Verdict.DEGRADED_REAP
