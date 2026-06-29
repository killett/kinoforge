"""Unit tests for SweeperLoop bounded stall-history deques.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §4.2
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from kinoforge.core.clock import FakeClock
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
from kinoforge.core.reaper_actor import SweepReport
from kinoforge.core.sweeper import SweeperLoop
from kinoforge.stores.local import LocalArtifactStore

_THR: dict[str, Any] = {
    "max_lifetime_s": 5 * 3600.0,
    "stall_window_s": 120.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "heartbeat_interval_s": 30.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def _empty_registry(name: str) -> Any:
    return None


def _make_loop(
    tmp_path: Any,
    sweep_fn: Any,
    interval_s: float = 30.0,
    thresholds: dict[str, Any] | None = None,
) -> SweeperLoop:
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    return SweeperLoop(
        store=store,
        ledger=ledger,
        registry_get_provider=_empty_registry,
        thresholds=thresholds or _THR,
        interval_s=interval_s,
        host="test-host",
        policy=DEFAULT_APPLY_POLICY,
        clock=FakeClock(start=1.0e6),
        _sweep_fn=sweep_fn,
    )


def _ephemeral_entry(
    pod_id: str, gpu: float = 0.0, cpu: float = 0.0, probe_state: str = "ok"
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": pod_id,
        "provider": "runpod",
        "kinoforge_ephemeral": True,
        "probe_state": probe_state,
        "created_at": 1.0e6 - 60.0,
    }
    if probe_state == "ok":
        entry["gpu_util_pct"] = gpu
        entry["cpu_pct"] = cpu
    return entry


def test_loop_owns_empty_stall_history_at_init(tmp_path: Any) -> None:
    """SweeperLoop._stall_history exists and is empty before any tick."""

    def noop_sweep(*args: Any, **kw: Any) -> SweepReport:
        return SweepReport(snapshot={}, actions=[])

    loop = _make_loop(tmp_path, noop_sweep)
    history = loop._stall_history
    assert isinstance(history, dict)
    assert history == {}


def test_loop_passes_stall_history_to_sweep(tmp_path: Any) -> None:
    """Each tick forwards SweeperLoop._stall_history to sweep_fn via kwarg."""
    captured: dict[str, Any] = {}

    def capturing_sweep(*args: Any, **kw: Any) -> SweepReport:
        captured["stall_history"] = kw.get("stall_history")
        return SweepReport(snapshot={}, actions=[])

    loop = _make_loop(tmp_path, capturing_sweep)
    loop._tick_once()
    assert captured["stall_history"] is loop._stall_history


def test_loop_appends_ok_probe_sample_per_tick(tmp_path: Any) -> None:
    """Each ok-probe ephemeral entry in the snapshot appends (gpu, cpu) to its deque."""

    def sweep_with_one_ok(*args: Any, **kw: Any) -> SweepReport:
        entry = _ephemeral_entry("pod-1", gpu=0.0, cpu=0.0, probe_state="ok")
        return SweepReport(snapshot={"pod-1": (entry, Verdict.LIVE)}, actions=[])

    loop = _make_loop(tmp_path, sweep_with_one_ok)
    loop._tick_once()
    history = loop._stall_history
    assert "pod-1" in history
    assert list(history["pod-1"]) == [(0.0, 0.0)]
    loop._tick_once()
    assert list(history["pod-1"]) == [(0.0, 0.0), (0.0, 0.0)]


def test_loop_deque_maxlen_bounded(tmp_path: Any) -> None:
    """Deque caps at ceil(stall_window_s / interval_s); oldest evicted FIFO."""

    def sweep_one(*args: Any, **kw: Any) -> SweepReport:
        entry = _ephemeral_entry("pod-1", gpu=0.0, cpu=0.0, probe_state="ok")
        return SweepReport(snapshot={"pod-1": (entry, Verdict.LIVE)}, actions=[])

    # stall_window=120, interval=30 → 4 samples
    loop = _make_loop(tmp_path, sweep_one, interval_s=30.0)
    expected_maxlen = math.ceil(120.0 / 30.0)
    for _ in range(expected_maxlen + 5):
        loop._tick_once()
    history = loop._stall_history
    assert history["pod-1"].maxlen == expected_maxlen
    assert len(history["pod-1"]) == expected_maxlen


def test_loop_evicts_history_when_pod_no_longer_in_snapshot(tmp_path: Any) -> None:
    """Pod gone from snapshot → its deque entry pruned from _stall_history."""
    state = {"present": True}

    def conditional_sweep(*args: Any, **kw: Any) -> SweepReport:
        if state["present"]:
            entry = _ephemeral_entry("pod-1", gpu=0.0, cpu=0.0)
            return SweepReport(snapshot={"pod-1": (entry, Verdict.LIVE)}, actions=[])
        return SweepReport(snapshot={}, actions=[])

    loop = _make_loop(tmp_path, conditional_sweep)
    loop._tick_once()
    assert "pod-1" in loop._stall_history
    state["present"] = False
    loop._tick_once()
    assert "pod-1" not in loop._stall_history


def test_loop_skips_non_ok_probe_states(tmp_path: Any) -> None:
    """Failed / no_substrate / not_found probes do NOT append to history."""

    def sweep_failed(*args: Any, **kw: Any) -> SweepReport:
        entries = {
            "p-fail": (
                _ephemeral_entry("p-fail", probe_state="failed"),
                Verdict.PROBE_FAILED,
            ),
            "p-none": (
                _ephemeral_entry("p-none", probe_state="no_substrate"),
                Verdict.SKIP_NO_PROBE,
            ),
            "p-404": (
                _ephemeral_entry("p-404", probe_state="not_found"),
                Verdict.GC_404,
            ),
        }
        return SweepReport(snapshot=entries, actions=[])

    loop = _make_loop(tmp_path, sweep_failed)
    loop._tick_once()
    history = loop._stall_history
    assert history == {}


def test_loop_skips_ledger_backed_entries(tmp_path: Any) -> None:
    """Ledger entries (no kinoforge_ephemeral flag) never appended to history."""

    def sweep_ledger(*args: Any, **kw: Any) -> SweepReport:
        ledger_entry: dict[str, Any] = {
            "id": "ledger-pod",
            "provider": "runpod",
            # NO kinoforge_ephemeral key
        }
        return SweepReport(
            snapshot={"ledger-pod": (ledger_entry, Verdict.LIVE)}, actions=[]
        )

    loop = _make_loop(tmp_path, sweep_ledger)
    loop._tick_once()
    assert "ledger-pod" not in loop._stall_history


def test_loop_reload_preserves_history(tmp_path: Any) -> None:
    """reload() swaps policy/thresholds atomically; _stall_history persists."""

    def sweep_one(*args: Any, **kw: Any) -> SweepReport:
        entry = _ephemeral_entry("pod-1", gpu=0.0, cpu=0.0)
        return SweepReport(snapshot={"pod-1": (entry, Verdict.LIVE)}, actions=[])

    loop = _make_loop(tmp_path, sweep_one)
    loop._tick_once()
    snapshot_history = dict(loop._stall_history)
    new_thresholds = {**_THR, "stall_window_s": 240.0}
    loop.reload(thresholds=new_thresholds)
    assert "pod-1" in loop._stall_history
    assert list(loop._stall_history["pod-1"]) == list(snapshot_history["pod-1"])


_ = deque  # imported for type-annotation hint
