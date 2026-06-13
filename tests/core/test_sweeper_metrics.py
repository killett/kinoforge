"""Layer W: sweeper_metrics renderers (human / JSON / Prom).

Pure functions; no fixtures beyond constructed dicts.
"""

from __future__ import annotations

import json
from typing import Any

from kinoforge.core.sweeper_metrics import (
    render_metrics_prom,
    render_status_human,
    render_status_json,
)


def _live_entry(*, now: float = 2000.0) -> dict[str, Any]:
    return {
        "id": "sweeper:hostname.local",
        "provider": "_sweeper",
        "pid": 12345,
        "last_heartbeat": now - 8.0,
        "heartbeat_thread_tick": now - 8.0,
        "sweeps_total": 1421,
        "destroys_total": 17,
        "errors_total": 0,
        "deferred_session_claim": 3,
        "deferred_heartbeat_unknown_skipped": 0,
        "deferred_heartbeat_substrate_missing": 0,
    }


def test_prom_format_emits_all_required_series() -> None:
    """All six required metric series emit, each with host label and
    HELP+TYPE lines. UTF-8 + LF endings."""
    entry = _live_entry(now=2000.0)
    out = render_metrics_prom(entry, host="hostname.local", interval_s=60.0)
    assert "\r\n" not in out and "\r" not in out
    for name in (
        "kinoforge_sweeper_last_sweep_ts",
        "kinoforge_sweeper_sweeps_total",
        "kinoforge_sweeper_destroys_total",
        "kinoforge_sweeper_deferred_total",
        "kinoforge_sweeper_errors_total",
        "kinoforge_sweeper_interval_s",
    ):
        assert f"# HELP {name}" in out
        assert f"# TYPE {name}" in out
    for reason in (
        "session-claim",
        "heartbeat-unknown-skipped",
        "heartbeat-substrate-missing",
    ):
        assert f'reason="{reason}"' in out
    assert out.count('host="hostname.local"') >= 6


def test_prom_omits_last_sweep_ts_when_no_entry() -> None:
    """No ledger entry → no last_sweep_ts series; zero counters elsewhere."""
    out = render_metrics_prom(None, host="hostname.local", interval_s=60.0)
    assert "kinoforge_sweeper_last_sweep_ts" not in out
    assert 'kinoforge_sweeper_sweeps_total{host="hostname.local"} 0' in out
    assert 'kinoforge_sweeper_errors_total{host="hostname.local"} 0' in out
    assert (
        'kinoforge_sweeper_deferred_total{host="hostname.local",reason="session-claim"} 0'
        in out
    )


def test_json_shape_lock_matches_status_spec() -> None:
    """Stable shape per spec §4.6.3."""
    entry = _live_entry(now=2000.0)
    out_str = render_status_json(
        entry, host="hostname.local", interval_s=60.0, now=2000.0
    )
    out = json.loads(out_str)
    assert out["host"] == "hostname.local"
    assert out["pid"] == 12345
    assert out["running"] is True
    assert out["last_sweep_age_s"] == 8
    assert out["interval_s"] == 60
    assert out["stale"] is False
    assert out["sweeps_total"] == 1421
    assert out["destroys_total"] == 17
    assert out["errors_total"] == 0
    assert out["deferred_total"] == {
        "session-claim": 3,
        "heartbeat-unknown-skipped": 0,
        "heartbeat-substrate-missing": 0,
    }


def test_json_stale_flag_computed_correctly() -> None:
    """stale iff last_sweep_age_s > 3 * interval_s. Boundary check."""
    interval = 60.0
    entry = _live_entry(now=2000.0)
    entry["heartbeat_thread_tick"] = 2000.0 - 180.0
    out = json.loads(
        render_status_json(entry, host="h", interval_s=interval, now=2000.0)
    )
    assert out["stale"] is False
    entry["heartbeat_thread_tick"] = 2000.0 - 180.5
    out = json.loads(
        render_status_json(entry, host="h", interval_s=interval, now=2000.0)
    )
    assert out["stale"] is True


def test_human_render_key_value_style() -> None:
    """Renders sibling-of-`kinoforge status` key=value lines."""
    entry = _live_entry(now=2000.0)
    out = render_status_human(entry, host="hostname.local", interval_s=60.0, now=2000.0)
    assert "{" not in out and "# HELP" not in out
    for line in (
        "host=hostname.local",
        "running=true",
        "pid=12345",
        "interval_s=60",
        "stale=false",
        "sweeps_total=1421",
        "destroys_total=17",
        "errors_total=0",
        "deferred_session_claim=3",
    ):
        assert line in out


def test_running_false_when_pid_missing() -> None:
    """running=false when entry present but pid missing/zero, or entry None."""
    entry = _live_entry(now=2000.0)
    entry.pop("pid")
    out = json.loads(render_status_json(entry, host="h", interval_s=60.0, now=2000.0))
    assert out["running"] is False
    out = json.loads(render_status_json(None, host="h", interval_s=60.0, now=2000.0))
    assert out["running"] is False
    assert out["pid"] is None
