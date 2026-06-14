"""C29 Phase B live smoke — boot-phase RESTART_LOOP_REAP fires DURING engine.provision.

The C29 closure starts ``HeartbeatLoop`` right after the RunPod status poll
returns ``ready`` and BEFORE ``engine.provision`` runs. The pod's
``provision_script="sleep 5; exit 1"`` exits non-zero immediately; RunPod's
auto-restart kicks the container back up; ``runtime.uptimeInSeconds`` stays
near 5 every tick. After 4 consecutive low-uptime ticks (60 s window) the
RESTART_LOOP_REAP predicate fires, the loop calls
``provider.destroy_instance(id)`` + ``cancel_token.set()``, and the test
verifies the reap fired within the deadline.

Pre-C29: this smoke would NEVER fire — the loop didn't start until
``deploy_session`` resumed AFTER provision returned, so the restart loop
would have to be capped by ``boot_timeout`` (3 min here) ⇒ ``ProvisionTimeout``,
not ``RESTART_LOOP_REAP``.

Gated by ``KINOFORGE_LIVE_RUNPOD=1``. Live spend ceiling: $0.20.

Spec:
``docs/superpowers/specs/2026-06-14-c29-heartbeat-earlier-design.md`` §
"Acceptance smoke B — boot-phase RESTART_LOOP_REAP".
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
_BUDGET_USD_CAP = 0.20
_INTERVAL_S = 15.0
_RESTART_LOOP_WINDOW_S = 60.0
_UPTIME_THRESHOLD_S = 90.0
_BOOT_TIMEOUT_S = 180.0
_FIRE_DEADLINE_S = _RESTART_LOOP_WINDOW_S + 4 * _INTERVAL_S + 60.0
_SIDECAR_PATH = Path("tests/live/_c29_phase_b_evidence.json")


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the C29 Phase B boot-phase "
            f"RESTART_LOOP_REAP smoke (~${_BUDGET_USD_CAP} spend per invocation)"
        )


class _SpyLedger:
    """Minimal ledger spy — records every touch + forget for the sidecar."""

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
        self.touches.append(
            {
                "ts": datetime.now().astimezone().isoformat(),
                "id": instance_id,
                "last_heartbeat": last_heartbeat,
                **extra,
            }
        )
        return True

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)


@pytest.mark.xfail(
    reason="C29 RED scaffold — flip to expected-pass once live invocation succeeds",
    strict=True,
)
def test_c29_phase_b_boot_restart_loop_reap_fires_during_provision() -> None:
    """Boot-phase RESTART_LOOP_REAP destroys a pod whose container restart-loops.

    Bug catch: a regression that re-attaches the start-heartbeat closure to
    deploy_session's post-provision block (the pre-C29 site) would leave the
    restart-loop uncovered → ProvisionTimeout, not RESTART_LOOP_REAP. The
    evidence sidecar pins outcome + uptime counter trail.
    """
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
        f"\nC29 Phase B offer: {cheapest.id!r} @ "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr",
        file=sys.stderr,
    )

    # provision_script forces a restart loop — sleep then exit 1 means RunPod's
    # auto-restart spins the container back up every ~5 s, so uptime stays
    # well below the 90 s threshold tick over tick.
    spec = InstanceSpec(
        image="alpine:latest",
        offer=cheapest,
        env={},
        provision_script="sleep 5; exit 1",
    )
    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(f"C29 Phase B pod created: {instance_id!r}", file=sys.stderr)

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
        print(
            f"C29 Phase B pod ready after {time.monotonic() - started_at:.1f}s",
            file=sys.stderr,
        )

        # C29: simulate the start_heartbeat closure invoked right after
        # status=ready, BEFORE engine.provision (which would itself sleep
        # waiting for the restart loop to stabilise — but that never happens
        # because the loop keeps restarting).
        loop = HeartbeatLoop(
            ledger=ledger,
            provider=provider,
            instance_id=instance_id,
            interval_s=_INTERVAL_S,
            util_endpoint=util_ep,
            cancel_token=token,
            provider_kind="runpod",
            stall_window_s=None,  # C26 path off — only RESTART_LOOP fires here
            restart_loop_window_s=_RESTART_LOOP_WINDOW_S,
            restart_loop_uptime_threshold_s=_UPTIME_THRESHOLD_S,
            logger_=logging.getLogger("kinoforge.live.c29_phase_b"),
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
        except Exception:  # noqa: BLE001
            pass

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
        "boot_window_s": _BOOT_TIMEOUT_S,
        "fired_at_s_since_status_ready": fired_at,
        "uptime_counter_trail": uptime_counters,
        "uptime_seconds_trail": uptime_readings,
        "tick_count": len(ledger.touches),
        "forgotten": ledger.forgotten,
    }
    _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2, default=str))
    print(
        f"\nC29 Phase B sidecar written: {_SIDECAR_PATH} (outcome={outcome})",
        file=sys.stderr,
    )

    assert outcome == "PROVEN", (
        f"RESTART_LOOP_REAP did not fire within {_FIRE_DEADLINE_S}s while the "
        f"pod was restart-looping in provision; uptime counters: {uptime_counters}; "
        f"uptime readings: {uptime_readings}"
    )
    assert any(
        isinstance(c, int) and c * _INTERVAL_S >= _RESTART_LOOP_WINDOW_S
        for c in uptime_counters
    ), f"no tick crossed the predicate boundary; counters: {uptime_counters}"
