"""C29 Phase C live smoke — `kinoforge status --id` surfaces liveness during boot.

The C29 closure starts ``HeartbeatLoop`` right after the RunPod status poll
returns ``ready`` and BEFORE engine.provision runs, so the ledger gains
``last_heartbeat`` + tick fields throughout the boot phase. An operator
running ``kinoforge status --id <pod>`` during boot now sees real liveness
data instead of the pre-C29 "no record yet — pod is still booting" blank.

The pod sleeps in a fake provision-script loop for 120 s. After 60 s the test
runs ``kinoforge status --id <pod>`` via subprocess and asserts four markers
in the output:

1. ``id=<pod_id>`` — status lookup found the entry.
2. ``provider=runpod`` — provider tag persisted.
3. ``last_heartbeat=`` — the closure's tick wrote a fresh heartbeat
   (the load-bearing C29 evidence — pre-C29 the field never appeared).
4. ``provider_status=ready`` — provider side agrees with the ledger.

Pre-C29: the ``last_heartbeat`` field would be absent because hb_loop
wouldn't start until engine.provision returned. Operators had no liveness
signal mid-boot.

Gated by ``KINOFORGE_LIVE_RUNPOD=1``. Live spend ceiling: $0.20.

Spec:
``docs/superpowers/specs/2026-06-14-c29-heartbeat-earlier-design.md`` §
"Acceptance smoke C — kinoforge status during boot".
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.20
_INTERVAL_S = 10.0
_BOOT_TIMEOUT_S = 180.0
_PROVISION_SLEEP_S = 120.0
_STATUS_CHECK_AT_S = 60.0
_SIDECAR_PATH = Path("tests/live/_c29_phase_c_evidence.json")


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the C29 Phase C boot-phase status "
            f"smoke (~${_BUDGET_USD_CAP} spend per invocation)"
        )


class _RealLedgerProxy:
    """Forwards touches to the LocalArtifactStore-backed Ledger.

    The smoke needs the touches to land in the on-disk ledger so the
    subprocess ``kinoforge status`` call sees them.
    """

    def __init__(self, store: Any) -> None:
        from kinoforge.core.lifecycle import Ledger

        self._inner = Ledger(store=store)
        self.touches: list[dict[str, Any]] = []

    def record(self, instance: Any, **kw: Any) -> None:  # noqa: D102
        self._inner.record(instance, **kw)

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
        return self._inner.touch(instance_id, last_heartbeat=last_heartbeat, **extra)

    def forget(self, instance_id: str) -> None:
        self._inner.forget(instance_id)


def test_c29_phase_c_status_shows_liveness_during_boot() -> None:
    """`kinoforge status --id <pod>` returns 4 liveness markers during boot.

    Bug catch: a regression that re-attaches the start-heartbeat closure to
    the post-provision site would leave the ledger empty during the 120 s
    sleep, so the subprocess `kinoforge status` call would print
    "no ledger record yet" instead of the expected key/value markers.
    """
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.heartbeat_loop import HeartbeatLoop
    from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
    from kinoforge.providers.runpod import RunPodProvider
    from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint
    from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint
    from kinoforge.stores.local import LocalArtifactStore

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    assert api_key, "RUNPOD_API_KEY must be set"

    provider = RunPodProvider(
        creds=creds,
        heartbeat_endpoint=RunPodGraphQLHeartbeatEndpoint(api_key=api_key),
    )
    reqs = HardwareRequirements(
        min_vram_gb=0, min_cuda="0.0", max_usd_per_hr=10.0, disk_gb=0
    )
    offers = provider.find_offers(reqs)
    assert offers, "no RunPod offers available"
    cheapest = min(offers, key=lambda o: o.cost_rate_usd_per_hr)
    est_spend = cheapest.cost_rate_usd_per_hr * (
        (_BOOT_TIMEOUT_S + _PROVISION_SLEEP_S + 60.0) / 3600.0
    )
    assert est_spend <= _BUDGET_USD_CAP, (
        f"offer too expensive for ≤${_BUDGET_USD_CAP}: "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr → est {est_spend:.4f} USD"
    )
    print(
        f"\nC29 Phase C offer: {cheapest.id!r} @ "
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
    print(f"C29 Phase C pod created: {instance_id!r}", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="kinoforge-c29-phase-c-") as state_dir:
        # State dir layout must match what `kinoforge --state-dir DIR status`
        # expects: LocalArtifactStore(state_dir) — no extra "store" subdir.
        store = LocalArtifactStore(Path(state_dir))
        ledger_proxy = _RealLedgerProxy(store)
        ledger_proxy.record(instance)

        cancel_token = CancelToken()
        util_ep = RunPodGraphQLUtilEndpoint(api_key=api_key)

        started_at = time.monotonic()
        boot_deadline = started_at + _BOOT_TIMEOUT_S
        while time.monotonic() < boot_deadline:
            inst = provider.get_instance(instance_id)
            if inst.status == "ready":
                break
            time.sleep(3.0)
        else:
            try:
                provider.destroy_instance(instance_id)
            except Exception:  # noqa: BLE001
                pass
            pytest.fail(f"pod {instance_id} never ready in {_BOOT_TIMEOUT_S}s")

        # C29: simulate the boot-phase start_heartbeat closure firing right
        # after status=ready, BEFORE the provision sleep below.
        loop = HeartbeatLoop(
            ledger=ledger_proxy,
            provider=provider,
            instance_id=instance_id,
            interval_s=_INTERVAL_S,
            util_endpoint=util_ep,
            cancel_token=cancel_token,
            provider_kind="runpod",
            stall_window_s=None,
            restart_loop_window_s=None,
            logger_=logging.getLogger("kinoforge.live.c29_phase_c"),
        )
        loop.start()

        status_output: str = ""
        markers_found: dict[str, bool] = {}
        try:
            # Sleep mid-provision; the heartbeat ticks throughout.
            time.sleep(_STATUS_CHECK_AT_S)

            # Run `kinoforge status --id <pod>` via subprocess pointed at the
            # same state dir so it reads the ledger the C29 hb_loop just wrote.
            try:
                result = subprocess.run(
                    [
                        "pixi",
                        "run",
                        "kinoforge",
                        "--state-dir",
                        state_dir,
                        "status",
                        "--id",
                        instance_id,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=os.environ.copy(),
                    check=False,
                )
                status_output = result.stdout + result.stderr
            except subprocess.TimeoutExpired:
                status_output = "<subprocess timeout>"

            print(
                f"\n--- kinoforge status output:\n{status_output}\n---",
                file=sys.stderr,
            )

            markers_found = {
                "id_marker": f"id={instance_id}" in status_output,
                "provider_marker": "provider=runpod" in status_output,
                "last_heartbeat_marker": "last_heartbeat=" in status_output,
                "provider_status_marker": "provider_status=" in status_output,
            }
        finally:
            loop.stop()
            try:
                provider.destroy_instance(instance_id)
            except Exception:  # noqa: BLE001
                pass

        sidecar = {
            "outcome": "PROVEN" if all(markers_found.values()) else "TIMEOUT",
            "captured_at": datetime.now().astimezone().isoformat(),
            "pod_id": instance_id,
            "offer_usd_per_hr": cheapest.cost_rate_usd_per_hr,
            "interval_s": _INTERVAL_S,
            "boot_window_s": _BOOT_TIMEOUT_S,
            "provision_sleep_ceiling_s": _PROVISION_SLEEP_S,
            "status_check_at_s_since_status_ready": _STATUS_CHECK_AT_S,
            "markers_found": markers_found,
            "tick_count": len(ledger_proxy.touches),
            "status_output_excerpt": status_output[:2000],
        }
        _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2, default=str))
        print(
            f"\nC29 Phase C sidecar written: {_SIDECAR_PATH}",
            file=sys.stderr,
        )

        assert markers_found["id_marker"], (
            f"`kinoforge status` did not surface id={instance_id} during boot: "
            f"{status_output!r}"
        )
        assert markers_found["provider_marker"], (
            f"`kinoforge status` did not surface provider=runpod: {status_output!r}"
        )
        assert markers_found["last_heartbeat_marker"], (
            f"`kinoforge status` did not surface last_heartbeat= during boot — "
            f"pre-C29 regression suspected: {status_output!r}"
        )
        assert markers_found["provider_status_marker"], (
            f"`kinoforge status` did not surface provider_status=: {status_output!r}"
        )
