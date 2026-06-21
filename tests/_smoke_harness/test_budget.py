"""BudgetTracker post-condition assertion."""

from __future__ import annotations

import time

import pytest

from tests._smoke_harness import budget


def test_under_cap_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug: tracker raises on every call regardless of cap."""
    monkeypatch.setattr(budget, "_get_cost_rate", lambda _pid: 1.0)
    tracker = budget.BudgetTracker(cap_usd=10.0, pod_id="x")
    tracker._start_ts = time.time() - 60
    tracker.assert_under_cap()  # 1.0 * (1/60) = $0.017 < $10


def test_over_cap_raises_assertion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: tracker uses wrong arithmetic and never trips."""
    monkeypatch.setattr(budget, "_get_cost_rate", lambda _pid: 100.0)
    tracker = budget.BudgetTracker(cap_usd=0.50, pod_id="x")
    tracker._start_ts = time.time() - 60  # 1m @ $100/hr = $1.67
    with pytest.raises(AssertionError, match="cap"):
        tracker.assert_under_cap()


def test_assert_under_cap_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: tracker mutates state per call → flaky."""
    monkeypatch.setattr(budget, "_get_cost_rate", lambda _pid: 1.0)
    tracker = budget.BudgetTracker(cap_usd=10.0, pod_id="x")
    tracker._start_ts = time.time() - 60
    tracker.assert_under_cap()
    tracker.assert_under_cap()
