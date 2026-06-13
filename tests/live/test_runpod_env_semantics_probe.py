"""Live probe: RunPod podEditJob env-array merge semantics (C25 Task 1).

Disambiguates whether RunPod's podEditJob mutation MERGES a single-key
env array into the pod's existing env map (Branch A path) or REPLACES
the whole env (Branch B path) OR does not surface env on the read side
(Branch B path).

Writes the outcome to ``tests/live/_runpod_env_semantics.json``. Task 2
of the C25 implementation plan reads that sidecar to pick the
RunPodGraphQLHeartbeatEndpoint wire shape.

Gated by ``KINOFORGE_LIVE_RUNPOD=1``. Live spend ceiling: $0.05.
Spec: docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md §8.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.05
_SESSION_LIFETIME_S = 120.0
_SIDECAR_PATH = Path("tests/live/_runpod_env_semantics.json")

# Probe env vars stamped on pod creation.
_PROBE_KEEP_A = ("PROBE_KEEP_A", "keep-a")
_PROBE_KEEP_B = ("PROBE_KEEP_B", "keep-b")
# Probe env var written by the podEditJob under test.
_PROBE_NEW = ("PROBE_NEW", "new-value")


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the env-semantics probe "
            f"(~$0.05 spend per invocation)"
        )


def test_runpod_env_array_merge_semantics() -> None:
    """Determine env-array semantics; write sidecar; destroy pod."""
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
    from kinoforge.providers.runpod import RunPodProvider

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    assert api_key, "RUNPOD_API_KEY must be in environment for live probe"

    provider = RunPodProvider(creds=creds)

    # Pick the cheapest offer; the probe is GraphQL-only — no GPU needed.
    reqs = HardwareRequirements(
        min_vram_gb=0,
        min_cuda="0.0",
        max_usd_per_hr=10.0,
    )
    offers = provider.find_offers(reqs)
    assert offers, "no RunPod offers available"
    cheapest = min(offers, key=lambda o: o.cost_rate_usd_per_hr)
    estimated_spend = cheapest.cost_rate_usd_per_hr * (_SESSION_LIFETIME_S / 3600.0)
    assert estimated_spend <= _BUDGET_USD_CAP, (
        f"cheapest offer too expensive for ≤${_BUDGET_USD_CAP} budget: "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr → "
        f"{estimated_spend:.5f} USD for {_SESSION_LIFETIME_S}s"
    )
    print(
        f"\nPod offer selected: id={cheapest.id!r} "
        f"cost={cheapest.cost_rate_usd_per_hr:.4f} USD/hr",
        file=sys.stderr,
    )

    spec = InstanceSpec(
        offer=cheapest,
        image="alpine:latest",
        env={_PROBE_KEEP_A[0]: _PROBE_KEEP_A[1], _PROBE_KEEP_B[0]: _PROBE_KEEP_B[1]},
        provision_script=None,
    )
    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(f"Pod created: id={instance_id!r}", file=sys.stderr)

    semantics = "unknown"
    envelope: dict[str, Any] = {}

    try:
        # Wait for ready.
        deadline = time.monotonic() + _SESSION_LIFETIME_S
        while time.monotonic() < deadline:
            inst = provider.get_instance(instance_id)
            if inst.status == "ready":
                break
            time.sleep(3.0)
        else:
            pytest.fail(
                f"pod {instance_id} did not reach ready in {_SESSION_LIFETIME_S}s"
            )

        # Issue podEditJob with a single-key env array.
        mutation = """
        mutation PodEditJob($input: PodEditJobInput!) {
          podEditJob(input: $input) { id }
        }
        """.strip()
        edit_resp = provider._http_post(  # noqa: SLF001 — wire-level probe needs the seam
            provider._base_url,
            {
                "query": mutation,
                "variables": {
                    "input": {
                        "podId": instance_id,
                        "env": [{"key": _PROBE_NEW[0], "value": _PROBE_NEW[1]}],
                    }
                },
            },
        )
        envelope["edit_resp"] = edit_resp
        if "errors" in edit_resp:
            pytest.fail(f"podEditJob env probe failed: {edit_resp['errors']!r}")

        # Query the pod env back. RunPod's GraphQL pod schema may not
        # surface env at all — in which case the read either 400s (field
        # rejected) or returns data.pod.env == null. Both map to
        # ``read-unavailable`` which selects Task 2 Branch B.
        query = """
        query GetPod($podId: String!) {
          pod(input: {podId: $podId}) {
            id
            env { key value }
          }
        }
        """.strip()
        try:
            read_resp = provider._http_post(  # noqa: SLF001
                provider._base_url,
                {"query": query, "variables": {"podId": instance_id}},
            )
            envelope["read_resp"] = read_resp
        except urllib.error.HTTPError as exc:
            envelope["read_http_error"] = {
                "code": exc.code,
                "reason": exc.reason,
                "body": exc.read().decode("utf-8", errors="replace")[:2000],
            }
            semantics = "read-unavailable"
            read_resp = {}

        if semantics != "read-unavailable":
            # Determine semantics from the read body.
            pod = (read_resp.get("data") or {}).get("pod") or {}
            env_field = pod.get("env")
            if env_field is None:
                # Pod query does not surface env at all — Branch B fallback.
                semantics = "read-unavailable"
            else:
                keys = {e.get("key") for e in env_field}
                has_a = _PROBE_KEEP_A[0] in keys
                has_b = _PROBE_KEEP_B[0] in keys
                has_new = _PROBE_NEW[0] in keys
                if has_a and has_b and has_new:
                    semantics = "additive"
                elif has_new and not (has_a or has_b):
                    semantics = "replace"
                else:
                    pytest.fail(
                        f"unexpected env state: has_a={has_a} has_b={has_b} "
                        f"has_new={has_new}; envelope={envelope!r}"
                    )
    finally:
        try:
            provider.destroy_instance(instance_id)
            print(f"Pod destroyed: id={instance_id!r}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(
                f"WARN: pod {instance_id!r} teardown raised {exc!r}; check console",
                file=sys.stderr,
            )

    sidecar = {
        "semantics": semantics,
        "captured_at": datetime.now().astimezone().isoformat(),
        "tested_pod_id": instance_id,
        "envelope": envelope,
    }
    _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2))
    print(
        f"RUNPOD_ENV_SEMANTICS={semantics} sidecar={_SIDECAR_PATH}",
        file=sys.stderr,
    )

    assert semantics in {"additive", "replace", "read-unavailable"}, (
        f"unexpected semantics value: {semantics!r}"
    )
