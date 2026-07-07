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
