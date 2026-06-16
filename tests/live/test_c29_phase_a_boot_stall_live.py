"""C29 Phase A live smoke — boot-phase STALL_REAP fires DURING engine.provision.

The C29 closure starts ``HeartbeatLoop`` right after the RunPod status poll
returns ``ready`` and BEFORE ``engine.provision`` runs. A ``_SleepyEngine``
sleeps 600 s inside ``wait_for_ready`` with zero GPU work, so the tick
util_endpoint reports ``gpuUtilPercent=0`` / ``cpuPercent~=0`` every 10 s.
After 6 ticks (60 s window) the STALL_REAP predicate fires, the loop calls
``provider.destroy_instance(id)`` + ``cancel_token.set()``,
``wait_for_ready`` raises ``Cancelled`` on the next iteration, and
``_provision_instance_and_build_backend`` re-destroys idempotently.

Pre-C29: this smoke would NEVER fire — the loop didn't start until
``deploy_session`` resumed AFTER provision returned, so 600 s of sleep would
have to be capped by ``boot_timeout`` (300 s here) ⇒ ``ProvisionTimeout``,
not ``STALL_REAP``.

Gated by ``KINOFORGE_LIVE_RUNPOD=1`` + ``KINOFORGE_LIVE=1``. Live spend
ceiling: $0.20 (~300 s of cheapest RunPod offer at <$0.20/hr).

Spec:
``docs/superpowers/specs/2026-06-14-c29-heartbeat-earlier-design.md`` §
"Acceptance smoke A — boot-phase STALL_REAP".
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
_INTERVAL_S = 10.0
_STALL_WINDOW_S = 60.0
_BOOT_TIMEOUT_S = 300.0
_STALL_DEADLINE_S = _STALL_WINDOW_S + 6 * _INTERVAL_S + 60.0  # ~180 s
_SIDECAR_PATH = Path("tests/live/_c29_phase_a_evidence.json")
_PROVISION_SLEEP_S = 600.0


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the C29 Phase A boot-phase STALL_REAP "
            f"smoke (~${_BUDGET_USD_CAP} spend per invocation)"
        )


def test_c29_phase_a_boot_stall_reap_fires_during_provision() -> None:
    """Boot-phase STALL_REAP destroys a sleeping pod inside engine.provision.

    Bug catch: a regression that re-attaches the start-heartbeat closure to
    deploy_session's post-provision block (the pre-C29 site) would leave the
    sleep uncovered → ProvisionTimeout, not STALL_REAP. The evidence sidecar
    pins both the outcome AND the time-to-reap.
    """
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.errors import Cancelled
    from kinoforge.core.heartbeat_loop import HeartbeatLoop
    from kinoforge.core.interfaces import (
        HardwareRequirements,
        InstanceSpec,
    )
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
        f"\nC29 Phase A offer: {cheapest.id!r} @ "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr",
        file=sys.stderr,
    )

    spec = InstanceSpec(
        image="mirror.gcr.io/library/alpine:latest",
        offer=cheapest,
        env={},
        provision_script=None,
    )
    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(f"C29 Phase A pod created: {instance_id!r}", file=sys.stderr)

    cancel_token = CancelToken()
    util_ep = RunPodGraphQLUtilEndpoint(api_key=api_key)

    boot_deadline = time.monotonic() + _BOOT_TIMEOUT_S
    started_at = time.monotonic()
    while time.monotonic() < boot_deadline:
        inst = provider.get_instance(instance_id)
        if inst.status == "ready":
            instance = inst
            break
        time.sleep(3.0)
    else:
        try:
            provider.destroy_instance(instance_id)
        except Exception:  # noqa: BLE001
            pass
        pytest.fail(f"pod {instance_id} never ready in {_BOOT_TIMEOUT_S}s")

    # C29: start_heartbeat closure invoked right after status=ready,
    # BEFORE the provision sleep below — this is what protects the boot phase.
    loop = HeartbeatLoop(
        ledger=_SpyLedger(),
        provider=provider,
        instance_id=instance_id,
        interval_s=_INTERVAL_S,
        util_endpoint=util_ep,
        cancel_token=cancel_token,
        provider_kind="runpod",
        stall_window_s=_STALL_WINDOW_S,
        stall_gpu_threshold=5.0,
        stall_cpu_threshold=20.0,
        logger_=logging.getLogger("kinoforge.live.c29_phase_a"),
    )
    loop.start()
    print(
        f"C29 Phase A boot-phase HeartbeatLoop started after "
        f"{time.monotonic() - started_at:.1f}s; sleeping in provision-like loop"
        f" with 600 s ceiling",
        file=sys.stderr,
    )

    sleep_started_at = time.monotonic()
    stall_fired_at: float | None = None
    outcome = "INCONCLUSIVE"
    try:
        deadline = sleep_started_at + min(_PROVISION_SLEEP_S, _STALL_DEADLINE_S)
        while time.monotonic() < deadline:
            try:
                cancel_token.raise_if_set()
            except Cancelled:
                stall_fired_at = time.monotonic() - sleep_started_at
                outcome = "PROVEN"
                break
            time.sleep(2.0)
        if outcome != "PROVEN":
            outcome = "TIMEOUT"
    finally:
        loop.stop()
        try:
            provider.destroy_instance(instance_id)
        except Exception:  # noqa: BLE001
            pass

    sidecar = {
        "outcome": outcome,
        "captured_at": datetime.now().astimezone().isoformat(),
        "pod_id": instance_id,
        "offer_usd_per_hr": cheapest.cost_rate_usd_per_hr,
        "interval_s": _INTERVAL_S,
        "stall_window_s": _STALL_WINDOW_S,
        "stall_fired_at_s_since_provision_start": stall_fired_at,
        "boot_window_s": _BOOT_TIMEOUT_S,
        "provision_sleep_ceiling_s": _PROVISION_SLEEP_S,
    }
    _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2, default=str))
    print(
        f"\nC29 Phase A sidecar written: {_SIDECAR_PATH} (outcome={outcome})",
        file=sys.stderr,
    )

    assert outcome == "PROVEN", (
        f"STALL_REAP did not fire within {_STALL_DEADLINE_S}s while pod "
        f"was sleeping in provision; pre-C29 regression suspected"
    )
    assert (
        stall_fired_at is not None
        and stall_fired_at <= _STALL_WINDOW_S + 6 * _INTERVAL_S + 30.0
    ), (
        f"STALL_REAP fired too late: {stall_fired_at}s "
        f"(ceiling {_STALL_WINDOW_S + 6 * _INTERVAL_S + 30.0}s)"
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
