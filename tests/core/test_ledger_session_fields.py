"""B3 Task a — `is_session_busy` pure helper + `Ledger.touch` session fields."""

from __future__ import annotations

import pytest

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import _PROTECTED_LEDGER_KEYS, Ledger, is_session_busy
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture
def ledger(tmp_path):
    store = LocalArtifactStore(tmp_path)
    ld = Ledger(store=store, run_id="_test")
    ld.record(
        Instance(
            id="pod-001",
            provider="runpod",
            tags={"kinoforge_key": "abc123"},
            created_at=100.0,
            cost_rate_usd_per_hr=0.5,
            status="ready",
            endpoints={},
        )
    )
    return ld


# ---------------------------------------------------------------------------
# is_session_busy
# ---------------------------------------------------------------------------


def test_is_session_busy_false_when_session_start_absent():
    """Bug: returning True when no session ever started would block all warm-reuse."""
    assert is_session_busy({}, now=100.0, heartbeat_interval_s=30.0) is False


def test_is_session_busy_true_when_session_start_set_session_end_absent_hb_fresh():
    """Bug: returning False on an open session would let two CLIs collide on same pod."""
    entry = {"session_start": 100.0, "heartbeat_thread_tick": 100.0}
    assert is_session_busy(entry, now=100.0, heartbeat_interval_s=30.0) is True


def test_is_session_busy_false_when_session_end_GTE_session_start():
    """Bug: cleanly-closed sessions appearing busy would force unnecessary cold creates."""
    entry = {"session_start": 100.0, "session_end": 200.0}
    assert is_session_busy(entry, now=300.0, heartbeat_interval_s=30.0) is False


def test_is_session_busy_true_when_session_end_lt_session_start():
    """Bug: a later session's session_start with earlier stale session_end must read as busy."""
    entry = {
        "session_start": 200.0,
        "session_end": 100.0,
        "heartbeat_thread_tick": 200.0,
    }
    assert is_session_busy(entry, now=200.0, heartbeat_interval_s=30.0) is True


def test_is_session_busy_false_when_heartbeat_thread_tick_stale():
    """Bug: stale-busy not clearing would forever block warm-reuse after a crash."""
    entry = {"session_start": 100.0, "heartbeat_thread_tick": 100.0}
    # 200 - 100 = 100 > 3 * 30 = 90 → stale
    assert is_session_busy(entry, now=200.0, heartbeat_interval_s=30.0) is False


def test_is_session_busy_false_when_heartbeat_thread_tick_missing():
    """Bug: crashed claimant before first tick must auto-clear, not pin pod as busy forever."""
    entry = {"session_start": 100.0}
    # HB enabled, tick never landed → crashed before first tick.
    assert is_session_busy(entry, now=100.0, heartbeat_interval_s=30.0) is False


def test_is_session_busy_true_when_heartbeat_interval_s_None_and_session_start_set():
    """Bug: HB-disabled invocation must trust the marker (no freshness gate available)."""
    entry = {"session_start": 100.0}
    assert is_session_busy(entry, now=100.0, heartbeat_interval_s=None) is True


def test_is_session_busy_uses_3x_sentinel_window_floor():
    """Bug: wrong multiplier would mis-classify near the dead-man window boundary."""
    entry = {"session_start": 100.0, "heartbeat_thread_tick": 100.0}
    # Exactly at 3 * 30 = 90 boundary → still fresh.
    assert is_session_busy(entry, now=190.0, heartbeat_interval_s=30.0) is True
    # Past 90 → stale.
    assert is_session_busy(entry, now=190.001, heartbeat_interval_s=30.0) is False


# ---------------------------------------------------------------------------
# Ledger.touch session fields
# ---------------------------------------------------------------------------


def test_touch_writes_session_start(ledger):
    """Bug: session_start not persisting would break cross-CLI busy detection."""
    changed = ledger.touch("pod-001", session_start=150.0)
    assert changed is True
    entry = ledger.read("pod-001")
    assert entry is not None
    assert entry["session_start"] == 150.0


def test_touch_writes_session_end(ledger):
    """Bug: session_end not persisting would never auto-clear busy state."""
    ledger.touch("pod-001", session_start=150.0)
    ledger.touch("pod-001", session_end=200.0)
    entry = ledger.read("pod-001")
    assert entry is not None
    assert entry["session_end"] == 200.0
    assert entry["session_start"] == 150.0  # preserved


def test_touch_session_start_then_session_end_both_persisted(ledger):
    """Bug: second touch clobbering first would lose causal ordering."""
    ledger.touch("pod-001", session_start=150.0)
    ledger.touch("pod-001", session_end=200.0)
    entry = ledger.read("pod-001")
    assert entry is not None
    assert entry["session_start"] == 150.0
    assert entry["session_end"] == 200.0


def test_protected_keys_filter_does_not_drop_session_fields():
    """Bug: adding session_* to _PROTECTED would silently drop the writes."""
    assert "session_start" not in _PROTECTED_LEDGER_KEYS
    assert "session_end" not in _PROTECTED_LEDGER_KEYS
