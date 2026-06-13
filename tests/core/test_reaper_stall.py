"""C26 STALL_REAP branch tests for classify() (Task 7)."""

from __future__ import annotations

from typing import Any

from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict, classify


def _entry(
    *,
    eid: str = "p1",
    created_at: float = 0.0,
    last_hb: float = 9.0,
    hb_tick: float = 9.0,
    util_tick: float | None = 9.0,
    counter: int | None = 12,
    provider: str = "runpod",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "id": eid,
        "created_at": created_at,
        "last_heartbeat": last_hb,
        "heartbeat_thread_tick": hb_tick,
        "provider": provider,
    }
    if util_tick is not None:
        e["util_thread_tick"] = util_tick
    if counter is not None:
        e["consecutive_low_util_count"] = counter
    if extra:
        e.update(extra)
    return e


_KW: dict[str, Any] = {
    "idle_timeout_s": 3600.0,
    "max_lifetime_s": 18000.0,
    "heartbeat_interval_s": 30.0,
    "grace_after_session_s": 300.0,
    "stall_window_s": 300.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 20.0,
}


def test_stall_reap_appended_at_end_of_verdict_enum() -> None:
    members = list(Verdict)
    assert members[-1] == Verdict.STALL_REAP
    assert Verdict.STALL_REAP.value == "STALL_REAP"


def test_default_apply_policy_includes_stall_reap() -> None:
    assert Verdict.STALL_REAP in DEFAULT_APPLY_POLICY.act_verdicts


def test_stall_reap_fires_when_consecutive_low_exceeds_window() -> None:
    """counter (12) × interval (30 s) = 360 s ≥ window (300 s) → STALL_REAP."""
    entry = _entry(counter=12, util_tick=9.0)
    verdict = classify(entry, frozenset({"p1"}), now=10.0, **_KW)
    assert verdict == Verdict.STALL_REAP


def test_stall_reap_suppressed_when_counter_below_window() -> None:
    """counter (5) × interval (30 s) = 150 s < window (300 s) → LIVE."""
    entry = _entry(counter=5, util_tick=9.0)
    verdict = classify(entry, frozenset({"p1"}), now=10.0, **_KW)
    assert verdict == Verdict.LIVE


def test_stall_reap_suppressed_when_util_tick_stale() -> None:
    """util_tick older than sentinel_window (3×30=90s) suppresses STALL."""
    entry = _entry(counter=12, util_tick=-200.0)  # now - tick = 210s > 90s
    verdict = classify(entry, frozenset({"p1"}), now=10.0, **_KW)
    assert verdict == Verdict.LIVE


def test_stall_reap_suppressed_when_stall_window_s_none() -> None:
    """Kill switch: stall_window_s=None → never STALL_REAP."""
    entry = _entry(counter=999, util_tick=9.0)
    kw = {**_KW, "stall_window_s": None}
    verdict = classify(entry, frozenset({"p1"}), now=10.0, **kw)
    assert verdict == Verdict.LIVE


def test_stall_reap_suppressed_on_legacy_entry_missing_util_fields() -> None:
    """No counter / no util_tick → LIVE (backward compat)."""
    entry = _entry(counter=None, util_tick=None)
    verdict = classify(entry, frozenset({"p1"}), now=10.0, **_KW)
    assert verdict == Verdict.LIVE


def test_stall_reap_per_entry_override_via_ledger_field() -> None:
    """Per-entry stall_window_s override beats default — and disables."""
    # counter × interval = 360 s, default window 300 s → would fire.
    # Per-entry override 600 s → 360 s < 600 s → LIVE.
    entry = _entry(counter=12, util_tick=9.0, extra={"stall_window_s": 600.0})
    verdict = classify(entry, frozenset({"p1"}), now=10.0, **_KW)
    assert verdict == Verdict.LIVE


def test_stall_reap_suppressed_on_provider_without_util_substrate() -> None:
    """SkyPilot has no util substrate → suppressed."""
    entry = _entry(counter=12, util_tick=9.0, provider="skypilot")
    verdict = classify(entry, frozenset({"p1"}), now=10.0, **_KW)
    assert verdict == Verdict.LIVE


def test_stall_reap_fires_with_per_entry_override_that_enables() -> None:
    """Per-entry override 200 s → 360 s ≥ 200 s → STALL_REAP (overrides 600 s default)."""
    kw = {**_KW, "stall_window_s": 600.0}
    entry = _entry(counter=12, util_tick=9.0, extra={"stall_window_s": 200.0})
    verdict = classify(entry, frozenset({"p1"}), now=10.0, **kw)
    assert verdict == Verdict.STALL_REAP
