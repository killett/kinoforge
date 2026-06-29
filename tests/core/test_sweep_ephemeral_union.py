"""Unit tests for sweep() ephemeral union + per-tick probe cache.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §3.6
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import TransportError
from kinoforge.core.interfaces import (
    ComputeProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import Verdict
from kinoforge.core.reaper_actor import sweep
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _FakeProvider(ComputeProvider):
    """Minimal ComputeProvider stub controllable via scripted_probes."""

    name = "runpod"

    def __init__(self, name: str = "runpod") -> None:
        self.name = name
        self.probe_calls: list[str] = []
        self.scripted_probes: dict[str, RuntimeProbe | None | Exception] = {}
        self._live_instances: list[Instance] = []

    def list_instances(self) -> list[Instance]:
        return list(self._live_instances)

    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:
        self.probe_calls.append(pod_id)
        result = self.scripted_probes.get(pod_id, None)
        if isinstance(result, Exception):
            raise result
        return result

    # ABC stubs (unused in these tests)
    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        return []

    def create_instance(self, spec: InstanceSpec) -> Instance:
        raise NotImplementedError

    def get_instance(self, instance_id: str) -> Instance:
        raise NotImplementedError

    def stop_instance(self, instance_id: str) -> None:
        pass

    def destroy_instance(self, instance_id: str) -> None:
        pass

    def heartbeat(self, instance_id: str) -> None:
        pass

    def endpoints(self, instance: Instance) -> dict[str, str]:
        return {}


def _registry_for(provider: ComputeProvider) -> Any:
    def get_provider(name: str) -> Any:
        return lambda: provider

    return get_provider


def _add_index_row(
    index: EphemeralIndex,
    pod_id: str,
    provider: str = "runpod",
) -> None:
    index.add(
        EphemeralIndexRow(
            id=pod_id,
            warm_attach_key="wak-1",
            kinoforge_key="k-12345678901",
            endpoints={"8188": f"https://{pod_id}-8188.proxy.runpod.net"},
            provider=provider,
            created_at_local="2026-06-28T12:00:00",
        )
    )


_THRESHOLDS: dict[str, Any] = {
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


def _live_probe(pod_id: str) -> RuntimeProbe:
    return RuntimeProbe(
        pod_id=pod_id,
        found=True,
        container_uptime_s=60.0,
        gpu_util_pct=50.0,
        cpu_pct=20.0,
        cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:01",
    )


def test_sweep_empty_ledger_one_ephemeral_row_classifies_one_entry(
    tmp_path: Any,
) -> None:
    """Empty ledger + 1 ephemeral row → 1 synthesised entry in snapshot."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    provider = _FakeProvider()
    provider.scripted_probes["pod-1"] = _live_probe("pod-1")
    _add_index_row(index, "pod-1")

    report = sweep(
        store, ledger, _registry_for(provider), _THRESHOLDS, FakeClock(start=1.0e6)
    )

    assert "pod-1" in {eid for eid in report.snapshot}
    entry, verdict = report.snapshot["pod-1"]
    assert entry.get("kinoforge_ephemeral") is True
    assert entry["probe_state"] == "ok"
    assert verdict == Verdict.LIVE


def test_sweep_overlap_ledger_wins_no_sentinel_added(tmp_path: Any) -> None:
    """Ledger entry for same id → ledger wins; ephemeral sentinel NOT added."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    provider = _FakeProvider()
    provider.scripted_probes["pod-1"] = _live_probe("pod-1")
    provider._live_instances = [
        Instance(id="pod-1", provider="runpod", status="ready", created_at=0.0)
    ]
    ledger.record(
        Instance(id="pod-1", provider="runpod", status="ready", created_at=0.0)
    )
    _add_index_row(index, "pod-1")

    report = sweep(
        store, ledger, _registry_for(provider), _THRESHOLDS, FakeClock(start=1.0e6)
    )

    entry, _verdict = report.snapshot["pod-1"]
    assert entry.get("kinoforge_ephemeral") is not True
    assert provider.probe_calls == []  # ledger pod doesn't get probed


def test_sweep_probe_cache_one_call_per_pod_per_tick(tmp_path: Any) -> None:
    """Per-tick probe cache: same pod probed exactly once per sweep call."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    provider = _FakeProvider()
    provider.scripted_probes["pod-1"] = _live_probe("pod-1")
    _add_index_row(index, "pod-1")

    sweep(store, ledger, _registry_for(provider), _THRESHOLDS, FakeClock(start=1.0e6))

    assert len(provider.probe_calls) == 1


