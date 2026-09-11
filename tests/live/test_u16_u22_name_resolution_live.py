"""Live: the reserved ephemeral NAME is a real handle, and one idle probe is not (U16, U22).

One RunPod pod discharges four claims that no offline test can reach, because
every one of them is about what the REAL provider does:

* **U16a — the API contract the whole U16 fix hangs on.** Nothing in the tree
  proved RunPod's ``myself { pods }`` actually RETURNS ``name``.
  ``_LIST_PODS_QUERY`` selects it and ``_pod_to_instance`` maps it, but
  ``tests/providers/fixtures/runpod/list_pods.json`` is an EMPTY pod list and no
  fixture in the tree carries a pod name, so every offline test in
  ``test_runpod_name_resolution.py`` encodes that contract in a fake. If RunPod
  omits the field the fix is inert **and silent**. This is the first assertion
  for that reason.
* **U16b — ``probe_runtime(<name>)`` resolves.** Pre-fix the sweeper asked
  ``pod(input:{podId: <name>})``, got ``found=False``, and could not tell a live
  booting pod from a phantom — so GC_404 could delete the only handle on a
  billing pod.
* **U16c — ``destroy_instance(<name>)`` actually terminates.** Pre-fix the
  terminate went out with the name as ``podId``, the confirmation poll asked
  about that same unknown string, got ``data.pod = null``, and reported the pod
  CONFIRMED GONE while it kept billing. A false success, not the failure the
  defect was filed as. Here the reap's own act path performs that destroy, so
  U16c is proven by U22's teardown rather than by a separate step.
* **U22 — the orphan verdict defers past a single idle sample.** The 2026-09-07
  U9 cell reaped on the SECOND tick, 2 s in, off ONE 0.0% reading. With the
  three-sample window it must now stay LIVE through two ticks and reap on the
  third. Watching that deferral happen across real heartbeats against a real
  billing pod is the thing the offline suite cannot do.

**Deliberate shortcut, stated rather than hidden:** the index row is stamped two
hours old so the ``ephemeral_orphan_age_s`` floor is satisfied by construction.
The age gate is offline-proven and waiting it out would bill an hour of GPU to
re-prove arithmetic. What is NOT shortcut is the sample window: the ticks are
real, spaced by a real interval, each carrying a real utilisation reading
fetched from RunPod.

The pod is an alpine container doing nothing, so it reads idle on both GPU and
CPU — which is exactly the orphan shape under test.

Auto-skips when ``RUNPOD_API_KEY`` is unset so default runs stay offline.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.clock import RealClock
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import Policy, Verdict
from kinoforge.core.sweeper import SweeperLoop
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore

# BOTH gates, deliberately. The `live` MARKER is what `pixi run test`
# (`pytest -m 'not live'`) deselects on, and it is the only thing standing
# between this file and a real pod booked by a routine offline test run — which
# is exactly what happened on 2026-09-10 when the first version of this module
# carried the skipif alone: `pixi run test` created pod `xf5jjm6psazv15` and
# billed for it. The skipif keeps an explicitly-requested live run from failing
# confusingly when creds are absent; it does NOT keep the default run out.
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("KINOFORGE_LIVE_TESTS") != "1"
        or not os.environ.get("RUNPOD_API_KEY"),
        reason="live smoke — requires KINOFORGE_LIVE_TESTS=1 and RUNPOD_API_KEY",
    ),
]

#: Hard cap. An alpine pod for a handful of ticks costs cents; anything that
#: would exceed this means the offer search returned something unexpected and
#: the test must refuse to book it rather than discover the price afterwards.
_BUDGET_USD_CAP: float = 0.35
#: Ticks are real and spaced by this. Three ticks is the default window.
_TICK_INTERVAL_S: float = 20.0
_BOOT_TIMEOUT_S: float = 420.0

_EVIDENCE = Path(__file__).parent / "_u16_u22_live_evidence.json"


def _emit(evidence: dict[str, Any]) -> None:
    _EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True))
    print(f"\nevidence → {_EVIDENCE}", file=sys.stderr)


def test_reserved_name_is_a_real_handle_and_one_sample_does_not_reap() -> None:
    """U16a/b/c + U22, on one pod.

    Bug catch, per claim: (a) a RunPod schema that never returned ``name``
    would make ``_pod_ids_matching`` match nothing and U16's fix inert while
    every offline test stayed green; (b) a probe that still 404s the name puts
    a live pod one GC_404 away from losing its only handle; (c) a destroy that
    reports success without terminating leaves a pod billing behind a clean
    exit line; (d) an orphan predicate that acts on tick 1 destroys a pod
    sampled between two compute-light steps, taking the operator's work with
    it.
    """
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.ephemeral import EphemeralSession
    from kinoforge.core.interfaces import InstanceSpec, Placement
    from kinoforge.providers.runpod import RunPodProvider

    creds = EnvCredentialProvider()
    provider = RunPodProvider(creds=creds)
    evidence: dict[str, Any] = {"started_at_local": datetime.now().isoformat()}

    offers = provider.find_offers(
        Placement(min_vram_gb=0, min_cuda="0.0", max_usd_per_hr=10.0, disk_gb=0)
    )
    assert offers, "no RunPod offers available"
    cheapest = min(offers, key=lambda o: o.cost_rate_usd_per_hr)
    est = cheapest.cost_rate_usd_per_hr * ((_BOOT_TIMEOUT_S + 240.0) / 3600.0)
    assert est <= _BUDGET_USD_CAP, (
        f"offer too expensive: {cheapest.cost_rate_usd_per_hr:.4f}/hr → est ${est:.4f}"
    )
    evidence["offer"] = {
        "id": cheapest.id,
        "usd_per_hr": cheapest.cost_rate_usd_per_hr,
        "est_usd": round(est, 4),
    }

    run_id = f"u16-live-{datetime.now():%Y%m%d-%H%M%S}"
    instance_id: str | None = None
    try:
        # The REAL naming seam: under --ephemeral's STRICT_POLICY the pod name
        # is minted controller-side by EphemeralSession.resource_name, and
        # _create_pod asks the active session for it. Reserving it here rather
        # than hand-rolling a `kinoforge-<hex>` string is the point — a fix
        # that only works for strings shaped like the real thing is not a fix.
        with EphemeralSession(enabled=True) as session:
            reserved_name = session.resource_name(run_id, "runpod")
            assert reserved_name.startswith("kinoforge-"), reserved_name
            assert reserved_name != run_id, "STRICT_POLICY leaked the run id"
            evidence["reserved_name"] = reserved_name

            instance = provider.create_instance(
                InstanceSpec(
                    image="mirror.gcr.io/library/alpine:latest",
                    env={},
                    run_id=run_id,
                    # Memory reference_runpod_community_pool_deletions: the
                    # community pool has deleted pods minutes after create,
                    # which would confound every assertion below with an
                    # absence that is not the code's doing.
                    backend_options={"runpod": {"cloud_type": "secure"}},
                )
            )
        instance_id = instance.id
        evidence["runpod_id"] = instance_id
        print(
            f"\ncreated: runpod_id={instance_id!r} name={reserved_name!r}",
            file=sys.stderr,
        )
        assert instance_id != reserved_name, (
            "RunPod returned the name as the id — this provider no longer has "
            "the divergence U16 exists for, and the test proves nothing"
        )

        # ---- U16a: does RunPod return `name` at all? --------------------
        listed = {i.id: i for i in provider.list_instances()}
        assert instance_id in listed, "the pod we just created is not listed"
        evidence["listed_tags"] = dict(listed[instance_id].tags)
        assert listed[instance_id].tags.get("name") == reserved_name, (
            "RunPod's myself{pods} did not return the pod name — "
            "_pod_ids_matching can never match, and U16's fix is INERT. "
            f"tags seen: {listed[instance_id].tags!r}"
        )

        # ---- U16b: the name resolves through probe_runtime ---------------
        # Wait for a NUMERIC reading, not merely for `found`. RunPod answers a
        # booting pod with `runtime = null`: found is True while both readings
        # are None, and the 2026-09-10 run that stopped at `found` fed the
        # sample window nothing but unobservable ticks — which produced a
        # green result whose tick 1 proved nothing (it was LIVE under the old
        # conservative-on-ignorance rule, not the new window) and, on the
        # re-run after U30, a red one. The window under test is about
        # OBSERVED idleness, so the ticks must not start until the provider is
        # actually reporting utilisation.
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        probe = None
        while time.monotonic() < deadline:
            probe = provider.probe_runtime(reserved_name)
            if probe is not None and probe.found and probe.gpu_util_pct is not None:
                break
            time.sleep(5.0)
        assert probe is not None and probe.found, (
            f"probe_runtime({reserved_name!r}) never resolved — the name "
            "fallback did not fire against the real API"
        )
        assert probe.gpu_util_pct is not None and probe.cpu_pct is not None, (
            "the pod never reported a utilisation reading within "
            f"{_BOOT_TIMEOUT_S:.0f}s; the sample window cannot be exercised "
            "with nothing to observe (see U30)"
        )
        evidence["probe_by_name"] = {
            "pod_id": probe.pod_id,
            "found": probe.found,
            "gpu_util_pct": probe.gpu_util_pct,
            "cpu_pct": probe.cpu_pct,
        }
        assert probe.pod_id == reserved_name, (
            "the probe renamed itself to the resolved id; the reaper keys its "
            "snapshot and act path by the row id and would lose the row"
        )

        # ---- U22: three real ticks, keyed by the NAME --------------------
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalArtifactStore(root=Path(tmp))
            index = EphemeralIndex(store=store)
            index.add(
                EphemeralIndexRow(
                    id=reserved_name,
                    warm_attach_key="wak-u16-live",
                    kinoforge_key="k-u16live001",
                    endpoints={"8188": f"https://{instance_id}-8188.proxy.runpod.net"},
                    provider="runpod",
                    # Stated shortcut: age gate satisfied by construction.
                    created_at_local=(datetime.now() - timedelta(hours=2)).isoformat(),
                )
            )
            loop = SweeperLoop(
                store=store,
                ledger=Ledger(store=store, run_id="u16-live-sweeper"),
                registry_get_provider=lambda _name: lambda: provider,
                thresholds={
                    "idle_timeout_s": 2 * 3600.0,
                    "max_lifetime_s": 5 * 3600.0,
                    "heartbeat_interval_s": 30.0,
                    "grace_after_session_s": 1800.0,
                    # STALL_REAP off, so ORPHAN_REAP is the only thing that can
                    # fire — and so the deque's maxlen comes from the orphan
                    # window alone, which is the trap U22's fix had to close.
                    "stall_window_s": None,
                    "stall_gpu_threshold": 5.0,
                    "stall_cpu_threshold": 20.0,
                    "restart_loop_window_s": None,
                    "restart_loop_uptime_threshold_s": 90.0,
                    "ephemeral_orphan_age_s": 60.0,
                    "ephemeral_orphan_samples": 3,
                },
                interval_s=_TICK_INTERVAL_S,
                host=socket.gethostname(),
                policy=Policy(
                    act_verdicts=frozenset({Verdict.ORPHAN_REAP, Verdict.GC_404})
                ),
                clock=RealClock(),
            )

            verdicts: list[str] = []
            readings: list[dict[str, Any]] = []
            for tick in range(3):
                if tick:
                    time.sleep(_TICK_INTERVAL_S)
                # Record what the provider reported AT each tick. Without this
                # the tick sequence is unattributable: a LIVE tick on a null
                # reading is the OLD conservative-on-ignorance rule, not the
                # new window, and the 2026-09-10 run could only be read
                # correctly by reconstructing the readings after the fact.
                at_tick = provider.probe_runtime(reserved_name)
                readings.append(
                    {
                        "found": None if at_tick is None else at_tick.found,
                        "gpu": None if at_tick is None else at_tick.gpu_util_pct,
                        "cpu": None if at_tick is None else at_tick.cpu_pct,
                    }
                )
                loop._tick_once()
                rows = {r.id for r in EphemeralIndex(store=store).rows()}
                verdicts.append("REAPED" if reserved_name not in rows else "LIVE")
                print(
                    f"tick {tick + 1}: {verdicts[-1]} {readings[-1]}",
                    file=sys.stderr,
                )
            evidence["ticks"] = verdicts
            evidence["tick_readings"] = readings

            assert all(r["gpu"] is not None for r in readings), (
                "a tick observed nothing, so its LIVE verdict is the old "
                f"ignorance rule rather than the sample window: {readings}"
            )

            assert verdicts[0] == "LIVE", (
                "reaped on the FIRST sample — U22 is not fixed on the live path"
            )
            assert verdicts[1] == "LIVE", (
                "reaped on the SECOND sample — the window is one short"
            )
            assert verdicts[2] == "REAPED", (
                "three consecutive idle samples did not reap; either the "
                "window never fills (the maxlen trap) or the name never "
                "resolved to a utilisation reading"
            )

        # ---- U16c: the reap's destroy went out by NAME and worked --------
        remaining = {i.id for i in provider.list_instances()}
        evidence["pod_gone_after_reap"] = instance_id not in remaining
        assert instance_id not in remaining, (
            f"the reap reported success but pod {instance_id!r} is still "
            "listed — this is the false-success shape U16 was filed under"
        )
        instance_id = None
    finally:
        if instance_id is not None:
            try:
                provider.destroy_instance(instance_id)
                evidence["fallback_teardown"] = instance_id
            except Exception as exc:  # noqa: BLE001
                evidence["fallback_teardown_failed"] = repr(exc)
        evidence["finished_at_local"] = datetime.now().isoformat()
        _emit(evidence)
