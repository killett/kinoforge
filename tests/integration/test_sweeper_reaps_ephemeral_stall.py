"""Integration: scripted probe sequence drives STALL_REAP + index cleanup.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5
"""

from __future__ import annotations

from typing import Any

from kinoforge.core.clock import FakeClock

# Minimal local Ledger import — keep this test free of real instances.
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
from kinoforge.core.reaper_actor import reset_warning_dedup, sweep
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _ScriptedProvider:
    """Provider whose probe_runtime returns the next scripted value per call."""

    name = "runpod"

    def __init__(self, scripted: list[Any]) -> None:
        self.scripted = list(scripted)
        self.destroy_calls: list[str] = []

    def list_instances(self) -> list[Any]:
        return []

    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:
        if not self.scripted:
            raise RuntimeError("probe script exhausted")
        result = self.scripted.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def destroy_instance(self, instance_id: str) -> None:
        self.destroy_calls.append(instance_id)

    # ABC duck-stubs
    def find_offers(self, reqs: Any) -> list[Any]:
        return []

    def create_instance(self, spec: Any) -> Any:
        raise NotImplementedError

    def get_instance(self, instance_id: str) -> Any:
        raise NotImplementedError

    def stop_instance(self, instance_id: str) -> None:
        pass

    def heartbeat(self, instance_id: str) -> None:
        pass

    def endpoints(self, instance: Any) -> dict[str, str]:
        return {}


def _live_probe() -> RuntimeProbe:
    return RuntimeProbe(
        pod_id="pod-1",
        found=True,
        container_uptime_s=60.0,
        gpu_util_pct=80.0,
        cpu_pct=40.0,
        cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:00",
    )


def _zero_probe() -> RuntimeProbe:
    return RuntimeProbe(
        pod_id="pod-1",
        found=True,
        container_uptime_s=120.0,
        gpu_util_pct=0.0,
        cpu_pct=0.0,
        cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:00",
    )


def _gone_probe() -> RuntimeProbe:
    return RuntimeProbe(
        pod_id="pod-1",
        found=False,
        container_uptime_s=None,
        gpu_util_pct=None,
        cpu_pct=None,
        cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:00",
    )


_THRESHOLDS: dict[str, Any] = {
    "max_lifetime_s": 5 * 3600.0,
    "stall_window_s": 90.0,
    "heartbeat_interval_s": 30.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def _add_pod(index: EphemeralIndex, pod_id: str = "pod-1") -> None:
    index.add(
        EphemeralIndexRow(
            id=pod_id,
            warm_attach_key="w",
            kinoforge_key="k-12345678901",
            endpoints={"8188": f"https://{pod_id}"},
            provider="runpod",
            created_at_local="2026-06-28T12:00:00",
        )
    )


def test_stall_window_drives_destroy_and_index_removal(tmp_path: Any) -> None:
    """3 zero-util samples (window=90, interval=30) → STALL_REAP → destroy + GC."""
    reset_warning_dedup()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    _add_pod(index)
    provider = _ScriptedProvider(
        [_live_probe(), _zero_probe(), _zero_probe(), _zero_probe()]
    )

    def get_provider(name: str) -> Any:
        return (lambda: provider) if name == "runpod" else None

    clock = FakeClock(start=1.0e6)
    history: dict[str, Any] = {}

    # Tick 1: live util → LIVE
    report = sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        clock,
        policy=DEFAULT_APPLY_POLICY,
        stall_history=history,
    )
    assert report.snapshot["pod-1"][1] == Verdict.LIVE
    assert provider.destroy_calls == []

    # Sweeper-loop would append sample now; simulate by mutating history dict
    # the way SweeperLoop._update_stall_history does.
    from collections import deque as _dq

    history["pod-1"] = _dq([(80.0, 40.0)], maxlen=3)

    # Tick 2: zero — append
    clock._t += 30.0
    history["pod-1"].append((0.0, 0.0))
    sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        clock,
        policy=DEFAULT_APPLY_POLICY,
        stall_history=history,
    )
    assert provider.destroy_calls == []

    # Tick 3: still building
    clock._t += 30.0
    history["pod-1"].append((0.0, 0.0))
    sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        clock,
        policy=DEFAULT_APPLY_POLICY,
        stall_history=history,
    )

    # Tick 4: 3 zeros at the tail → STALL_REAP → destroy + remove row
    clock._t += 30.0
    history["pod-1"].append((0.0, 0.0))
    report = sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        clock,
        policy=DEFAULT_APPLY_POLICY,
        stall_history=history,
    )
    assert provider.destroy_calls == ["pod-1"]
    assert not any(r.id == "pod-1" for r in index.rows())


def test_gc_404_removes_row_no_destroy_called(tmp_path: Any) -> None:
    """probe_runtime returns found=False → GC_404 → index row gone, destroy NOT called."""
    reset_warning_dedup()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    _add_pod(index)
    provider = _ScriptedProvider([_gone_probe()])

    def get_provider(name: str) -> Any:
        return (lambda: provider) if name == "runpod" else None

    report = sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        FakeClock(start=1.0e6),
        policy=DEFAULT_APPLY_POLICY,
        stall_history={},
    )

    assert report.snapshot["pod-1"][1] == Verdict.GC_404
    assert provider.destroy_calls == []  # pod already gone
    assert not any(r.id == "pod-1" for r in index.rows())
    assert any(a.action == "gc_404_removed" for a in report.actions)
