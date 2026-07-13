"""Integration: Modal EphemeralIndexRow flows sweep → probe → GC_404 / STALL_REAP.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5
Plan: docs/superpowers/plans/2026-07-12-modal-ephemeral-parity.md Task 7
"""

from __future__ import annotations

from collections import deque
from typing import Any

from kinoforge.core.clock import FakeClock
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
from kinoforge.core.reaper_actor import reset_warning_dedup, sweep
from kinoforge.core.util_endpoints import UtilSnapshot
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.providers.modal import ModalProvider
from kinoforge.stores.local import LocalArtifactStore

# Thresholds mirror the RunPod offline reference test
# (tests/integration/test_sweeper_reaps_ephemeral_stall.py).
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

_EPH_ID = "eph-feedf00d"
_MODAL_URL = "https://acct--kinoforge-eph-feedf00d-fn.modal.run"
_APP_NAME = f"kinoforge-{_EPH_ID}"


def _add_modal_row(index: EphemeralIndex, eph_id: str = _EPH_ID) -> None:
    """Add a Modal EphemeralIndexRow to *index*."""
    index.add(
        EphemeralIndexRow(
            id=eph_id,
            warm_attach_key="w",
            kinoforge_key="k-12345678901",
            endpoints={"8000": _MODAL_URL},
            provider="modal",
            created_at_local="2026-07-12T12:00:00",
        )
    )


def test_gone_modal_app_row_is_gc404(tmp_path: Any) -> None:
    """Bug caught: Modal rows stuck at SKIP_NO_PROBE forever (no probe
    substrate) — dead rows accumulate and the index never converges.

    A lister that returns no active apps must produce GC_404 →
    the index row is removed and stopper is NEVER called (app already gone).
    """
    reset_warning_dedup()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    _add_modal_row(index)

    stopper_calls: list[str] = []

    def _fake_stopper(app_name: str) -> None:
        stopper_calls.append(app_name)

    def get_provider(name: str) -> Any:
        if name != "modal":
            return None
        provider = ModalProvider(
            lister=lambda: [],  # app absent — returns empty list
            stopper=_fake_stopper,
            sleep=lambda _: None,
        )
        return lambda: provider

    report = sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        FakeClock(start=1.0e6),
        policy=DEFAULT_APPLY_POLICY,
        stall_history={},
    )

    assert report.snapshot[_EPH_ID][1] == Verdict.GC_404
    assert stopper_calls == [], "stopper must NOT be called for an already-gone app"
    assert not any(r.id == _EPH_ID for r in index.rows()), "row must be removed"
    assert any(a.action == "gc_404_removed" for a in report.actions)


def test_idle_modal_app_is_stall_reaped(tmp_path: Any, monkeypatch: Any) -> None:
    """Bug caught: an orphaned bare-ephemeral Modal app idling forever,
    invisible to kinoforge (memory-only ledger) and never reaped.

    With stall_history pre-filled with enough consecutive zero-util samples
    the sweep must classify STALL_REAP → call destroy_instance (which calls
    stopper) → remove the index row.
    """
    reset_warning_dedup()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    _add_modal_row(index)

    # Stateful lister: returns the active app until the stopper fires, then
    # returns empty so destroy_instance's post-stop poll loop exits cleanly.
    stopper_calls: list[str] = []

    def _fake_lister() -> list[dict[str, Any]]:
        if stopper_calls:
            return []
        return [{"name": _APP_NAME, "state": "deployed"}]

    def _fake_stopper(app_name: str) -> None:
        stopper_calls.append(app_name)

    # Monkeypatch ModalUtilEndpoint.read_util to return a zero-util snapshot.
    # This patch IS reachable: sweep's _probe_with_cache calls
    # note_endpoints(row.id, row.endpoints) BEFORE probe_runtime, priming the
    # provider's URL cache from the index row, so probe_runtime takes the
    # url-known branch and hits ModalUtilEndpoint.read_util (a reviewer once
    # misjudged this patch dead because the provider was built with no
    # deployments — the priming happens inside the sweep, not here).
    idle_snap = UtilSnapshot(
        gpu_util_percent=0.0,
        cpu_percent=0.0,
        memory_percent=5.0,
        disk_percent=None,
        uptime_seconds=300,
    )

    monkeypatch.setattr(
        "kinoforge.providers.modal.util.ModalUtilEndpoint.read_util",
        lambda self, instance_id: idle_snap,
    )

    provider = ModalProvider(
        lister=_fake_lister,
        stopper=_fake_stopper,
        sleep=lambda _: None,
    )

    def get_provider(name: str) -> Any:
        if name != "modal":
            return None
        return lambda: provider

    # Pre-fill stall_history with enough consecutive zero-util samples to
    # trigger STALL_REAP. Required samples = ceil(stall_window_s /
    # heartbeat_interval_s) = ceil(90 / 30) = 3. classify READS the mapping
    # as passed — it does not append the current probe's sample (that is
    # SweeperLoop's job between ticks) — so all 3 must be pre-loaded here.
    history: dict[str, Any] = {
        _EPH_ID: deque([(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)], maxlen=3),
    }

    clock = FakeClock(start=1.0e6)

    report = sweep(
        store,
        ledger,
        get_provider,
        _THRESHOLDS,
        clock,
        policy=DEFAULT_APPLY_POLICY,
        stall_history=history,
    )

    assert report.snapshot[_EPH_ID][1] == Verdict.STALL_REAP, (
        f"expected STALL_REAP, got {report.snapshot[_EPH_ID][1]}"
    )
    assert stopper_calls == [_APP_NAME], (
        f"destroy_instance must call stopper with {_APP_NAME!r}; got {stopper_calls!r}"
    )
    assert not any(r.id == _EPH_ID for r in index.rows()), (
        "EphemeralIndex row must be removed after STALL_REAP"
    )
    assert any(
        a.action in {"destroyed_and_forgot", "gc_404_removed"} for a in report.actions
    ), f"expected a destroy/gc action, got: {[a.action for a in report.actions]}"
