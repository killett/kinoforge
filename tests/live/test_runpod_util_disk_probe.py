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
_BUDGET_USD_CAP = 0.005
_SESSION_LIFETIME_S = 90.0
_SIDECAR_PATH = Path("tests/live/_runpod_util_disk_probe.json")

# Trial selection sets, priority order. Each entry is (label, sub-selection,
# placement) where placement is "container" or "runtime" — controls which
# block the sub-selection is appended to.
_DISK_TRIALS: list[tuple[str, str, str]] = [
    (
        "container.diskInfo.utilPercent",
        "diskInfo { utilPercent }",
        "container",
    ),
    (
        "runtime.disk.utilPercent",
        "disk { utilPercent }",
        "runtime",
    ),
    (
        "container.storage.used+total",
        "storage { used total }",
        "container",
    ),
]


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the disk-util GraphQL probe "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


def _build_query(disk_sub: str, placement: str) -> str:
    container_block = "container { cpuPercent memoryPercent"
    runtime_extra = ""
    if placement == "container":
        container_block += f" {disk_sub}"
    elif placement == "runtime":
        runtime_extra = f"      {disk_sub}\n"
    container_block += " }"
    return (
        "query GetRuntime($podId: String!) {\n"
        "  pod(input: {podId: $podId}) {\n"
        "    id\n"
        "    runtime {\n"
        "      uptimeInSeconds\n"
        "      gpus { id gpuUtilPercent memoryUtilPercent }\n"
        f"      {container_block}\n"
        f"{runtime_extra}"
        "    }\n"
        "  }\n"
        "}"
    )


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

    disk_field: str | None = None
    envelopes: list[dict[str, object]] = []

    try:
        deadline = time.monotonic() + _SESSION_LIFETIME_S
        while time.monotonic() < deadline:
            inst = provider.get_instance(instance_id)
            if inst.status == "ready":
                break
            time.sleep(3.0)
        else:
            pytest.fail(f"pod {instance_id} never ready in {_SESSION_LIFETIME_S}s")

        for label, subsel, placement in _DISK_TRIALS:
            query = _build_query(subsel, placement)
            resp = provider._http_post(  # noqa: SLF001 — wire-level probe
                provider._base_url,
                {"query": query, "variables": {"podId": instance_id}},
            )
            envelopes.append(
                {"label": label, "subsel": subsel, "placement": placement, "resp": resp}
            )
            if "errors" not in resp and resp.get("data", {}).get("pod"):
                disk_field = label
                print(f"WINNER: {label!r}", file=sys.stderr)
                break
            print(
                f"REJECTED: {label!r} → {resp.get('errors')!r}",
                file=sys.stderr,
            )

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
