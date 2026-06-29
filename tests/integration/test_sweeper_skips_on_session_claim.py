"""Integration: sweeper defers when provision:<pod_id> lock is held.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5
"""

from __future__ import annotations

from typing import Any

from kinoforge.core.clock import FakeClock
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY
from kinoforge.core.reaper_actor import reset_warning_dedup, sweep
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _ZeroUtilProvider:
    """Returns LIVE-but-zero-util so verdict would be STALL_REAP without claim."""

    name = "runpod"

    def __init__(self) -> None:
        self.destroy_calls: list[str] = []

    def list_instances(self) -> list[Any]:
        return []

    def probe_runtime(self, pod_id: str) -> RuntimeProbe:
        return RuntimeProbe(
            pod_id=pod_id,
            found=True,
            container_uptime_s=60.0,
            gpu_util_pct=0.0,
            cpu_pct=0.0,
            cost_per_hr=None,
            probed_at_local="2026-06-28T12:00:00",
        )

    def destroy_instance(self, instance_id: str) -> None:
        self.destroy_calls.append(instance_id)

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


_THRESHOLDS: dict[str, Any] = {
    "max_lifetime_s": 5 * 3600.0,
    "stall_window_s": 60.0,
    "heartbeat_interval_s": 30.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def test_provision_lock_held_defers_action(tmp_path: Any) -> None:
    """STALL_REAP would fire, but provision:<id> lock held → action deferred."""
    reset_warning_dedup()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    index.add(
        EphemeralIndexRow(
            id="claimed-pod",
            warm_attach_key="w",
            kinoforge_key="k-12345678901",
            endpoints={},
            provider="runpod",
            created_at_local="2026-06-28T12:00:00",
        )
    )
    provider = _ZeroUtilProvider()

    def get_provider(name: str) -> Any:
        return (lambda: provider) if name == "runpod" else None

    clock = FakeClock(start=1.0e6)

    from collections import deque as _dq

    # Pre-populate history so STALL_REAP would fire next tick (window=60,
    # interval=30 → 2 zero samples needed).
    history: dict[str, Any] = {"claimed-pod": _dq([(0.0, 0.0), (0.0, 0.0)], maxlen=2)}

    # Hold the provision lock; sweep must defer.
    lock = store.acquire_lock("provision:claimed-pod", ttl_s=60.0)
    token = lock.acquire(blocking=True)
    assert token is not None
    try:
        report = sweep(
            store,
            ledger,
            get_provider,
            _THRESHOLDS,
            clock,
            policy=DEFAULT_APPLY_POLICY,
            stall_history=history,
        )
        assert provider.destroy_calls == []
        actions_for_pod = [a for a in report.actions if a.instance_id == "claimed-pod"]
        assert actions_for_pod, "expected an ActionResult for claimed-pod"
        assert actions_for_pod[-1].action == "deferred-session-claim"
    finally:
        lock.release(token)


def test_provision_lock_released_destroys_next_tick(tmp_path: Any) -> None:
    """After provision lock released, next sweep destroys the wedged pod."""
    reset_warning_dedup()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    index.add(
        EphemeralIndexRow(
            id="claimed-pod",
            warm_attach_key="w",
            kinoforge_key="k-12345678901",
            endpoints={},
            provider="runpod",
            created_at_local="2026-06-28T12:00:00",
        )
    )
    provider = _ZeroUtilProvider()

    def get_provider(name: str) -> Any:
        return (lambda: provider) if name == "runpod" else None

    clock = FakeClock(start=1.0e6)
    from collections import deque as _dq

    history: dict[str, Any] = {"claimed-pod": _dq([(0.0, 0.0), (0.0, 0.0)], maxlen=2)}

    sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        clock,
        policy=DEFAULT_APPLY_POLICY,
        stall_history=history,
    )
    # No lock — destroy fires.
    assert provider.destroy_calls == ["claimed-pod"]
    assert not any(r.id == "claimed-pod" for r in index.rows())
