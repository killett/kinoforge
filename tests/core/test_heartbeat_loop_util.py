"""C26 Task 8: HeartbeatLoop util integration tests."""

from __future__ import annotations

from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.core.clock import Clock
from kinoforge.core.heartbeat_loop import HeartbeatLoop
from kinoforge.core.util_endpoints import UtilSnapshot
from kinoforge.providers.local.util import LocalUtilEndpoint


class _FakeClock(Clock):
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def tick(self, dt: float) -> None:
        self._t += dt


class _SpyProvider:
    def __init__(self, *, last_hb: float = 0.0) -> None:
        self.hbs: list[str] = []
        self.last_hb_val = last_hb
        self.destroyed: list[str] = []

    def heartbeat(self, instance_id: str) -> None:
        self.hbs.append(instance_id)

    def last_heartbeat(self, instance_id: str) -> float | None:
        return self.last_hb_val

    def destroy_instance(self, instance_id: str) -> None:
        self.destroyed.append(instance_id)


class _SpyLedger:
    def __init__(self) -> None:
        self.touches: list[dict[str, Any]] = []
        self.forgotten: list[str] = []

    def touch(
        self,
        instance_id: str,
        *,
        last_heartbeat: float | None = None,
        **extra: Any,
    ) -> bool:
        rec = {"id": instance_id, "last_heartbeat": last_heartbeat, **extra}
        self.touches.append(rec)
        return True

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)


def _snap(
    *,
    gpu: float | None = 0.0,
    cpu: float | None = 0.0,
    mem: float | None = 30.0,
    uptime: int | None = 100,
) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=gpu,
        cpu_percent=cpu,
        memory_percent=mem,
        disk_percent=None,
        uptime_seconds=uptime,
    )


def _build_loop(
    *,
    ledger: _SpyLedger,
    provider: _SpyProvider,
    util_endpoint: LocalUtilEndpoint | None,
    stall_window_s: float | None = None,
    cancel_token: CancelToken | None = None,
    clock: Clock | None = None,
) -> HeartbeatLoop:
    return HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="p1",
        interval_s=30.0,
        clock=clock or _FakeClock(),
        util_endpoint=util_endpoint,
        cancel_token=cancel_token,
        provider_kind="runpod",
        stall_window_s=stall_window_s,
    )


def test_loop_without_util_endpoint_preserves_b5a_persist_shape() -> None:
    ledger = _SpyLedger()
    provider = _SpyProvider(last_hb=42.0)
    loop = _build_loop(ledger=ledger, provider=provider, util_endpoint=None)
    loop._tick_once()  # noqa: SLF001 — direct invocation for unit-level test
    assert provider.hbs == ["p1"]
    rec = ledger.touches[0]
    assert rec["last_heartbeat"] == 42.0
    assert "heartbeat_thread_tick" in rec
    # No util fields present.
    assert "consecutive_low_util_count" not in rec
    assert "last_gpu_util_percent" not in rec


def test_loop_with_util_endpoint_persists_seven_fields_per_tick() -> None:
    ledger = _SpyLedger()
    provider = _SpyProvider(last_hb=42.0)
    ep = LocalUtilEndpoint(script=[_snap(gpu=1.0, cpu=10.0, mem=30.0, uptime=100)])
    loop = _build_loop(ledger=ledger, provider=provider, util_endpoint=ep)
    loop._tick_once()  # noqa: SLF001
    rec = ledger.touches[0]
    for field in (
        "heartbeat_thread_tick",
        "util_thread_tick",
        "consecutive_low_util_count",
        "last_gpu_util_percent",
        "last_cpu_percent",
        "last_memory_percent",
        "last_uptime_seconds",
    ):
        assert field in rec, field
    assert rec["consecutive_low_util_count"] == 1


