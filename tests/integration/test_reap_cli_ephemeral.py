"""kinoforge reap CLI + one-shot STALL-skip contract.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5
"""

from __future__ import annotations

from typing import Any

from kinoforge.core.clock import FakeClock
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
from kinoforge.core.reaper_actor import reset_warning_dedup, sweep
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _ZeroProvider:
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
    "stall_window_s": 30.0,
    "heartbeat_interval_s": 30.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def _add_row(index: EphemeralIndex, pod_id: str = "pod-1") -> None:
    index.add(
        EphemeralIndexRow(
            id=pod_id,
            warm_attach_key="w",
            kinoforge_key="k-12345678901",
            endpoints={},
            provider="runpod",
            created_at_local="2026-06-28T12:00:00",
        )
    )


def test_reap_one_shot_skips_stall_reap(tmp_path: Any) -> None:
    """`kinoforge reap` one-shot passes stall_history=None → STALL never fires.

    Mimics the CLI one-shot. Even with a zero-util probe and a tiny
    stall_window_s, the verdict must be LIVE (not STALL_REAP) because
    no cross-tick history exists.
    """
    reset_warning_dedup()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    _add_row(index)
    provider = _ZeroProvider()

    def get_provider(name: str) -> Any:
        return (lambda: provider) if name == "runpod" else None

    report = sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        FakeClock(start=1.0e6),
        policy=DEFAULT_APPLY_POLICY,
        stall_history=None,
    )

    _entry, verdict = report.snapshot["pod-1"]
    assert verdict == Verdict.LIVE
    assert provider.destroy_calls == []


def test_sweeper_stats_count_gc_404_and_probe_failed(tmp_path: Any) -> None:
    """SweeperStats counters increment for the new ephemeral action literals."""
    from kinoforge.core.reaper_actor import ActionResult
    from kinoforge.core.sweeper import _SweeperStats

    stats = _SweeperStats()
    snapshot: dict[str, Any] = {}
    actions = [
        ActionResult(
            instance_id="p1",
            snapshot_verdict=Verdict.GC_404,
            applied_verdict=Verdict.GC_404,
            action="gc_404_removed",
        ),
        ActionResult(
            instance_id="p2",
            snapshot_verdict=Verdict.PROBE_FAILED,
            applied_verdict=Verdict.PROBE_FAILED,
            action="probe_failed",
        ),
    ]
    from kinoforge.core.reaper_actor import SweepReport

    snapshot["p3"] = (
        {"id": "p3", "kinoforge_ephemeral": True, "probe_state": "no_substrate"},
        Verdict.SKIP_NO_PROBE,
    )
    snapshot["p4"] = (
        {"id": "p4", "kinoforge_ephemeral": True, "probe_state": "failed"},
        Verdict.PROBE_FAILED,
    )
    stats.fold(SweepReport(snapshot=snapshot, actions=actions), now=0.0)

    assert stats.gc_404_total == 1
    assert stats.probe_failed_total == 1
    assert stats.skip_no_probe_total == 1
    assert stats.probe_failed_seen == 1  # from snapshot accounting


def test_emit_reap_jsonl_includes_new_action_literals() -> None:
    """JSONL action records expose gc_404_removed and probe_failed as-is."""
    import io
    import json
    from contextlib import redirect_stdout

    from kinoforge.cli._commands import _emit_reap_jsonl
    from kinoforge.core.reaper_actor import ActionResult, SweepReport

    report = SweepReport(
        snapshot={
            "p1": (
                {"id": "p1", "kinoforge_ephemeral": True, "probe_state": "not_found"},
                Verdict.GC_404,
            ),
        },
        actions=[
            ActionResult(
                instance_id="p1",
                snapshot_verdict=Verdict.GC_404,
                applied_verdict=Verdict.GC_404,
                action="gc_404_removed",
            ),
            ActionResult(
                instance_id="p2",
                snapshot_verdict=Verdict.PROBE_FAILED,
                applied_verdict=Verdict.PROBE_FAILED,
                action="probe_failed",
                reason="TransportError",
            ),
        ],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        _emit_reap_jsonl(report)
    out = buf.getvalue().splitlines()
    parsed = [json.loads(line) for line in out]
    action_records = [r for r in parsed if r["type"] == "action"]
    assert any(r["action"] == "gc_404_removed" for r in action_records)
    assert any(r["action"] == "probe_failed" for r in action_records)


def test_emit_reap_human_includes_gc_404_display() -> None:
    """Human output prints GC_404 verdict + ephemeral pod id."""
    import io
    from contextlib import redirect_stdout

    from kinoforge.cli._commands import _emit_reap_human
    from kinoforge.core.reaper_actor import ActionResult, SweepReport

    report = SweepReport(
        snapshot={
            "pod-stale": (
                {
                    "id": "pod-stale",
                    "provider": "runpod",
                    "kinoforge_ephemeral": True,
                    "probe_state": "not_found",
                    "created_at": 1.0e6,
                },
                Verdict.GC_404,
            ),
        },
        actions=[
            ActionResult(
                instance_id="pod-stale",
                snapshot_verdict=Verdict.GC_404,
                applied_verdict=Verdict.GC_404,
                action="gc_404_removed",
            ),
        ],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        _emit_reap_human(report, applied=True, include_orphans=False)
    out = buf.getvalue()
    assert "GC_404" in out
    assert "pod-stale" in out
    assert "gc" in out.lower()
