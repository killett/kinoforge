"""Tests for the B2 pure cost aggregator."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from kinoforge.core.balance_endpoints import ProviderBalance
from kinoforge.core.cost import (
    _BURNING_VERDICTS,
    CostSnapshot,
    ProviderBreakdown,
    aggregate,
)
from kinoforge.core.reaper import Verdict

_NOW = datetime(2026, 6, 12, 14, 0, 0)


def _entry(
    *,
    id: str,
    provider: str = "runpod",
    rate: float = 0.79,
    created_at_offset_hours: float = 1.0,
) -> dict[str, Any]:
    """Build a ledger-shaped entry. created_at is _NOW minus the offset."""
    created_at = (_NOW - timedelta(hours=created_at_offset_hours)).timestamp()
    return {
        "id": id,
        "provider": provider,
        "cost_rate_usd_per_hr": rate,
        "created_at": created_at,
    }


def test_burning_verdicts_constant_excludes_stale_and_unroutable() -> None:
    """BUG CATCH: STALE_LEDGER MUST NOT contribute to burn — that verdict
    means the pod is gone from the provider per Layer V Row 1."""
    assert Verdict.LIVE in _BURNING_VERDICTS
    assert Verdict.IDLE_REAP in _BURNING_VERDICTS
    assert Verdict.OVERAGE_REAP in _BURNING_VERDICTS
    assert Verdict.ORPHAN_REAP in _BURNING_VERDICTS
    assert Verdict.HEARTBEAT_UNKNOWN in _BURNING_VERDICTS
    assert Verdict.HEARTBEAT_SUBSTRATE_MISSING in _BURNING_VERDICTS
    assert Verdict.STALE_LEDGER not in _BURNING_VERDICTS
    assert Verdict.UNROUTABLE not in _BURNING_VERDICTS


def test_empty_ledger_yields_zero_snapshot() -> None:
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert snap.burn_rate_usd_per_hr == 0.0
    assert snap.per_provider == ()
    assert snap.as_of == _NOW


def test_single_live_entry_burn_and_spend() -> None:
    entries = [_entry(id="a", rate=0.79, created_at_offset_hours=1.0)]
    snap = aggregate(
        entries=entries,
        verdicts_by_id={"a": Verdict.LIVE},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert snap.burn_rate_usd_per_hr == 0.79
    assert len(snap.per_provider) == 1
    p = snap.per_provider[0]
    assert p.provider == "runpod"
    assert p.burn_rate_usd_per_hr == 0.79
    assert p.spend_usd_total == pytest.approx(0.79, abs=1e-9)


def test_stale_ledger_excluded_from_burn() -> None:
    """BUG CATCH: STALE_LEDGER counts ARE incremented, but burn excludes them."""
    entries = [
        _entry(id="live", rate=0.50),
        _entry(id="stale", rate=99.0),
    ]
    snap = aggregate(
        entries=entries,
        verdicts_by_id={"live": Verdict.LIVE, "stale": Verdict.STALE_LEDGER},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert snap.burn_rate_usd_per_hr == 0.50
    p = snap.per_provider[0]
    assert p.pod_counts_by_verdict[Verdict.LIVE] == 1
    assert p.pod_counts_by_verdict[Verdict.STALE_LEDGER] == 1


def test_per_provider_sorted_alphabetically() -> None:
    entries = [
        _entry(id="r1", provider="runpod", rate=0.50),
        _entry(id="s1", provider="skypilot", rate=1.20),
        _entry(id="r2", provider="runpod", rate=0.30),
    ]
    verdicts = {"r1": Verdict.LIVE, "s1": Verdict.LIVE, "r2": Verdict.IDLE_REAP}
    snap = aggregate(
        entries=entries,
        verdicts_by_id=verdicts,
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert [p.provider for p in snap.per_provider] == ["runpod", "skypilot"]
    runpod = snap.per_provider[0]
    assert runpod.burn_rate_usd_per_hr == pytest.approx(0.80, abs=1e-9)
    assert runpod.pod_counts_by_verdict[Verdict.LIVE] == 1
    assert runpod.pod_counts_by_verdict[Verdict.IDLE_REAP] == 1


def test_bad_entry_silently_skipped() -> None:
    """BUG CATCH: a malformed entry MUST NOT poison the whole snapshot.
    Mirrors sweep() bad-entry isolation."""
    entries: list[dict[str, Any]] = [
        _entry(id="ok", rate=0.50),
        {"provider": "runpod"},  # no id
        {"id": "bad-rate", "provider": "runpod", "cost_rate_usd_per_hr": "NaN-string"},
    ]
    verdicts = {"ok": Verdict.LIVE, "bad-rate": Verdict.LIVE}
    snap = aggregate(
        entries=entries,
        verdicts_by_id=verdicts,
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert snap.burn_rate_usd_per_hr == 0.50


def test_all_eight_verdict_keys_present_in_counts() -> None:
    """BUG CATCH: counts dict MUST carry every Verdict key (zeros included)
    so --json / --prom emit a stable shape."""
    entries = [_entry(id="a", rate=0.10)]
    verdicts = {"a": Verdict.LIVE}
    snap = aggregate(
        entries=entries,
        verdicts_by_id=verdicts,
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    p = snap.per_provider[0]
    for v in Verdict:
        assert v in p.pod_counts_by_verdict


def test_balances_and_errors_pass_through() -> None:
    """Aggregator does NOT do I/O; balances and errors are CLI-supplied passthrough."""
    pb = ProviderBalance(usd=42.18, as_of=_NOW, source="runpod-graphql-clientBalance")
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={"runpod": pb},
        balance_errors={"skypilot": "no satisfier"},
        heartbeat_partial_truth=("skypilot",),
    )
    assert snap.balances["runpod"] is pb
    assert snap.balance_errors["skypilot"] == "no satisfier"
    assert snap.heartbeat_partial_truth == ("skypilot",)


def test_snapshot_is_frozen() -> None:
    import dataclasses

    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.burn_rate_usd_per_hr = 99.0  # type: ignore[misc]


def test_provider_breakdown_is_frozen() -> None:
    import dataclasses

    entries = [_entry(id="a", rate=0.10)]
    snap = aggregate(
        entries=entries,
        verdicts_by_id={"a": Verdict.LIVE},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.per_provider[0].burn_rate_usd_per_hr = 99.0  # type: ignore[misc]


def test_hosted_spend_pending_default_true() -> None:
    """Until B10 ships, hosted-engine spend is NOT in the totals; flag stays True."""
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert snap.hosted_spend_pending is True


def test_throttle_warnings_passthrough() -> None:
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
        throttle_warnings=("replicate approaching $5 throttle",),
    )
    assert snap.throttle_warnings == ("replicate approaching $5 throttle",)


def test_snapshot_type_is_costsnapshot() -> None:
    """Smoke: return shape is the right class so docstrings stay honest."""
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert isinstance(snap, CostSnapshot)


def test_provider_breakdown_is_breakdown() -> None:
    entries = [_entry(id="a")]
    snap = aggregate(
        entries=entries,
        verdicts_by_id={"a": Verdict.LIVE},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert isinstance(snap.per_provider[0], ProviderBreakdown)
