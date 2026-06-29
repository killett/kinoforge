"""Tests for `_refuse_reason_for_verdict` honesty.

Regression guard: prior message reported `sentinel_age=Xs past
grace_after_session_s=Ys` for ORPHAN_REAP, but classify decides ORPHAN_REAP via
`time_since_session_end > grace` (with `created_at` fallback when `session_end`
absent), never against the sentinel. The string was mathematically nonsensical
(X < Y) and misled an operator on 2026-06-28.
"""

from __future__ import annotations

from kinoforge.cli._commands import _refuse_reason_for_verdict
from kinoforge.core.interfaces import Lifecycle


def test_orphan_reap_reason_cites_time_since_session_end_when_session_end_present() -> (
    None
):
    """ORPHAN_REAP message MUST cite time_since_session_end vs grace.

    Forbidden: literal substring `sentinel_age=` (the misleading old phrasing).
    Required: numeric `time_since_session_end=<int>s` and the configured grace.
    """
    now = 10_000.0
    entry = {
        "id": "i-1",
        "created_at": 0.0,
        "session_end": 8_000.0,
        "heartbeat_thread_tick": 9_500.0,  # sentinel only 500s stale (irrelevant)
    }
    lifecycle = Lifecycle(grace_after_session_s=1_800.0)

    reason = _refuse_reason_for_verdict("ORPHAN_REAP", entry, lifecycle, now)

    assert "sentinel_age=" not in reason, (
        f"refuse reason still cites sentinel_age (decision is not based on it): {reason!r}"
    )
    # time_since_session_end = 10_000 - 8_000 = 2_000
    assert "time_since_session_end=2000s" in reason, reason
    assert "grace_after_session_s=1800s" in reason, reason


def test_orphan_reap_reason_falls_back_to_pod_age_when_session_end_absent() -> None:
    """Legacy entry without session_end: cite pod_age vs grace, not sentinel."""
    now = 10_000.0
    entry = {
        "id": "i-1",
        "created_at": 0.0,
        "heartbeat_thread_tick": 9_500.0,
    }
    lifecycle = Lifecycle(grace_after_session_s=1_800.0)

    reason = _refuse_reason_for_verdict("ORPHAN_REAP", entry, lifecycle, now)

    assert "sentinel_age=" not in reason, reason
    # pod_age = 10_000 - 0 = 10_000
    assert "pod_age=10000s" in reason, reason
    assert "grace_after_session_s=1800s" in reason, reason