def test_loop_counter_increments_then_resets_on_high_util() -> None:
    ledger = _SpyLedger()
    provider = _SpyProvider()
    ep = LocalUtilEndpoint(
        script=[
            _snap(gpu=1.0, cpu=1.0, uptime=100),
            _snap(gpu=1.0, cpu=1.0, uptime=200),
            _snap(gpu=80.0, cpu=1.0, uptime=300),
        ]
    )
    loop = _build_loop(ledger=ledger, provider=provider, util_endpoint=ep)
    for _ in range(3):
        loop._tick_once()  # noqa: SLF001
    counters = [t["consecutive_low_util_count"] for t in ledger.touches]
    assert counters == [1, 2, 0]


def test_loop_counter_resets_on_uptime_decrease() -> None:
    ledger = _SpyLedger()
    provider = _SpyProvider()
    ep = LocalUtilEndpoint(
        script=[
            _snap(gpu=1.0, cpu=1.0, uptime=200),
            _snap(gpu=1.0, cpu=1.0, uptime=5),  # restart
        ]
    )
    loop = _build_loop(ledger=ledger, provider=provider, util_endpoint=ep)
    loop._tick_once()  # noqa: SLF001
    loop._tick_once()  # noqa: SLF001
    counters = [t["consecutive_low_util_count"] for t in ledger.touches]
    assert counters == [1, 0]


def test_loop_tolerates_util_transport_error_preserving_counter() -> None:
    from kinoforge.core.errors import TransportError

    ledger = _SpyLedger()
    provider = _SpyProvider()

    class _ExplodingEndpoint:
        def __init__(self) -> None:
            self.calls = 0

        def read_util(self, instance_id: str) -> UtilSnapshot | None:
            self.calls += 1
            if self.calls == 2:
                raise TransportError("simulated rate limit")
            return _snap(gpu=1.0, cpu=1.0, uptime=100 + self.calls)

    ep = _ExplodingEndpoint()
    loop = _build_loop(ledger=ledger, provider=provider, util_endpoint=ep)  # type: ignore[arg-type]
    loop._tick_once()  # noqa: SLF001 — counter goes to 1
    loop._tick_once()  # noqa: SLF001 — transport error, counter preserved at 1
    loop._tick_once()  # noqa: SLF001 — counter goes to 2
    counters = [t["consecutive_low_util_count"] for t in ledger.touches]
    assert counters == [1, 1, 2]


def test_loop_fires_stall_reap_when_counter_exceeds_window() -> None:
    """Window 60 s + interval 30 s = need counter ≥ 2 to fire."""
    ledger = _SpyLedger()
    provider = _SpyProvider()
    token = CancelToken()
    ep = LocalUtilEndpoint(
        script=[_snap(gpu=1.0, cpu=1.0, uptime=100 + i) for i in range(5)]
    )
    loop = _build_loop(
        ledger=ledger,
        provider=provider,
        util_endpoint=ep,
        stall_window_s=60.0,
        cancel_token=token,
    )
    loop._tick_once()  # noqa: SLF001 — counter=1, 30s < 60s, no fire
    assert provider.destroyed == []
    loop._tick_once()  # noqa: SLF001 — counter=2, 60s ≥ 60s, fires
    assert provider.destroyed == ["p1"]
    assert "p1" in ledger.forgotten
    assert token.is_set()


def test_loop_does_not_fire_stall_reap_when_window_none() -> None:
    """Kill switch: stall_window_s=None → never fires even with high counter."""
    ledger = _SpyLedger()
    provider = _SpyProvider()
    token = CancelToken()
    ep = LocalUtilEndpoint(
        script=[_snap(gpu=1.0, cpu=1.0, uptime=100 + i) for i in range(5)]
    )
    loop = _build_loop(
        ledger=ledger,
        provider=provider,
        util_endpoint=ep,
        stall_window_s=None,
        cancel_token=token,
    )
    for _ in range(5):
        loop._tick_once()  # noqa: SLF001
    assert provider.destroyed == []
    assert not token.is_set()


