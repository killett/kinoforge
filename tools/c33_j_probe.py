"""C33 follow-up (j): image-only sleep probe.

Spin a single RunPod pod with bare `bash -c 'sleep 120'` dockerArgs against
the same image as Q4 (`runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`).
No kinoforge provision script (no `KINOFORGE_PROVISION_SCRIPT`), no selfterm,
no diagnostic trap. Poll `runtime.uptimeInSeconds` every 5 s for 4 min.

Verdicts
--------
- ``bash_survived``: uptime climbed monotonically to >=110 s without resets.
  → kill is provision-script-triggered (apt / git / pip first-15 s memory or
    something else). Image entrypoint + RunPod-side cleared.
- ``cycling``: uptime reset to 0 (or jumped down by >=10 s) multiple times
  inside the window. → image entrypoint OR RunPod container manager kills
  bash regardless of payload. Need follow-on probe to discriminate.
- ``ambiguous``: pod never reached desiredStatus=RUNNING (capacity miss,
  startup hang).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GRAPHQL_URL = "https://api.runpod.io/graphql"
REST_URL = "https://rest.runpod.io/v1/pods"
UA = "kinoforge/0.1 c33-j-probe"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

POLL_INTERVAL_S = 5.0
OBSERVATION_WINDOW_S = 240.0
SLEEP_SECONDS_IN_DOCKERARGS = 200
MAX_USD_PER_HR = 0.40
HARD_CAP_USD = 1.00

GPU_CANDIDATES: list[str] = [
    "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 3090",
    "NVIDIA GeForce RTX 3080",
    "NVIDIA RTX A4500",
    "NVIDIA GeForce RTX 4090",
]


def _load_env() -> None:
    """Populate ``os.environ`` from /workspace/.env."""
    env_path = Path("/workspace/.env")
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _api_key() -> str:
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        sys.stderr.write("RUNPOD_API_KEY not set\n")
        sys.exit(2)
    return key


def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    url = f"{GRAPHQL_URL}?api_key={urllib.parse.quote(_api_key(), safe='')}"
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode()))


def _list_offers(gpu_type: str) -> list[dict[str, Any]]:
    q = """
    query($input: GpuLowestPriceInput) {
      gpuTypes(input: {id: $gpuType}) {
        id displayName lowestPrice(input: $input) { gpuName uninterruptablePrice }
      }
    }
    """
    # Simpler: use the GpuTypes endpoint via the gpuTypeId only — kinoforge's
    # filter_offers does the heavy lifting; here we just want a price.
    q2 = """
    {
      gpuTypes {
        id displayName memoryInGb secureCloud communityCloud
        lowestPrice(input: {gpuCount: 1}) { gpuName uninterruptablePrice }
      }
    }
    """
    del q
    data = _graphql(q2)
    gpus = data.get("data", {}).get("gpuTypes", []) or []
    out: list[dict[str, Any]] = []
    for g in gpus:
        if g.get("displayName") != gpu_type and g.get("id") != gpu_type:
            continue
        lp = g.get("lowestPrice") or {}
        price = lp.get("uninterruptablePrice")
        if price is None or price > MAX_USD_PER_HR:
            continue
        out.append(
            {
                "gpuTypeId": g["id"],
                "displayName": g.get("displayName"),
                "priceUsdPerHr": price,
            }
        )
    return out


def _create_pod(gpu_type_id: str, docker_args: str, pod_name: str) -> str:
    mutation = (
        "mutation($input: PodFindAndDeployOnDemandInput!) "
        "{ podFindAndDeployOnDemand(input: $input) "
        "{ id desiredStatus imageName } }"
    )
    input_body = {
        "cloudType": "ALL",
        "gpuCount": 1,
        "volumeInGb": 0,
        "containerDiskInGb": 20,
        "minVcpuCount": 2,
        "minMemoryInGb": 8,
        "gpuTypeId": gpu_type_id,
        "name": pod_name,
        "imageName": IMAGE,
        "dockerArgs": docker_args,
        "ports": "",
        "volumeMountPath": "",
        "env": [],
    }
    data = _graphql(mutation, {"input": input_body})
    errors = data.get("errors")
    if errors:
        raise RuntimeError(f"create_pod errors: {errors}")
    pod = data.get("data", {}).get("podFindAndDeployOnDemand") or {}
    pod_id = pod.get("id")
    if not pod_id:
        raise RuntimeError(f"create_pod returned no id: {data}")
    return str(pod_id)


def _get_pod(pod_id: str) -> dict[str, Any] | None:
    q = (
        '{ pod(input: {podId: "' + pod_id + '"}) '
        "{ id desiredStatus lastStartedAt "
        "runtime { uptimeInSeconds "
        "container { cpuPercent memoryPercent } "
        "gpus { id gpuUtilPercent memoryUtilPercent } "
        "pod { uptimeInSeconds } } } }"
    )
    data = _graphql(q)
    pod = data.get("data", {}).get("pod")
    if pod is None:
        return None
    return dict(pod)


def _destroy_pod(pod_id: str) -> bool:
    """REST DELETE with the terminate-scoped key."""
    term_key = os.environ.get("RUNPOD_TERMINATE_KEY") or _api_key()
    req = urllib.request.Request(  # noqa: S310
        f"{REST_URL}/{pod_id}",
        method="DELETE",
        headers={
            "Authorization": f"Bearer {term_key}",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"destroy_pod HTTPError: {e.code} {e.reason}\n")
        return False
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"destroy_pod error: {e}\n")
        return False


def _classify(samples: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (verdict, reasoning) based on uptime trajectory."""
    uptimes = [
        s["uptime_runtime"]
        for s in samples
        if s.get("uptime_runtime") is not None and s["uptime_runtime"] >= 0
    ]
    if not uptimes:
        return "ambiguous", "no positive uptime samples observed"
    max_uptime = max(uptimes)
    # Reset detected when current uptime < previous - 10
    resets = 0
    prev = None
    for u in uptimes:
        if prev is not None and u < prev - 10:
            resets += 1
        prev = u
    if max_uptime >= 110 and resets == 0:
        return (
            "bash_survived",
            f"max_uptime={max_uptime}s reached without resets — kill is provision-script-triggered",
        )
    if resets >= 1:
        return (
            "cycling",
            f"observed {resets} uptime reset(s) within window, max_uptime={max_uptime}s — image entrypoint OR RunPod-side kills bash regardless of payload",
        )
    return (
        "ambiguous",
        f"max_uptime={max_uptime}s, resets={resets} — neither clear survival nor cycling; rerun or extend window",
    )


