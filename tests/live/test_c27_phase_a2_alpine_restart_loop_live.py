"""C27 Phase A2 live smoke — real alpine restart loop ⇒ RESTART_LOOP_REAP.

Spins a cheap RunPod pod (alpine:latest on the cheapest GPU offer) with
``provision_script="sleep 5; exit 1"`` so the container boots, sleeps,
exits non-zero, and RunPod's auto-restart kicks the cycle over again.
Every iteration leaves ``runtime.uptimeInSeconds`` near 5 — well below
the 90 s threshold — so the C27 counter accumulates as fast as the
HeartbeatLoop ticks.

Phase A2 differs from A1 in three places:
  - Real :class:`RunPodGraphQLUtilEndpoint` (no fake) exercises the
    end-to-end wire-shape: ``RUNPOD_API_KEY`` Bearer + the runtime{}
    field on the pod GraphQL query that surfaces uptime.
  - Real alpine restart loop via ``provision_script="sleep 5; exit 1"``
    — proves the predicate is awake even when the production wire
    really IS surfacing low uptime tick over tick.
  - Tighter window (15 s × 6 = 90 s) at a slower cadence so we get
    more raw evidence in the sidecar before reap fires.

Gated by ``KINOFORGE_LIVE_RUNPOD=1``. Live spend ceiling: $0.15.
Spec: docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md §12.3.
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
_BUDGET_USD_CAP = 0.15
_INTERVAL_S = 15.0
_RESTART_LOOP_WINDOW_S = 90.0
_UPTIME_THRESHOLD_S = 90.0
_BOOT_TIMEOUT_S = 180.0
_FIRE_DEADLINE_S = _RESTART_LOOP_WINDOW_S + 4 * _INTERVAL_S + 60.0


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the Phase A2 smoke "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


class _SpyLedger:
    """In-memory ledger spy that records the full tick trail."""

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


def test_c27_phase_a2_alpine_restart_loop_live() -> None:
    """Real alpine restart loop drives the C27 predicate end-to-end."""
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
        (_BOOT_TIMEOUT_S + _FIRE_DEADLINE_S + 60.0) / 3600.0
    )
    assert est_spend <= _BUDGET_USD_CAP, (
        f"offer too expensive for ≤${_BUDGET_USD_CAP}: "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr → est {est_spend:.4f} USD"
    )
    print(
        f"\nPhase A2 offer: {cheapest.id!r} @ "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr",
        file=sys.stderr,
    )

    # provision_script gets base64-encoded into dockerArgs by RunPodProvider
    # (_create_pod) → 'bash -c "echo $SCRIPT | base64 -d > /tmp/p.sh && bash
    # /tmp/p.sh"'. Inside the script we sleep then exit 1, forcing the container
    # to terminate; RunPod's restart policy spins it back up immediately → loop.
    spec = InstanceSpec(
        image="mirror.gcr.io/library/alpine:latest",
        offer=cheapest,
        env={},
        provision_script="sleep 5; exit 1",
    )
    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(f"Phase A2 pod created: {instance_id!r}", file=sys.stderr)

    ledger = _SpyLedger()
    token = CancelToken()
    util_ep = RunPodGraphQLUtilEndpoint(api_key=api_key)

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
            stall_window_s=None,  # C26 path off — only C27 fires.
            restart_loop_window_s=_RESTART_LOOP_WINDOW_S,
            restart_loop_uptime_threshold_s=_UPTIME_THRESHOLD_S,
            logger_=logging.getLogger("kinoforge.live.phase_a2"),
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
    uptime_readings = [t.get("last_uptime_seconds") for t in ledger.touches]
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
        "uptime_seconds_trail": uptime_readings,
        "tick_count": len(ledger.touches),
        "forgotten": ledger.forgotten,
        "touches": ledger.touches,
    }
    sidecar_path = Path("tests/live/_c27_phase_a2_evidence.json")
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str))
    print(
        f"\nSidecar written: {sidecar_path} (outcome={outcome})",
        file=sys.stderr,
    )

    assert outcome == "PROVEN", (
        f"RESTART_LOOP_REAP did not fire within {_FIRE_DEADLINE_S}s; "
        f"uptime counter trail: {uptime_counters}; "
        f"uptime readings: {uptime_readings}"
    )
    # AC: uptime < 90 for >= 8 consecutive ticks. With interval=15 s and
    # window=90 s we need counter >= 6 to fire — the trail of integers
    # captures all observed counter values.
    assert any(
        isinstance(c, int) and c * _INTERVAL_S >= _RESTART_LOOP_WINDOW_S
        for c in uptime_counters
    ), f"no tick crossed the predicate boundary; counters: {uptime_counters}"
    assert instance_id in ledger.forgotten, "ledger.forget never invoked"
