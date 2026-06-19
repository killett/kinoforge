"""``HeartbeatLoop`` must record last_heartbeat=now when provider returns None.

Regression context (2026-06-18 Wan 1.3B CLI warm-reuse smoke):
    First retry of the cross-CLI warm-reuse demonstration failed. cmd 2
    cold-created a new pod instead of attaching to cmd 1's pod because
    the cmd 1 ledger row was missing the ``last_heartbeat`` field, and
    ``reaper.classify`` at ``src/kinoforge/core/reaper.py:334`` returns
    ``HEARTBEAT_UNKNOWN`` when that field is ``None``. The cross-CLI
    scan's conservative-on-ignorance gate then forced ``cold create``.

Root cause: ``HeartbeatLoop._tick_once`` (``heartbeat_loop.py:237``)
reads ``last_hb = self._provider.last_heartbeat(self._instance_id)``,
which for RunPod post-C33 returns ``None`` because
``RunPodGraphQLHeartbeatEndpoint.read()`` parses a marker that was
never written (write was permanently disabled in C33-m, ``c2526ac``).
``Ledger.touch`` at ``lifecycle.py:651-652`` then SKIPS writing
``last_heartbeat`` when the value is ``None``, so the row never gets
the field.

Per the B5b deferral spec, the local ledger is meant to be the
same-host substrate. That implies the orchestrator IS the source of
truth for "this pod is being driven". The orchestrator's clock at
tick time is therefore the correct ``last_heartbeat`` value when the
wire-level substrate has nothing to surface — fall back to it.
"""

from __future__ import annotations

import threading
from typing import Any

from kinoforge.core.clock import FakeClock
from kinoforge.core.heartbeat_loop import HeartbeatLoop


class _NullHeartbeatProvider:
    """Provider whose ``last_heartbeat`` always returns ``None``.

    Models the RunPod post-C33 reality: ``provider.heartbeat`` is a
    no-op (write substrate disabled), ``provider.last_heartbeat`` returns
    ``None`` (read substrate has nothing to parse).
    """

    def __init__(self) -> None:
        self.heartbeat_calls: list[str] = []
        self.last_heartbeat_calls: list[str] = []
        self.tick_event = threading.Event()

    def heartbeat(self, instance_id: str) -> None:
        self.heartbeat_calls.append(instance_id)
        self.tick_event.set()

    def last_heartbeat(self, instance_id: str) -> float | None:
        self.last_heartbeat_calls.append(instance_id)
        return None


class _RecordingLedger:
    """Captures every ``touch`` call's kwargs verbatim."""

    def __init__(self) -> None:
        self.touch_calls: list[dict[str, Any]] = []

    def touch(
        self,
        instance_id: str,
        *,
        last_heartbeat: float | None = None,
        **extra: float | int | str | None,
    ) -> bool:
        self.touch_calls.append(
            {
                "instance_id": instance_id,
                "last_heartbeat": last_heartbeat,
                "extra": dict(extra),
            }
        )
        return True


def test_tick_records_clock_now_as_last_heartbeat_when_provider_returns_none() -> None:
    """HeartbeatLoop must fall back to clock.now() when provider returns None."""
    clock = FakeClock(start=1_000_000.0)
    provider = _NullHeartbeatProvider()
    ledger = _RecordingLedger()

    loop = HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="i-fallback",
        interval_s=0.01,
        clock=clock,
    )
    try:
        loop.start()
        assert provider.tick_event.wait(timeout=5.0), (
            "provider.heartbeat never fired; loop may not have started"
        )
    finally:
        loop.stop()

    assert ledger.touch_calls, (
        "ledger.touch never invoked; HeartbeatLoop._tick_once did not call it"
    )
    first_call = ledger.touch_calls[0]
    assert first_call["instance_id"] == "i-fallback"
    assert first_call["last_heartbeat"] is not None, (
        "HeartbeatLoop wrote last_heartbeat=None when provider.last_heartbeat "
        "returned None. Ledger.touch SKIPS writing None, so reaper.classify "
        "will see entry['last_heartbeat'] missing and return HEARTBEAT_UNKNOWN, "
        "defeating cross-CLI warm-reuse. Expected: fall back to clock.now()."
    )
    assert first_call["last_heartbeat"] == 1_000_000.0, (
        f"expected last_heartbeat=clock.now()={1_000_000.0}, got "
        f"{first_call['last_heartbeat']!r}"
    )