def test_sweep_probe_failure_one_pod_does_not_abort_others(tmp_path: Any) -> None:
    """TransportError on pod-A → PROBE_FAILED; pod-B still classified LIVE."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    provider = _FakeProvider()
    provider.scripted_probes["pod-A"] = TransportError("simulated")
    provider.scripted_probes["pod-B"] = _live_probe("pod-B")
    _add_index_row(index, "pod-A")
    _add_index_row(index, "pod-B")

    report = sweep(
        store, ledger, _registry_for(provider), _THRESHOLDS, FakeClock(start=1.0e6)
    )

    entry_a, verdict_a = report.snapshot["pod-A"]
    assert entry_a["probe_state"] == "failed"
    assert verdict_a == Verdict.PROBE_FAILED
    entry_b, verdict_b = report.snapshot["pod-B"]
    assert entry_b["probe_state"] == "ok"
    assert verdict_b == Verdict.LIVE


def test_sweep_provider_returns_none_yields_no_substrate(tmp_path: Any) -> None:
    """provider.probe_runtime returns None → probe_state="no_substrate"."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    provider = _FakeProvider(name="skypilot")
    provider.scripted_probes["pod-1"] = None
    _add_index_row(index, "pod-1", provider="skypilot")

    report = sweep(
        store, ledger, _registry_for(provider), _THRESHOLDS, FakeClock(start=1.0e6)
    )

    entry, verdict = report.snapshot["pod-1"]
    assert entry["probe_state"] == "no_substrate"
    assert verdict == Verdict.SKIP_NO_PROBE


def test_sweep_policy_none_does_not_mutate_index(tmp_path: Any) -> None:
    """Read-only sweep (policy=None) NEVER removes an index row."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    provider = _FakeProvider()
    provider.scripted_probes["pod-dead"] = RuntimeProbe(
        pod_id="pod-dead",
        found=False,
        container_uptime_s=None,
        gpu_util_pct=None,
        cpu_pct=None,
        cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:01",
    )
    _add_index_row(index, "pod-dead")

    sweep(
        store,
        ledger,
        _registry_for(provider),
        _THRESHOLDS,
        FakeClock(start=1.0e6),
        policy=None,
    )

    assert any(r.id == "pod-dead" for r in index.rows())


def test_sweep_unknown_provider_skips_row(tmp_path: Any) -> None:
    """Index row whose provider is not in the registry → row skipped silently."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    _add_index_row(index, "pod-1", provider="exotic")

    def empty_registry(name: str) -> Any:
        return None  # no factory for "exotic"

    report = sweep(store, ledger, empty_registry, _THRESHOLDS, FakeClock(start=1.0e6))

    # No snapshot entry for pod-1 since no provider could be resolved.
    assert "pod-1" not in report.snapshot


def test_sweep_passes_stall_history_through_to_classify(tmp_path: Any) -> None:
    """stall_history kwarg threads through sweep → classify → _classify_ephemeral.

    Reach: with 4 zero-util samples + stall_history passed in, the verdict
    should be STALL_REAP (not LIVE).
    """
    from collections import deque

    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    provider = _FakeProvider()
    provider.scripted_probes["pod-1"] = RuntimeProbe(
        pod_id="pod-1",
        found=True,
        container_uptime_s=300.0,
        gpu_util_pct=0.0,
        cpu_pct=0.0,
        cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:01",
    )
    _add_index_row(index, "pod-1")
    history: dict[str, Any] = {"pod-1": deque([(0.0, 0.0)] * 4)}

    report = sweep(
        store,
        ledger,
        _registry_for(provider),
        _THRESHOLDS,
        FakeClock(start=1.0e6),
        stall_history=history,
    )

    _entry, verdict = report.snapshot["pod-1"]
    assert verdict == Verdict.STALL_REAP


def test_sweep_unknown_provider_no_exception(tmp_path: Any) -> None:
    """Pytest sanity: regression check confirming graceful skip path."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    _add_index_row(index, "pod-z", provider="unknown")

    def empty_registry(name: str) -> Any:
        return None

    # Must not raise
    sweep(
        store,
        ledger,
        empty_registry,
        _THRESHOLDS,
        FakeClock(start=1.0e6),
    )


# Suppress unused-import lint for fixtures defined-then-used dynamically
_ = pytest
