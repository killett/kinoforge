# mypy: ignore-errors
# ruff: noqa: S310, D103, D205, E702, B905, ANN001, ANN201
"""Hourly sweep — probe both RunPod uptime fields across varied GPU types.

Runs ``n_iterations`` 5-min probes on a cycling list of GPU types,
spread across community and SECURE cloud, one iteration per hour.
Each iteration:
  - Picks the next-in-list GPU that has live capacity (lowestPrice
    non-null AND a successful podFindAndDeployOnDemand)
  - Creates a stock ubuntu:22.04 pod, polls every 30s for 5 minutes
  - Captures (status, runtime.uptimeInSeconds, Pod.uptimeSeconds,
    lastStartedAt, wall-clock estimate) per sample
  - Destroys the pod via podTerminate
  - Appends a structured record to ``_uptime_field_sweep_log.jsonl``
  - Sleeps until the next hour boundary, then repeats

Designed to run unattended via ``nohup`` for 16+ hours. No external
deps (stdlib only). Loads ``RUNPOD_API_KEY`` from environment.

Exit code 0 on normal completion; 2 if env missing; 1 on fatal error.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

API = "https://api.runpod.io/graphql"
LOG = Path(__file__).resolve().parent / "_uptime_field_sweep_log.jsonl"
ERR_LOG = Path(__file__).resolve().parent / "_uptime_field_sweep_errors.log"

# Ordered preference list for GPU candidates. Mix of community-cloud
# (cloud_type=ALL, will pick community when secure unavailable) and
# explicit SECURE candidates. Each entry: (gpu_type_id, cloud_type,
# max_cents_per_hr_cap). Cap caps the per-iteration spend.
GPU_CYCLE: list[tuple[str, str, int]] = [
    ("NVIDIA RTX A5000", "COMMUNITY", 25),
    ("NVIDIA RTX A4000", "SECURE", 30),
    ("NVIDIA GeForce RTX 3080", "COMMUNITY", 25),
    ("NVIDIA RTX 4000 Ada Generation", "SECURE", 30),
    ("NVIDIA RTX A4500", "COMMUNITY", 25),
    ("NVIDIA L4", "SECURE", 45),
    ("Tesla V100-SXM2-16GB", "COMMUNITY", 30),
    ("NVIDIA A40", "SECURE", 50),
    ("NVIDIA GeForce RTX 3090", "COMMUNITY", 30),
    ("NVIDIA RTX A6000", "SECURE", 55),
    ("NVIDIA GeForce RTX 4070 Ti", "COMMUNITY", 25),
    ("NVIDIA RTX PRO 4500 Blackwell", "SECURE", 80),
]


def _api_key() -> str:
    k = os.environ.get("RUNPOD_API_KEY")
    if not k:
        sys.stderr.write("error: RUNPOD_API_KEY not set. Export it first.\n")
        sys.exit(2)
    return k


HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {_api_key()}",
    "User-Agent": "uptime-field-sweep/0.1",
}


def gql(query: str, variables: dict | None = None) -> dict:
    payload = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode(),
        headers=HEADERS,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"http_error": e.code, "body": body}


def query_lowest_prices(secure_cloud: bool) -> dict[str, float]:
    """Return {gpu_id: cents_per_hr} for GPUs with a non-null listing today."""
    flag = "true" if secure_cloud else "false"
    r = gql(
        f"{{ gpuTypes {{ id lowestPrice(input: {{ gpuCount: 1, "
        f"secureCloud: {flag} }}) {{ uninterruptablePrice }} }} }}"
    )
    out: dict[str, float] = {}
    for g in (r.get("data") or {}).get("gpuTypes") or []:
        p = (g.get("lowestPrice") or {}).get("uninterruptablePrice")
        if p is not None:
            out[g["id"]] = float(p) * 100  # cents
    return out


def _pick_gpu(iteration: int) -> tuple[str, str, int] | None:
    """Cycle through GPU_CYCLE; skip ones with no live capacity or over cap."""
    secure_prices = query_lowest_prices(secure_cloud=True)
    community_prices = query_lowest_prices(secure_cloud=False)
    # Start at the iteration-indexed position, then walk forward.
    n = len(GPU_CYCLE)
    for offset in range(n):
        gid, ct, cap = GPU_CYCLE[(iteration + offset) % n]
        prices = secure_prices if ct == "SECURE" else community_prices
        if gid not in prices:
            continue
        cents = int(round(prices[gid]))
        if cents > cap:
            continue
        return gid, ct, cents
    return None


def _create_pod(gpu_id: str, cloud_type: str, name: str) -> tuple[str | None, dict]:
    r = gql(
        """
