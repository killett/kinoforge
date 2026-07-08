"""Heartbeat pod-gone detection: forget + stop when probe() says not-found."""

from __future__ import annotations

from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.core.heartbeat_loop import HeartbeatLoop


class _FakeProvider:
    def heartbeat(self, iid: str) -> None: ...
    def last_heartbeat(self, iid: str) -> float | None:
        return None

    def destroy_instance(self, iid: str) -> None: ...


class _RecordingLedger:
    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def touch(
        self,
        instance_id: str,
        *,
        last_heartbeat: float | None = None,
        **extra: Any,
    ) -> bool:
        return True

    def forget(self, iid: str) -> None:
        self.forgotten.append(iid)


class _Probe:
    """UtilSnapshotEndpoint stub with a settable probe() return."""

    def __init__(self, exists: bool) -> None:
        self._exists = exists

    def read_util(self, iid: str) -> None:
        return None

    def probe(self, iid: str) -> tuple[bool, None]:
        return (self._exists, None)


def _make_loop(
    *, exists: bool, ledger: _RecordingLedger, cancel: CancelToken
) -> HeartbeatLoop:
    return HeartbeatLoop(
        instance_id="pod1",
        provider=_FakeProvider(),
        ledger=ledger,
        interval_s=1.0,
        util_endpoint=_Probe(exists),
        cancel_token=cancel,
        provider_kind="runpod",
        # STALL/RESTART windows off — isolate pod-gone behavior:
        stall_window_s=None,
        restart_loop_window_s=None,
    )


def test_probe_not_found_forgets_and_stops() -> None:
    """probe(exists=False) → forget + cancel set + loop stop.

    Bug caught: a host-reclaimed pod is swallowed by _tick_once's broad
    except and its ledger row orphans, inflating est_spend forever.
    """
    ledger = _RecordingLedger()
    cancel = CancelToken()
    loop = _make_loop(exists=False, ledger=ledger, cancel=cancel)
    loop._tick_once()
    assert ledger.forgotten == ["pod1"]
    assert cancel.is_set()
    assert loop._stop.is_set()


def test_probe_exists_does_not_reap() -> None:
    """probe(exists=True) → no forget, loop keeps running.

    Bug caught: a live pod wrongly forgotten mid-run kills a good session.
    """
    ledger = _RecordingLedger()
    cancel = CancelToken()
    loop = _make_loop(exists=True, ledger=ledger, cancel=cancel)
    loop._tick_once()
    assert ledger.forgotten == []
    assert not cancel.is_set()
    assert not loop._stop.is_set()


def test_no_util_endpoint_no_probe() -> None:
    """util_endpoint=None → no existence probe, no reap.

    Bug caught: adding an unconditional probe would break the tuned
    heartbeat_mode:none path and add a network call every tick.
    """
    ledger = _RecordingLedger()
    cancel = CancelToken()
    loop = HeartbeatLoop(
        instance_id="pod1",
        provider=_FakeProvider(),
        ledger=ledger,
        interval_s=1.0,
        util_endpoint=None,
        cancel_token=cancel,
        provider_kind="runpod",
        stall_window_s=None,
        restart_loop_window_s=None,
    )
    loop._tick_once()
    assert ledger.forgotten == []
    assert not loop._stop.is_set()


class _NotFoundDestroyProvider(_FakeProvider):
    """destroy_instance raises as if the pod is already gone (POD_NOT_FOUND)."""

    def destroy_instance(self, iid: str) -> None:
        raise RuntimeError(f"RunPod GraphQL terminate {iid} failed: POD_NOT_FOUND")


def test_pod_gone_destroy_not_found_logs_calm(
    caplog: Any,
) -> None:
    """POD_GONE whose destroy 404s (pod already gone) logs calm, not a stack.

    Bug caught (2026-07-07 repro): every host-reclaim logs the EXPECTED
    POD_NOT_FOUND at exception level (full traceback), turning the normal
    self-heal path into log noise that reads like a real failure. The reap
    must still forget + stop.
    """
    import logging

    ledger = _RecordingLedger()
    cancel = CancelToken()
    loop = HeartbeatLoop(
        instance_id="pod1",
        provider=_NotFoundDestroyProvider(),
        ledger=ledger,
        interval_s=1.0,
        util_endpoint=_Probe(exists=False),
        cancel_token=cancel,
        provider_kind="runpod",
        stall_window_s=None,
        restart_loop_window_s=None,
    )
    with caplog.at_level(logging.DEBUG):
        loop._tick_once()

    # Reap still completes despite the (expected) destroy failure.
    assert ledger.forgotten == ["pod1"]
    assert loop._stop.is_set()
    # The expected POD_NOT_FOUND must not surface as an ERROR/exception record.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], f"expected calm log, got {[r.getMessage() for r in errors]}"
