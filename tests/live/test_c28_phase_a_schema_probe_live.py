"""C28 Task 1 (A0) — empirical RunPod input-schema probe.

Verify which optional fields ``PodFindAndDeployOnDemandInput`` accepts.
Branches the rest of Phase A. Cost: $0 (each probe is rejected by GraphQL
validation OR by a runtime "not found" before any pod allocation).

Implementation note: RunPod disables Apollo introspection in production
(``INTROSPECTION_DISABLED`` validation error on ``__type`` queries), so the
plan's introspection approach is not feasible. Instead, we send a minimal
``podFindAndDeployOnDemand`` mutation per candidate field. The GraphQL
parser validates field names before resolver execution:

* Field accepted → response carries a non-validation error (e.g. "Network
  volume not found", "gpuTypeId is required") with the resolver path set.
  ``GRAPHQL_VALIDATION_FAILED`` code is absent.
* Field rejected → response carries ``Field "<name>" is not defined by
  type "PodFindAndDeployOnDemandInput"`` and ``GRAPHQL_VALIDATION_FAILED``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_SIDECAR = Path("tests/live/_c28_runpod_input_schema_probe.json")
_GRAPHQL_URL = "https://api.runpod.io/graphql"
# Bearer header + User-Agent both required (see balance.py module docstring).
_UA = "kinoforge-c28-schema-probe/1.0"
# Each (field, sample_value) is wired verbatim into a minimal mutation so the
# GraphQL parser sees the field reference and emits a validation error iff the
# input type does not declare it.
_PROBES: tuple[tuple[str, str], ...] = (
    ("restartPolicy", '"NEVER"'),
    ("networkVolumeId", '"00000000-0000-0000-0000-000000000000"'),
    ("registryAuthId", '"00000000-0000-0000-0000-000000000000"'),
)


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(f"set {_LIVE_GATE_ENV}=1 to run the C28 A0 schema probe")


def _post_probe(api_key: str, field_name: str, sample_value: str) -> dict[str, Any]:
    """Send a one-field mutation; return parsed JSON (errors-only is fine)."""
    query = (
        "mutation { podFindAndDeployOnDemand(input: { "
        f"{field_name}: {sample_value}"
        " }) { id } }"
    )
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        _GRAPHQL_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": _UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            payload: dict[str, Any] = json.loads(resp.read())
            return payload
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read())
        return payload


def _field_supported(payload: dict[str, Any], field_name: str) -> bool:
    """True iff payload does NOT carry a parser-level rejection of the field."""
    for err in payload.get("errors") or ():
        code = (err.get("extensions") or {}).get("code", "")
        msg = err.get("message", "")
        if code == "GRAPHQL_VALIDATION_FAILED" and field_name in msg:
            return False
    return True


def test_c28_phase_a_schema_probe_live() -> None:
    api_key = os.environ["RUNPOD_API_KEY"]
    raw_per_field: dict[str, dict[str, Any]] = {}
    support: dict[str, bool] = {}
    for field_name, sample_value in _PROBES:
        payload = _post_probe(api_key, field_name, sample_value)
        raw_per_field[field_name] = payload
        support[field_name] = _field_supported(payload, field_name)

    sidecar = {
        "captured_at": datetime.now().astimezone().isoformat(),
        "method": "empirical-mutation-probe",
        "introspection_disabled": True,
        "restart_policy_supported": support["restartPolicy"],
        "network_volume_supported": support["networkVolumeId"],
        "registry_auth_supported": support["registryAuthId"],
        "raw_per_field": raw_per_field,
    }
    _SIDECAR.write_text(json.dumps(sidecar, indent=2) + "\n")
    print(f"\nA0 schema probe → {_SIDECAR}")
    print(
        json.dumps(
            {k: v for k, v in sidecar.items() if k != "raw_per_field"},
            indent=2,
        ),
    )

    # Sanity: every probe must have returned SOMETHING (auth/transport works).
    for field_name in support:
        assert raw_per_field[field_name], (
            f"probe for {field_name} returned empty payload — transport or auth broken"
        )
