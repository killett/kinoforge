"""C27 Phase A1 live smoke — FakeUtilEndpoint forcing uptime=1 ⇒ RESTART_LOOP_REAP.

Spins a cheap RunPod pod (alpine:latest on the cheapest GPU offer) with
a HeartbeatLoop pointed at a :class:`FakeUtilEndpoint` that hands back a
fixed ``uptime_seconds=1`` snapshot every tick. The C27 counter ticks
[1, 2, 3, 4, 5, 6, ...] until ``counter × interval_s >= restart_loop_
window_s`` (here 6 × 10 s = 60 s), at which point the loop self-
classifies, calls ``provider.destroy_instance``, signals the
``CancelToken``, and sets its stop event.

Differs from the C26 Phase A smoke in three places:
  - Util endpoint is :class:`FakeUtilEndpoint` (no network) — the real
    RunPodGraphQLUtilEndpoint surfaces uptime that grows, defeating the
    restart-loop predicate. A1 isolates the wiring; A2 exercises the
    real wire.
  - ``stall_window_s=None`` kill-switches the C26 path so only C27 fires.
  - ``restart_loop_window_s=60.0`` + ``restart_loop_uptime_threshold_s
    =90.0`` arm the C27 predicate.

Gated by ``KINOFORGE_LIVE_RUNPOD=1``. Live spend ceiling: $0.05.
Spec: docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md §12.2.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.util_endpoints import UtilSnapshot
from tests.live._c27_fake_util_endpoint import FakeUtilEndpoint

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.05
_INTERVAL_S = 10.0
_RESTART_LOOP_WINDOW_S = 60.0
_UPTIME_THRESHOLD_S = 90.0
_BOOT_TIMEOUT_S = 120.0
# Fire by counter=6 → 60 s into the loop; allow 3 grace ticks + 30 s slack.
_FIRE_DEADLINE_S = _RESTART_LOOP_WINDOW_S + 3 * _INTERVAL_S + 30.0


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the Phase A1 smoke "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


class _SpyLedger:
    """In-memory ledger spy that records the counter trail per tick."""

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
        rec = {
            "ts": datetime.now().astimezone().isoformat(),
            "id": instance_id,
            "last_heartbeat": last_heartbeat,
            **extra,
        }
        self.touches.append(rec)
        return True

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)


def test_c27_phase_a1_uptime_streak_live() -> None:
    """FakeUtilEndpoint(uptime=1) drives RESTART_LOOP_REAP end-to-end."""
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.heartbeat_loop import HeartbeatLoop
    from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
    from kinoforge.providers.runpod import RunPodProvider

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    assert api_key, "RUNPOD_API_KEY must be set"

    provider = RunPodProvider(creds=creds)
    reqs = HardwareRequirements(
        min_vram_gb=0, min_cuda="0.0", max_usd_per_hr=10.0, disk_gb=0
    )
    offers = provider.find_offers(reqs)
    assert offers, "no RunPod offers available"
    cheapest = min(offers, key=lambda o: o.cost_rate_usd_per_hr)
    est_spend = cheapest.cost_rate_usd_per_hr * (
        (_BOOT_TIMEOUT_S + _FIRE_DEADLINE_S + 60.0) / 3600.0
    )
    assert est_spend <= _BUDGET_USD_CAP, (
        f"offer too expensive for ≤${_BUDGET_USD_CAP}: "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr → est {est_spend:.4f} USD"
    )
    print(
        f"\nPhase A1 offer: {cheapest.id!r} @ "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr",
        file=sys.stderr,
    )

    spec = InstanceSpec(
        image="alpine:latest", offer=cheapest, env={}, provision_script=None
    )
    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(f"Phase A1 pod created: {instance_id!r}", file=sys.stderr)

    ledger = _SpyLedger()
    token = CancelToken()
    fake_snap = UtilSnapshot(
        gpu_util_percent=0.0,
        cpu_percent=13.0,
        memory_percent=20.0,
        disk_percent=None,
        uptime_seconds=1,
    )
    util_ep = FakeUtilEndpoint(snap=fake_snap)

    started_at = time.monotonic()
    outcome: str = "INCONCLUSIVE"
    fired_at: float | None = None
    try:
        boot_deadline = time.monotonic() + _BOOT_TIMEOUT_S
        while time.monotonic() < boot_deadline:
            inst = provider.get_instance(instance_id)
            if inst.status == "ready":
                break
            time.sleep(3.0)
        else:
            pytest.fail(f"pod {instance_id} never ready in {_BOOT_TIMEOUT_S}s")
        print(f"Pod ready after {time.monotonic() - started_at:.1f}s", file=sys.stderr)

        loop = HeartbeatLoop(
            ledger=ledger,
            provider=provider,
            instance_id=instance_id,
            interval_s=_INTERVAL_S,
            util_endpoint=util_ep,
            cancel_token=token,
            provider_kind="runpod",
            stall_window_s=None,  # C26 path killed — only C27 fires.
            restart_loop_window_s=_RESTART_LOOP_WINDOW_S,
            restart_loop_uptime_threshold_s=_UPTIME_THRESHOLD_S,
            logger_=logging.getLogger("kinoforge.live.phase_a1"),
        )
        loop.start()
        loop_stop_deadline = time.monotonic() + _FIRE_DEADLINE_S
        while time.monotonic() < loop_stop_deadline:
            if token.is_set():
                fired_at = time.monotonic() - started_at
                outcome = "PROVEN"
                break
            time.sleep(2.0)
        loop.stop()
        if outcome != "PROVEN":
            outcome = "TIMEOUT"
    finally:
        try:
            provider.destroy_instance(instance_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        cleanup_deadline = time.monotonic() + 60.0
        while time.monotonic() < cleanup_deadline:
            live = {i.id for i in provider.list_instances()}
            if instance_id not in live:
                break
            time.sleep(2.0)
        else:
            pytest.fail(f"pod {instance_id} not destroyed within 60 s of stop")

    uptime_counters = [t.get("consecutive_low_uptime_count") for t in ledger.touches]
    sidecar = {
        "outcome": outcome,
        "captured_at": datetime.now().astimezone().isoformat(),
        "pod_id": instance_id,
        "offer_usd_per_hr": cheapest.cost_rate_usd_per_hr,
        "interval_s": _INTERVAL_S,
        "restart_loop_window_s": _RESTART_LOOP_WINDOW_S,
        "restart_loop_uptime_threshold_s": _UPTIME_THRESHOLD_S,
        "fired_at_s_since_loop_start": fired_at,
        "uptime_counter_trail": uptime_counters,
        "tick_count": len(ledger.touches),
        "forgotten": ledger.forgotten,
        "touches": ledger.touches,
    }
    sidecar_path = Path("tests/live/_c27_phase_a1_evidence.json")
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str))
    print(
        f"\nSidecar written: {sidecar_path} (outcome={outcome})",
        file=sys.stderr,
    )

    assert outcome == "PROVEN", (
        f"RESTART_LOOP_REAP did not fire within {_FIRE_DEADLINE_S}s; "
        f"uptime counter trail: {uptime_counters}"
    )
    # Counter trail must include at least one tick at counter >= 6 (6*10 = 60).
    assert any(
        isinstance(c, int) and c * _INTERVAL_S >= _RESTART_LOOP_WINDOW_S
        for c in uptime_counters
    ), f"no tick crossed the predicate boundary; counters: {uptime_counters}"
    assert instance_id in ledger.forgotten, "ledger.forget never invoked"