def test_loop_does_not_fire_stall_reap_when_util_high() -> None:
    """High GPU breaks the AND-clause → counter stays 0 → no STALL."""
    ledger = _SpyLedger()
    provider = _SpyProvider()
    token = CancelToken()
    ep = LocalUtilEndpoint(
        script=[_snap(gpu=90.0, cpu=1.0, uptime=100 + i) for i in range(5)]
    )
    loop = _build_loop(
        ledger=ledger,
        provider=provider,
        util_endpoint=ep,
        stall_window_s=10.0,
        cancel_token=token,
    )
    for _ in range(5):
        loop._tick_once()  # noqa: SLF001
    assert provider.destroyed == []
    assert not token.is_set()


def test_loop_stop_is_set_after_stall_reap_fires() -> None:
    """The thread is signalled to stop so the outer session can clean up."""
    ledger = _SpyLedger()
    provider = _SpyProvider()
    ep = LocalUtilEndpoint(
        script=[_snap(gpu=1.0, cpu=1.0, uptime=100 + i) for i in range(5)]
    )
    loop = _build_loop(
        ledger=ledger,
        provider=provider,
        util_endpoint=ep,
        stall_window_s=30.0,  # window = interval, fires on counter=1
    )
    loop._tick_once()  # noqa: SLF001
    assert loop._stop.is_set()  # noqa: SLF001 — internal stop event signalled


# ---------------------------------------------------------------------------
# C27 Task 7 — uptime counter persistence + state
# ---------------------------------------------------------------------------


def test_c27_loop_persists_uptime_counter_on_tick() -> None:
    """C27: ledger.touch receives consecutive_low_uptime_count each tick."""
    ledger = _SpyLedger()
    provider = _SpyProvider()
    ep = LocalUtilEndpoint(script=[_snap(gpu=0.0, cpu=13.0, uptime=1)])
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="p1",
        interval_s=30.0,
        clock=_FakeClock(),
        util_endpoint=ep,
        provider_kind="runpod",
        restart_loop_uptime_threshold_s=90.0,
        # restart_loop_window_s left None — Task 7 does not fire; Task 8 wires it.
    )
    loop._tick_once()  # noqa: SLF001
    rec = ledger.touches[0]
    assert "consecutive_low_uptime_count" in rec
    assert rec["consecutive_low_uptime_count"] == 1


def test_c27_loop_uptime_counter_increments_across_ticks() -> None:
    """C27: counter accumulates while uptime stays below threshold."""
    ledger = _SpyLedger()
    provider = _SpyProvider()
    ep = LocalUtilEndpoint(
        script=[_snap(gpu=0.0, cpu=13.0, uptime=1) for _ in range(5)]
    )
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="p1",
        interval_s=30.0,
        clock=_FakeClock(),
        util_endpoint=ep,
        provider_kind="runpod",
        restart_loop_uptime_threshold_s=90.0,
    )
    for _ in range(5):
        loop._tick_once()  # noqa: SLF001
    uptime_counters = [t["consecutive_low_uptime_count"] for t in ledger.touches]
    assert uptime_counters == [1, 2, 3, 4, 5]


def test_c27_loop_uptime_counter_resets_when_uptime_above_threshold() -> None:
    """C27: warm pod with uptime above threshold keeps counter at 0."""
    ledger = _SpyLedger()
    provider = _SpyProvider()
    ep = LocalUtilEndpoint(
        script=[_snap(gpu=0.0, cpu=13.0, uptime=200) for _ in range(3)]
    )
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="p1",
        interval_s=30.0,
        clock=_FakeClock(),
        util_endpoint=ep,
        provider_kind="runpod",
        restart_loop_uptime_threshold_s=90.0,
    )
    for _ in range(3):
        loop._tick_once()  # noqa: SLF001
    uptime_counters = [t["consecutive_low_uptime_count"] for t in ledger.touches]
    assert uptime_counters == [0, 0, 0]


