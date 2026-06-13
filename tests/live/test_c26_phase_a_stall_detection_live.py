"""C26 Phase A live smoke — FakeEngine intentional stall + STALL_REAP detection.

Spins a cheap RunPod pod (alpine:latest on the cheapest GPU offer) with
a HeartbeatLoop pointed at a RunPodGraphQLUtilEndpoint and a tight stall
window (60 s). Because the pod has no workload running, GPU + CPU stay
near zero and the consecutive-low counter trips STALL_REAP at counter ×
interval ≥ window (here 6 × 10 s = 60 s). The loop self-classifies,
calls provider.destroy_instance, signals the CancelToken, sets its
stop event. The test then verifies the pod is gone from
list_instances() and writes a sidecar with the counter trail.

Gated by KINOFORGE_LIVE_RUNPOD=1. Live spend ceiling: $0.05.
Spec: docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md §11 Phase A.
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

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.05
_INTERVAL_S = 10.0
_STALL_WINDOW_S = 60.0
_BOOT_TIMEOUT_S = 120.0
_STALL_DEADLINE_S = _STALL_WINDOW_S + 3 * _INTERVAL_S + 30.0  # ~120 s
_SIDECAR_PATH = Path("tests/live/_c26_phase_a_smoke_evidence.json")


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the Phase A stall-detection smoke "
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


def test_c26_phase_a_stall_detection_live() -> None:
    """End-to-end: cheap pod + tight window + FakeEngine ⇒ STALL_REAP fires."""
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.heartbeat_loop import HeartbeatLoop
    from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
    from kinoforge.providers.runpod import RunPodProvider
    from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

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
        (_BOOT_TIMEOUT_S + _STALL_DEADLINE_S + 60.0) / 3600.0
    )
    assert est_spend <= _BUDGET_USD_CAP, (
        f"offer too expensive for ≤${_BUDGET_USD_CAP}: "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr → est {est_spend:.4f} USD"
    )
    print(
        f"\nPhase A offer: {cheapest.id!r} @ "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr",
        file=sys.stderr,
    )

    spec = InstanceSpec(
        image="alpine:latest", offer=cheapest, env={}, provision_script=None
    )
    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(f"Phase A pod created: {instance_id!r}", file=sys.stderr)

    ledger = _SpyLedger()
    token = CancelToken()
    util_ep = RunPodGraphQLUtilEndpoint(api_key=api_key)

    started_at = time.monotonic()
    outcome: str = "INCONCLUSIVE"
    stall_fired_at: float | None = None
    try:
        # Wait for the pod to be ready before spinning the loop. RunPod
        # often flips to desiredStatus=ready before runtime{} populates, but
        # the satisfier's R12 branch (runtime=null → None) keeps the counter
        # at 0 until real data lands, so it's safe to start eagerly.
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
            stall_window_s=_STALL_WINDOW_S,
            logger_=logging.getLogger("kinoforge.live.phase_a"),
        )
        loop.start()
        loop_stop_deadline = time.monotonic() + _STALL_DEADLINE_S
        while time.monotonic() < loop_stop_deadline:
            if token.is_set():
                stall_fired_at = time.monotonic() - started_at
                outcome = "PROVEN"
                break
            time.sleep(2.0)
        loop.stop()
        if outcome != "PROVEN":
            outcome = "TIMEOUT"
    finally:
        # Safety-net destroy: cancel_token + HeartbeatLoop should already
        # have destroyed the pod, but a failure path may have skipped that.
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

    counters = [t.get("consecutive_low_util_count") for t in ledger.touches]
    sidecar = {
        "outcome": outcome,
        "captured_at": datetime.now().astimezone().isoformat(),
        "pod_id": instance_id,
        "offer_usd_per_hr": cheapest.cost_rate_usd_per_hr,
        "interval_s": _INTERVAL_S,
        "stall_window_s": _STALL_WINDOW_S,
        "stall_fired_at_s_since_loop_start": stall_fired_at,
        "counter_trail": counters,
        "tick_count": len(ledger.touches),
        "forgotten": ledger.forgotten,
        "touches": ledger.touches,
    }
    _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2, default=str))
    print(
        f"\nSidecar written: {_SIDECAR_PATH} (outcome={outcome})",
        file=sys.stderr,
    )

    assert outcome == "PROVEN", (
        f"STALL_REAP did not fire within {_STALL_DEADLINE_S}s; "
        f"counter trail: {counters}"
    )
    assert (
        stall_fired_at is not None
        and stall_fired_at <= _STALL_WINDOW_S + 2 * _INTERVAL_S + 30.0
    ), (
        f"STALL_REAP fired too late: {stall_fired_at}s "
        f"(ceiling {_STALL_WINDOW_S + 2 * _INTERVAL_S + 30.0}s)"
    )
    assert instance_id in ledger.forgotten, "ledger.forget never invoked"