mutation Create($input: PodFindAndDeployOnDemandInput!) {
  podFindAndDeployOnDemand(input: $input) { id }
}""",
        {
            "input": {
                "cloudType": cloud_type if cloud_type != "COMMUNITY" else "ALL",
                "gpuCount": 1,
                "containerDiskInGb": 5,
                "minVcpuCount": 1,
                "minMemoryInGb": 1,
                "gpuTypeId": gpu_id,
                "name": name,
                "imageName": "mirror.gcr.io/library/ubuntu:22.04",
                "dockerArgs": 'bash -c "sleep 600"',
            }
        },
    )
    if "errors" in r:
        return None, r
    pod_id = ((r.get("data") or {}).get("podFindAndDeployOnDemand") or {}).get("id")
    return pod_id, r


def _poll_once(pod_id: str) -> dict:
    return gql(
        f"""query {{ pod(input: {{ podId: "{pod_id}" }}) {{
  desiredStatus lastStartedAt uptimeSeconds
  runtime {{ uptimeInSeconds }}
}} }}"""
    )


def _destroy(pod_id: str) -> dict:
    return gql(f'mutation {{ podTerminate(input: {{ podId: "{pod_id}" }}) }}')


def _parse_lsa_utc(iso_z: str | None) -> datetime | None:
    if not iso_z:
        return None
    s = iso_z[:-1] + "+00:00" if iso_z.endswith("Z") else iso_z
    try:
        return datetime.fromisoformat(s).astimezone(UTC)
    except ValueError:
        return None


def run_one_iteration(iteration: int) -> dict:
    """Run a single 5-min probe and return a structured record."""
    started_at = datetime.now().astimezone().isoformat()
    pick = _pick_gpu(iteration)
    if pick is None:
        return {
            "iteration": iteration,
            "started_at": started_at,
            "ended_at": datetime.now().astimezone().isoformat(),
            "error": "no GPU with live capacity within cap",
        }
    gpu_id, cloud_type, cents_per_hr = pick
    name = f"uptime-sweep-{iteration:02d}-{int(time.time())}"
    pod_id, create_resp = _create_pod(gpu_id, cloud_type, name)
    if pod_id is None:
        return {
            "iteration": iteration,
            "started_at": started_at,
            "ended_at": datetime.now().astimezone().isoformat(),
            "gpu_id": gpu_id,
            "cloud_type": cloud_type,
            "cents_per_hr": cents_per_hr,
            "error": "create failed",
            "create_response": create_resp,
        }

    samples: list[dict] = []
    create_t = time.time()
    try:
        for i in range(11):  # 5 min @ 30s = 11 samples (0..300)
            time.sleep(30)
            now_utc = datetime.now(UTC)
            r = _poll_once(pod_id)
            p = (r.get("data") or {}).get("pod") or {}
            nested = (p.get("runtime") or {}).get("uptimeInSeconds")
            top = p.get("uptimeSeconds")
            lsa = p.get("lastStartedAt")
            status = p.get("desiredStatus")
            lsa_dt = _parse_lsa_utc(lsa)
            est = (now_utc - lsa_dt).total_seconds() if lsa_dt else None
            samples.append(
                {
                    "t": i * 30,
                    "status": status,
                    "runtime.uptimeInSeconds": nested,
                    "Pod.uptimeSeconds": top,
                    "lastStartedAt": lsa,
                    "now_utc": now_utc.isoformat(),
                    "est_uptime_s_from_lsa": est,
                }
            )
    finally:
        _destroy(pod_id)
    elapsed = time.time() - create_t
    spend = (cents_per_hr / 100.0) * (elapsed / 3600.0)

    nested_vals = [s["runtime.uptimeInSeconds"] for s in samples]
    top_vals = [s["Pod.uptimeSeconds"] for s in samples]
    est_vals = [s["est_uptime_s_from_lsa"] for s in samples]

    def _has_negative(xs: list) -> bool:
        return any(v is not None and v < 0 for v in xs)

    def _has_null(xs: list) -> bool:
        return any(v is None for v in xs)

    def _always_zero(xs: list) -> bool:
        return all(v == 0 for v in xs)

    def _monotonic(xs: list) -> bool:
        non_null = [v for v in xs if v is not None]
        return all(b >= a for a, b in zip(non_null, non_null[1:]))

    def _max_disagreement(field_vals: list, ests: list) -> float | None:
        diffs = [
            abs(float(v) - float(e))
            for v, e in zip(field_vals, ests)
            if v is not None and e is not None
        ]
        return max(diffs) if diffs else None

    summary = {
        "all_running": all(s["status"] == "RUNNING" for s in samples),
        "lsa_stable": len({s["lastStartedAt"] for s in samples if s["lastStartedAt"]})
        <= 1,
        "runtime_field_any_null": _has_null(nested_vals),
        "runtime_field_any_negative": _has_negative(nested_vals),
        "runtime_field_always_zero": _always_zero(nested_vals),
        "runtime_field_monotonic": _monotonic(nested_vals),
        "runtime_field_max_disagreement_s": _max_disagreement(nested_vals, est_vals),
        "top_field_any_null": _has_null(top_vals),
        "top_field_any_negative": _has_negative(top_vals),
        "top_field_always_zero": _always_zero(top_vals),
        "top_field_monotonic": _monotonic(top_vals),
        "top_field_max_disagreement_s": _max_disagreement(top_vals, est_vals),
    }
    return {
        "iteration": iteration,
        "started_at": started_at,
        "ended_at": datetime.now().astimezone().isoformat(),
        "gpu_id": gpu_id,
        "cloud_type": cloud_type,
        "cents_per_hr": cents_per_hr,
        "pod_id": pod_id,
        "image": "mirror.gcr.io/library/ubuntu:22.04",
        "elapsed_s": elapsed,
        "est_spend_usd": round(spend, 6),
        "n_samples": len(samples),
        "samples": samples,
        "summary": summary,
        "error": None,
    }


def _append_log(record: dict) -> None:
    with LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _append_err(msg: str) -> None:
    with ERR_LOG.open("a") as f:
        f.write(f"{datetime.now().astimezone().isoformat()}  {msg}\n")


def main() -> int:
    n_iter = int(os.environ.get("SWEEP_ITERATIONS", "16"))
    sleep_s = int(os.environ.get("SWEEP_SLEEP_S", "3600"))
    start = int(os.environ.get("SWEEP_START_ITERATION", "0"))
    for i in range(start, n_iter):
        try:
            rec = run_one_iteration(i)
            _append_log(rec)
        except Exception as e:  # noqa: BLE001
            _append_err(f"iter {i} fatal: {type(e).__name__}: {e}")
        if i < n_iter - 1:
            time.sleep(sleep_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
