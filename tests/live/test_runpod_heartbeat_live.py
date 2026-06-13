"""Live RunPod heartbeat substrate smoke (B5a Task f).

Gated by ``KINOFORGE_LIVE_RUNPOD=1`` — refuses to run otherwise. Spends
≤ $0.05 per invocation. Characterizes RunPod GraphQL podEditJob mutation
+ pod query round-trip latency under real network conditions and detects
429 rate-limiting if present.

Test discipline:
- The RED-scaffold + this docstring + the actual test body all commit
  BEFORE the first live invocation per CLAUDE.md Durability rule
  ("Commit RED scaffolds before any live spend").
- Pod teardown is in a try/finally; an exception in the assertion block
  must NOT leak a live pod.
- All HTTP via stdlib urllib through the prod _http_post seam (no test
  injection at the wire layer — this IS the wire test).

Caveats vs plan HEREDOC:
- ``HardwareRequirements`` has fields ``min_vram_gb``, ``min_cuda``,
  ``max_usd_per_hr`` — NOT ``gpu_count``/``cpu_count``/``cuda_min``.
  RunPod's ``find_offers`` returns GPU-pod offers only (the GraphQL
  ``gpuTypes`` endpoint has no CPU-only surface). We use relaxed
  ``min_vram_gb=0`` + ``max_usd_per_hr=3.00`` to capture the single
  cheapest GPU offer and then cap at ≤ $0.05 session spend.
- ``Offer.cost_rate_usd_per_hr`` — not ``cost_per_hour_usd``.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.05
_TICK_COUNT = 2
_TICK_INTERVAL_S = 5.0
_SESSION_LIFETIME_S = 60.0


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    """Refuse to fire without the explicit opt-in env var.

    Belt-and-suspenders: pytest's marker config also gates this, but
    a defensive env-var check inside the fixture guarantees no
    accidental pod spawn from a stray ``pytest -v`` invocation.
    """
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the live RunPod heartbeat smoke "
            f"(~$0.05 spend per invocation)"
        )


def test_runpod_heartbeat_round_trip_against_live_pod() -> None:
    """End-to-end: spawn the cheapest available pod, write+read heartbeat
    2x via GraphQL tags, capture latency, teardown.

    The single live test for B5a. Covers:
    - RUNPOD_API_KEY auth scope sufficient for podEditJob mutation
      (the open question from spec §2)
    - Real network round-trip latency (P50/P99) — feeds spec §9 amend
    - 429 detection within 60s of tick-cadence — feeds spec §11 Risk 2
      mitigation
    - Pod-side tag persistence across ~5s of network IO
    """
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
    from kinoforge.providers.runpod import RunPodProvider
    from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    assert api_key, "RUNPOD_API_KEY must be in environment for live smoke"

    endpoint = RunPodGraphQLHeartbeatEndpoint(api_key=api_key)
    provider = RunPodProvider(
        creds=creds,
        heartbeat_endpoint=endpoint,
    )

    # Find the cheapest available offer.  RunPod's GraphQL surface only
    # returns GPU pod offers (no CPU-only endpoint).  Use very relaxed
    # requirements (min_vram_gb=0, min_cuda="0.0") so nothing gets
    # filtered out, then take the cheapest by cost_rate_usd_per_hr.
    reqs = HardwareRequirements(
        min_vram_gb=0,
        min_cuda="0.0",
        max_usd_per_hr=10.0,  # allow anything; we cap by budget check below
    )
    offers = provider.find_offers(reqs)
    assert offers, "no RunPod offers available"
    cheapest = min(offers, key=lambda o: o.cost_rate_usd_per_hr)
    estimated_spend = cheapest.cost_rate_usd_per_hr * (_SESSION_LIFETIME_S / 3600.0)
    assert estimated_spend <= _BUDGET_USD_CAP, (
        f"cheapest offer too expensive for ≤${_BUDGET_USD_CAP} budget: "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr "
        f"→ {estimated_spend:.5f} USD for {_SESSION_LIFETIME_S}s"
    )
    print(
        f"\nPod offer selected: id={cheapest.id!r} "
        f"cost={cheapest.cost_rate_usd_per_hr:.4f} USD/hr "
        f"vram={cheapest.vram_gb} GB "
        f"estimated_spend={estimated_spend:.5f} USD",
        file=sys.stderr,
    )

    spec = InstanceSpec(
        offer=cheapest,
        image="runpod/base:latest",
        env={},
        provision_script=None,
    )

    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(
        f"Pod created: id={instance_id!r} status={instance.status!r}", file=sys.stderr
    )

    latencies_ms: list[float] = []
    rate_limit_hit = False

    try:
        # Wait for ready (bounded by SESSION_LIFETIME_S).
        deadline = time.monotonic() + _SESSION_LIFETIME_S
        while time.monotonic() < deadline:
            inst = provider.get_instance(instance_id)
            if inst.status == "ready":
                print(f"Pod ready: id={instance_id!r}", file=sys.stderr)
                break
            print(
                f"Waiting for pod ready (status={inst.status!r}) …",
                file=sys.stderr,
            )
            time.sleep(2.0)
        else:
            pytest.fail(
                f"pod {instance_id} did not reach ready within {_SESSION_LIFETIME_S}s"
            )

        # Two heartbeat ticks
        for tick_num in range(_TICK_COUNT):
            ts_before = datetime.now().astimezone()
            t0 = time.monotonic()
            try:
                provider.heartbeat(instance_id)
            except Exception as exc:  # noqa: BLE001
                if "429" in str(exc):
                    rate_limit_hit = True
                    pytest.fail(
                        f"RunPod GraphQL 429 within tick #{tick_num + 1} — "
                        f"rate limit observed: {exc}"
                    )
                raise
            t1 = time.monotonic()
            got_float = provider.last_heartbeat(instance_id)
            t2 = time.monotonic()
            write_ms = (t1 - t0) * 1000.0
            read_ms = (t2 - t1) * 1000.0
            latencies_ms.append(write_ms)
            latencies_ms.append(read_ms)

            print(
                f"Tick {tick_num + 1}/{_TICK_COUNT}: "
                f"write={write_ms:.0f}ms read={read_ms:.0f}ms",
                file=sys.stderr,
            )

            assert got_float is not None, "last_heartbeat returned None after write"
            ts_after = datetime.fromtimestamp(got_float).astimezone()
            delta = abs((ts_after - ts_before).total_seconds())
            assert delta < 1.0, (
                f"tick {tick_num + 1}: write→read mismatch {delta:.3f}s "
                f"(before={ts_before.isoformat()} after={ts_after.isoformat()})"
            )

            if tick_num < _TICK_COUNT - 1:
                time.sleep(_TICK_INTERVAL_S)

    finally:
        # Teardown — destroy pod regardless of test outcome.
        try:
            provider.destroy_instance(instance_id)
            print(f"Pod destroyed: id={instance_id!r}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(
                f"WARN: pod {instance_id!r} teardown raised {exc!r}; check console",
                file=sys.stderr,
            )

    # Capture latency stats to a sidecar JSON used in spec amend Step f.4.
    p50 = statistics.median(latencies_ms) if latencies_ms else 0.0
    p99 = (
        statistics.quantiles(latencies_ms, n=100)[98]
        if len(latencies_ms) >= 100
        else max(latencies_ms, default=0.0)
    )
    sidecar = Path("tests/live/_runpod_heartbeat_smoke_latencies.json")
    sidecar.write_text(
        json.dumps(
            {
                "p50_ms": round(p50, 1),
                "p99_ms": round(p99, 1),
                "samples": [round(x, 1) for x in latencies_ms],
                "rate_limit_hit": rate_limit_hit,
                "spend_cap_usd": _BUDGET_USD_CAP,
                "tick_count": _TICK_COUNT,
                "session_lifetime_s": _SESSION_LIFETIME_S,
                "offer_id": cheapest.id,
                "cost_rate_usd_per_hr": cheapest.cost_rate_usd_per_hr,
                "estimated_spend_usd": round(estimated_spend, 6),
                "instance_id": instance_id,
            },
            indent=2,
        )
    )

    print(
        f"RUNPOD_HEARTBEAT_LATENCY_MS_P50={p50:.0f} P99={p99:.0f} "
        f"rate_limit={'YES' if rate_limit_hit else 'no'}",
        file=sys.stderr,
    )
