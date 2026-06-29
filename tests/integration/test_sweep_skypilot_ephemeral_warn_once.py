"""Integration: probe=None → SKIP_NO_PROBE, WARN-once dedup.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY
from kinoforge.core.reaper_actor import (
    _WARNED_PROBE_MISSING,
    reset_warning_dedup,
    sweep,
)
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _NoProbeProvider:
    """Provider that returns None from probe_runtime (substrate-missing)."""

    name = "skypilot"

    def list_instances(self) -> list[Any]:
        return []

    def probe_runtime(self, pod_id: str) -> None:
        return None

    def destroy_instance(self, instance_id: str) -> None:
        pass

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
    "stall_window_s": 90.0,
    "heartbeat_interval_s": 30.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def _add_row(index: EphemeralIndex, pod_id: str = "sky-pod") -> None:
    index.add(
        EphemeralIndexRow(
            id=pod_id,
            warm_attach_key="w",
            kinoforge_key="k-12345678901",
            endpoints={},
            provider="skypilot",
            created_at_local="2026-06-28T12:00:00",
        )
    )


def test_two_ticks_yield_one_warn_log(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """SKIP_NO_PROBE WARN dedup: 2 ticks for same pod → 1 WARN line."""
    reset_warning_dedup()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    _add_row(index)

    def get_provider(name: str) -> Any:
        return (lambda: _NoProbeProvider()) if name == "skypilot" else None

    caplog.set_level(logging.WARNING, logger="kinoforge.core.reaper_actor")
    for _ in range(2):
        sweep(
            store,
            ledger,
            get_provider,
            _THRESHOLDS,
            FakeClock(start=1.0e6),
            policy=DEFAULT_APPLY_POLICY,
            stall_history={},
        )
    warn_lines = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "SKIP_NO_PROBE" in r.getMessage()
    ]
    assert len(warn_lines) == 1
    assert ("skypilot", "sky-pod") in _WARNED_PROBE_MISSING


def test_skip_no_probe_does_not_destroy(tmp_path: Any) -> None:
    """SKIP_NO_PROBE → outside apply policy → no action, no destroy, row stays."""
    reset_warning_dedup()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    _add_row(index)
    p = _NoProbeProvider()

    def get_provider(name: str) -> Any:
        return (lambda: p) if name == "skypilot" else None

    report = sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        FakeClock(start=1.0e6),
        policy=DEFAULT_APPLY_POLICY,
        stall_history={},
    )

    from kinoforge.core.reaper import Verdict as _V

    assert report.snapshot["sky-pod"][1] == _V.SKIP_NO_PROBE
    # SKIP_NO_PROBE is not in DEFAULT_APPLY_POLICY → no ActionResult entry.
    assert all(a.instance_id != "sky-pod" for a in report.actions)
    # Index row still present (no GC for SKIP_NO_PROBE).
    assert any(r.id == "sky-pod" for r in index.rows())