def _run_probe(
    gpu_type: str,
    sidecar_path: Path,
) -> dict[str, Any]:
    offers = _list_offers(gpu_type)
    if not offers:
        return {
            "gpu_type": gpu_type,
            "status": "no_offers",
        }
    offer = offers[0]
    gpu_type_id = offer["gpuTypeId"]
    price = offer["priceUsdPerHr"]

    run_id = f"c33-j-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    docker_args = (
        f"bash -c 'echo START_T0_$(date -u +%s); "
        f"sleep {SLEEP_SECONDS_IN_DOCKERARGS}; "
        f"echo END_T_$(date -u +%s)'"
    )
    pod_name = f"kinoforge-{run_id}"
    sys.stderr.write(
        f"[probe] creating pod gpu={gpu_type_id} @ ${price}/hr name={pod_name}\n"
    )
    sys.stderr.write(f"[probe] dockerArgs={docker_args!r}\n")
    try:
        pod_id = _create_pod(gpu_type_id, docker_args, pod_name)
    except RuntimeError as e:
        msg = str(e)
        if (
            "no longer any instances available" in msg
            or "resources to deploy" in msg
            or "RECEIVED_NULL_NEVER_ALLOWED" in msg
            or "INTERNAL_SERVER_ERROR" in msg
        ):
            return {
                "gpu_type": gpu_type,
                "status": "capacity_failed",
                "error": msg[:500],
            }
        raise
    sys.stderr.write(f"[probe] pod_id={pod_id}\n")

    t0 = time.time()
    samples: list[dict[str, Any]] = []
    poll_count = 0
    try:
        while time.time() - t0 < OBSERVATION_WINDOW_S:
            poll_count += 1
            now_wall = time.time() - t0
            try:
                pod = _get_pod(pod_id)
            except Exception as e:  # noqa: BLE001
                samples.append(
                    {
                        "poll": poll_count,
                        "wall_s": round(now_wall, 1),
                        "err": str(e)[:200],
                    }
                )
                time.sleep(POLL_INTERVAL_S)
                continue
            runtime = (pod or {}).get("runtime") or {}
            container = runtime.get("container") or {}
            top_pod = runtime.get("pod") or {}
            gpus = runtime.get("gpus") or []
            gpu_util = gpus[0].get("gpuUtilPercent") if gpus else None
            samples.append(
                {
                    "poll": poll_count,
                    "wall_s": round(now_wall, 1),
                    "desiredStatus": (pod or {}).get("desiredStatus"),
                    "lastStartedAt": (pod or {}).get("lastStartedAt"),
                    "uptime_runtime": runtime.get("uptimeInSeconds"),
                    "uptime_runtime_pod": top_pod.get("uptimeInSeconds"),
                    "cpu_percent": container.get("cpuPercent"),
                    "mem_percent": container.get("memoryPercent"),
                    "gpu_util_percent": gpu_util,
                }
            )
            time.sleep(POLL_INTERVAL_S)
    finally:
        sys.stderr.write(f"[probe] destroying pod {pod_id}\n")
        ok = _destroy_pod(pod_id)
        sys.stderr.write(f"[probe] destroy ok={ok}\n")

    verdict, reasoning = _classify(samples)
    result = {
        "probe": "C33 (j) — image-only sleep, no provision script, no selfterm",
        "date_utc": datetime.now(UTC).isoformat(),
        "image": IMAGE,
        "gpu_type": gpu_type,
        "gpu_type_id": gpu_type_id,
        "price_usd_per_hr": price,
        "pod_id": pod_id,
        "pod_name": pod_name,
        "docker_args": docker_args,
        "observation_window_s": OBSERVATION_WINDOW_S,
        "sleep_seconds_in_dockerargs": SLEEP_SECONDS_IN_DOCKERARGS,
        "poll_interval_s": POLL_INTERVAL_S,
        "samples": samples,
        "verdict": verdict,
        "reasoning": reasoning,
    }
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(result, indent=2, default=str))
    sys.stderr.write(
        f"[probe] verdict={verdict} samples={len(samples)} sidecar={sidecar_path}\n"
    )
    return result


def main() -> int:
    """Run the (j) probe across GPU_CANDIDATES until one succeeds."""
    _load_env()
    sidecar = Path("tests/live/_c33_probe_j_evidence.json")
    if sidecar.exists():
        sys.stderr.write(f"[probe] {sidecar} already exists — refusing to overwrite\n")
        return 1
    for gpu_type in GPU_CANDIDATES:
        sys.stderr.write(f"[probe] trying {gpu_type}\n")
        result = _run_probe(gpu_type, sidecar)
        if result.get("status") in ("capacity_failed", "no_offers"):
            sys.stderr.write(
                f"[probe] {gpu_type}: {result.get('status')} — falling through\n"
            )
            continue
        if result.get("verdict") in ("bash_survived", "cycling", "ambiguous"):
            print(
                json.dumps(
                    {"verdict": result["verdict"], "reasoning": result["reasoning"]}
                )
            )
            return 0
    sys.stderr.write("[probe] no GPU candidate succeeded — all capacity-failed\n")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