def test_c27_loop_without_util_endpoint_omits_uptime_counter_field() -> None:
    """Backward-compat: no util endpoint → ledger touch lacks the new field."""
    ledger = _SpyLedger()
    provider = _SpyProvider(last_hb=42.0)
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="p1",
        interval_s=30.0,
        clock=_FakeClock(),
        util_endpoint=None,
        provider_kind="runpod",
    )
    loop._tick_once()  # noqa: SLF001
    rec = ledger.touches[0]
    assert "consecutive_low_uptime_count" not in rec


# ---------------------------------------------------------------------------
# C27 Task 8 — _maybe_fire_reap rename + both-routes wiring
# ---------------------------------------------------------------------------


def test_c27_loop_fires_restart_loop_reap_when_only_restart_loop_fires(
    caplog: Any,
) -> None:
    """Uptime stays low; util is high → only RESTART_LOOP_REAP fires."""
    import logging

    ledger = _SpyLedger()
    provider = _SpyProvider()
    token = CancelToken()
    # High GPU breaks the stall AND-clause; low uptime triggers C27.
    ep = LocalUtilEndpoint(
        script=[_snap(gpu=99.0, cpu=99.0, uptime=1) for _ in range(3)]
    )
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="p1",
        interval_s=30.0,
        clock=_FakeClock(),
        util_endpoint=ep,
        cancel_token=token,
        provider_kind="runpod",
        stall_window_s=600.0,  # window large → stall path inert
        restart_loop_window_s=60.0,  # 30*2 = 60 → fires at counter=2
        restart_loop_uptime_threshold_s=90.0,
    )
    caplog.set_level(logging.WARNING)
    loop._tick_once()  # noqa: SLF001 — uptime_counter=1, 30 < 60 → no fire
    assert provider.destroyed == []
    loop._tick_once()  # noqa: SLF001 — uptime_counter=2, 60 >= 60 → fires
    assert provider.destroyed == ["p1"]
    assert "p1" in ledger.forgotten
    assert token.is_set()
    assert loop._stop.is_set()  # noqa: SLF001
    assert any("RESTART_LOOP_REAP" in r.message for r in caplog.records)


def test_c27_loop_stall_reap_wins_tiebreaker_when_both_predicates_fire(
    caplog: Any,
) -> None:
    """Both axes low → STALL checked first → STALL_REAP wins logged + fired."""
    import logging

    ledger = _SpyLedger()
    provider = _SpyProvider()
    token = CancelToken()
    # Low util + low uptime → both predicates fire at counter=2.
    ep = LocalUtilEndpoint(script=[_snap(gpu=0.0, cpu=1.0, uptime=1) for _ in range(3)])
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="p1",
        interval_s=30.0,
        clock=_FakeClock(),
        util_endpoint=ep,
        cancel_token=token,
        provider_kind="runpod",
        stall_window_s=60.0,
        restart_loop_window_s=60.0,
        restart_loop_uptime_threshold_s=90.0,
    )
    caplog.set_level(logging.WARNING)
    loop._tick_once()  # noqa: SLF001 — counters=1,1, both 30 < 60 → no fire
    loop._tick_once()  # noqa: SLF001 — counters=2,2 → STALL wins
    assert provider.destroyed == ["p1"]
    assert any("STALL_REAP" in r.message for r in caplog.records)
    assert not any("RESTART_LOOP_REAP" in r.message for r in caplog.records)


def test_c27_loop_no_fire_when_both_kill_switches_none() -> None:
    """Both windows None → loop is reap-inert even with both counters high."""
    ledger = _SpyLedger()
    provider = _SpyProvider()
    token = CancelToken()
    ep = LocalUtilEndpoint(script=[_snap(gpu=0.0, cpu=1.0, uptime=1) for _ in range(5)])
    loop = HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="p1",
        interval_s=30.0,
        clock=_FakeClock(),
        util_endpoint=ep,
        cancel_token=token,
        provider_kind="runpod",
        stall_window_s=None,
        restart_loop_window_s=None,
    )
    for _ in range(5):
        loop._tick_once()  # noqa: SLF001
    assert provider.destroyed == []
    assert not token.is_set()
