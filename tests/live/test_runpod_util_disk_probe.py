"""Live probe: RunPod GraphQL runtime{} disk-util field name (C26 Task 1).

RunPod introspection is disabled (__type returns null) so the disk-util
field name cannot be discovered statically. Tries three documented
candidates in priority order against a real pod; first successful
selection set wins. Writes outcome to tests/live/_runpod_util_disk_probe.json.

Task 3 of the C26 implementation plan reads that sidecar to finalize
the RunPodGraphQLUtilEndpoint's GraphQL query.

Gated by KINOFORGE_LIVE_RUNPOD=1. Live spend ceiling: $0.005.
Spec: docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md §8.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.01
_SESSION_LIFETIME_S = 120.0
_SIDECAR_PATH = Path("tests/live/_runpod_util_disk_probe.json")

# Trial selection sets, priority order. Each entry is (label, full inner
# runtime selection set). First baseline trial verifies runtime{} itself
# is reachable; remaining trials add candidate disk-field selections.
_TRIALS: list[tuple[str, str]] = [
    (
        "baseline.runtime.uptimeInSeconds",
        "uptimeInSeconds",
    ),
    (
        "baseline.runtime.gpus",
        "uptimeInSeconds gpus { id gpuUtilPercent memoryUtilPercent }",
    ),
    (
        "baseline.runtime.container",
        "uptimeInSeconds container { cpuPercent memoryPercent }",
    ),
    (
        "container.diskInfo.utilPercent",
        "uptimeInSeconds container { cpuPercent memoryPercent diskInfo { utilPercent } }",
    ),
    (
        "runtime.disk.utilPercent",
        "uptimeInSeconds disk { utilPercent } container { cpuPercent memoryPercent }",
    ),
    (
        "container.storage.used+total",
        "uptimeInSeconds container { cpuPercent memoryPercent storage { used total } }",
    ),
    (
        "container.diskPercent",
        "uptimeInSeconds container { cpuPercent memoryPercent diskPercent }",
    ),
]


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the disk-util GraphQL probe "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


def _build_query(runtime_inner: str, pod_id: str) -> str:
    return (
        "{\n"
        f'  pod(input: {{ podId: "{pod_id}" }}) {{\n'
        "    id\n"
        f"    runtime {{ {runtime_inner} }}\n"
        "  }\n"
        "}"
    )


def _post_capture_body(
    url: str, body: dict[str, object], api_key: str
) -> tuple[int | None, str | None, dict[str, object] | None]:
    """Wire-level POST that captures HTTP status + response body even on non-2xx."""
    import urllib.error
    import urllib.parse
    import urllib.request

    encoded_key = urllib.parse.quote(api_key, safe="")
    sep = "&" if "?" in url else "?"
    full_url = f"{url}{sep}api_key={encoded_key}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        full_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "kinoforge/0.1 (+disk-probe)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            return resp.status, raw, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            decoded = None
        return exc.code, raw, decoded


def test_runpod_util_disk_field_probe() -> None:
    """Pick cheapest RunPod offer; try each disk-field selection set; record outcome."""
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
    from kinoforge.providers.runpod import RunPodProvider

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    assert api_key, "RUNPOD_API_KEY must be set for live probe"

    provider = RunPodProvider(creds=creds)
    reqs = HardwareRequirements(
        min_vram_gb=0,
        min_cuda="0.0",
        max_usd_per_hr=10.0,
        disk_gb=0,
    )
    offers = provider.find_offers(reqs)
    assert offers, "no RunPod offers available"
    cheapest = min(offers, key=lambda o: o.cost_rate_usd_per_hr)
    est_spend = cheapest.cost_rate_usd_per_hr * (_SESSION_LIFETIME_S / 3600.0)
    assert est_spend <= _BUDGET_USD_CAP, (
        f"offer too expensive for ≤${_BUDGET_USD_CAP}: "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr → {est_spend:.5f} USD"
    )
    print(
        f"\nProbe pod offer: {cheapest.id!r} @ "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr",
        file=sys.stderr,
    )

    spec = InstanceSpec(
        image="alpine:latest",
        offer=cheapest,
        env={},
        provision_script=None,
    )
    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(f"Probe pod created: {instance_id!r}", file=sys.stderr)

    from typing import Any

    disk_field: str | None = None
    envelopes: list[dict[str, Any]] = []

    try:
        deadline = time.monotonic() + _SESSION_LIFETIME_S
        while time.monotonic() < deadline:
            inst = provider.get_instance(instance_id)
            if inst.status == "ready":
                break
            time.sleep(3.0)
        else:
            pytest.fail(f"pod {instance_id} never ready in {_SESSION_LIFETIME_S}s")

        baseline_ok = False
        for label, runtime_inner in _TRIALS:
            query = _build_query(runtime_inner, instance_id)
            status, raw, decoded = _post_capture_body(
                provider._base_url,  # noqa: SLF001 — wire-level probe
                {"query": query},
                api_key,
            )
            envelope = {
                "label": label,
                "runtime_inner": runtime_inner,
                "status": status,
                "body": decoded if decoded is not None else raw,
            }
            envelopes.append(envelope)
            if status == 200 and decoded and "errors" not in decoded:
                data_val = decoded.get("data")
                data_dict: dict[str, Any] = (
                    data_val if isinstance(data_val, dict) else {}
                )
                pod_data_raw = data_dict.get("pod")
                pod_data: dict[str, Any] | None = (
                    pod_data_raw if isinstance(pod_data_raw, dict) else None
                )
                if pod_data:
                    if label.startswith("baseline."):
                        baseline_ok = True
                        print(
                            f"BASELINE OK: {label!r} → "
                            f"runtime={pod_data.get('runtime')!r}",
                            file=sys.stderr,
                        )
                    else:
                        disk_field = label
                        print(
                            f"DISK WINNER: {label!r} → {pod_data!r}",
                            file=sys.stderr,
                        )
                        break
                else:
                    print(
                        f"NO POD: {label!r} → data.pod null",
                        file=sys.stderr,
                    )
            else:
                err = (decoded.get("errors") if decoded else None) or raw
                print(f"REJECTED: {label!r} ({status}) → {err!r}", file=sys.stderr)

        print(f"\nbaseline_runtime_reachable: {baseline_ok}", file=sys.stderr)

    finally:
        provider.destroy_instance(instance_id)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            live = {i.id for i in provider.list_instances()}
            if instance_id not in live:
                break
            time.sleep(2.0)
        else:
            pytest.fail(f"pod {instance_id} not destroyed in 30 s")

    sidecar = {
        "disk_field": disk_field,
        "captured_at": datetime.now().astimezone().isoformat(),
        "tested_pod_id": instance_id,
        "envelopes": envelopes,
    }
    _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2, default=str))
    print(f"\nSidecar written: {_SIDECAR_PATH}", file=sys.stderr)
