"""C33 follow-up (k): provision-phase bisection probe.

Spins one RunPod pod with a hand-crafted bash script that runs the suspect
provision phases (awscli install, apt aria2, git clone ComfyUI, pip install
ComfyUI requirements) one by one, marking each with a touch file in
``/tmp/phases/``. An EXIT trap uploads a diag snapshot to S3 on every
container respawn. The script is base64-encoded into a `PROBE_SCRIPT` env
var so we don't have to fight nested shell quoting in dockerArgs.

Verdict
-------
* The furthest phase reached across cycles tells us which command kills
  bash. If bash always dies inside the pip-install phase, then
  pip-install is the kill trigger.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GRAPHQL_URL = "https://api.runpod.io/graphql"
UA = "kinoforge/0.1 c33-k-probe"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
S3_BUCKET = "kinoforge-pod-diagnostics"

POLL_INTERVAL_S = 6.0
OBSERVATION_WINDOW_S = 360.0

GPU_CANDIDATES: list[str] = [
    "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 3090",
    "NVIDIA RTX A4500",
    "NVIDIA GeForce RTX 3080",
    "NVIDIA GeForce RTX 4090",
]

PHASES = [
    "T_START",
    "T_AWSCLI_BEGIN",
    "T_AWSCLI_DONE",
    "T_APT_BEGIN",
    "T_APT_DONE",
    "T_CLONE_BEGIN",
    "T_CLONE_DONE",
    "T_PIP_BEGIN",
    "T_PIP_DONE",
    "T_SLEEP_BEGIN",
]


def _build_inner_script(s3_prefix: str) -> str:
    """Return the bash script (plain text) that runs phase bisection."""
    return f"""#!/bin/bash
set -uo pipefail

# Pre-install awscli FIRST so the EXIT trap upload always works
pip install -q awscli >/tmp/awscli_preinstall.log 2>&1 || true

mkdir -p /tmp/phases

# Background memlogger
(
  while true; do
    echo "$(date +%s) $(free -m | awk '/Mem:/ {{print $2, $3, $7}}')" >> /tmp/memlog
    ps auxf 2>/dev/null > /tmp/pslog
    sleep 1
  done
) &

# EXIT trap — capture rc + phase list + tails + S3 upload
_diag_capture() {{
  local rc=$1
  {{
    echo "===== rc ====="; echo "$rc"
    echo "===== UTC ====="; date -u +%Y%m%dT%H%M%S.%NZ
    echo "===== phases (touch order, ls -lct rev-chronological) ====="
    ls -lct /tmp/phases 2>/dev/null | head -30
    echo "===== bash log tail ====="
    tail -100 /tmp/boot.log 2>/dev/null || true
    echo "===== memlog tail (last 80s) ====="
    tail -80 /tmp/memlog 2>/dev/null || true
    echo "===== pslog (last snapshot) ====="
    tail -150 /tmp/pslog 2>/dev/null || true
  }} > /tmp/diag.txt
  aws s3 cp /tmp/diag.txt \
    "s3://{S3_BUCKET}/{s3_prefix}/diag-$(date -u +%Y%m%dT%H%M%S-%N).txt" \
    --no-progress --quiet || true
}}
trap '_diag_capture $?' EXIT

exec > >(tee /tmp/boot.log) 2>&1
echo "PHASE_START_T0=$(date +%s.%N)"

# Phase 1 — awscli install (re-run so we record timing)
date +%s.%N > /tmp/phases/T_START
date +%s.%N > /tmp/phases/T_AWSCLI_BEGIN
echo "BEGIN awscli at $(date +%s)"
pip install -q awscli >/dev/null 2>&1 || true
date +%s.%N > /tmp/phases/T_AWSCLI_DONE
echo "DONE awscli at $(date +%s)"

# Phase 2 — apt-get install aria2
date +%s.%N > /tmp/phases/T_APT_BEGIN
echo "BEGIN apt at $(date +%s)"
(apt-get update -qq && apt-get install -y -qq aria2) >/dev/null 2>&1 || true
date +%s.%N > /tmp/phases/T_APT_DONE
echo "DONE apt at $(date +%s)"

# Phase 3 — git clone ComfyUI
date +%s.%N > /tmp/phases/T_CLONE_BEGIN
echo "BEGIN clone at $(date +%s)"
cd /workspace 2>/dev/null || cd /tmp
rm -rf ComfyUI
git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git >/dev/null 2>&1 || true
date +%s.%N > /tmp/phases/T_CLONE_DONE
echo "DONE clone at $(date +%s)"

# Phase 4 — pip install -r ComfyUI/requirements.txt (the suspect)
date +%s.%N > /tmp/phases/T_PIP_BEGIN
echo "BEGIN pip-req at $(date +%s)"
cd ComfyUI && pip install -q -r requirements.txt
date +%s.%N > /tmp/phases/T_PIP_DONE
echo "DONE pip-req at $(date +%s)"

# Phase 5 — sleep keeps bash alive so we observe steady state if it survives
date +%s.%N > /tmp/phases/T_SLEEP_BEGIN
echo "SLEEP-BEGIN at $(date +%s)"
sleep 300
echo "END at $(date +%s)"
"""


def _build_docker_args(s3_prefix: str) -> tuple[str, str]:
    """Return (dockerArgs, base64-encoded inner script)."""
    inner = _build_inner_script(s3_prefix)
    enc = base64.b64encode(inner.encode("utf-8")).decode("ascii")
    # dockerArgs decodes the env var, writes to /tmp/probe.sh, runs it
    docker_args = (
        'bash -c "echo $PROBE_SCRIPT | base64 -d > /tmp/probe.sh '
        '&& chmod +x /tmp/probe.sh && bash /tmp/probe.sh"'
    )
    return docker_args, enc


def _load_env() -> None:
    env_path = Path("/workspace/.env")
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


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


def _aws_env() -> list[dict[str, str]]:
    """Return AWS creds for the in-pod ``aws s3 cp`` call."""
    import boto3

    creds = boto3.Session().get_credentials().get_frozen_credentials()
    out = [
        {"key": "AWS_ACCESS_KEY_ID", "value": creds.access_key},
        {"key": "AWS_SECRET_ACCESS_KEY", "value": creds.secret_key},
        {
            "key": "AWS_DEFAULT_REGION",
            "value": os.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
        },
    ]
    if creds.token:
        out.append({"key": "AWS_SESSION_TOKEN", "value": creds.token})
    return out


def _list_offers(gpu_type: str, max_usd_per_hr: float) -> list[dict[str, Any]]:
    q = (
        "{ gpuTypes { id displayName memoryInGb "
        "lowestPrice(input: {gpuCount: 1}) "
        "{ gpuName uninterruptablePrice } } }"
    )
    data = _graphql(q)
    gpus = data.get("data", {}).get("gpuTypes", []) or []
    out: list[dict[str, Any]] = []
    for g in gpus:
        if g.get("displayName") != gpu_type and g.get("id") != gpu_type:
            continue
        lp = g.get("lowestPrice") or {}
        price = lp.get("uninterruptablePrice")
        if price is None or price > max_usd_per_hr:
            continue
        out.append(
            {
                "gpuTypeId": g["id"],
                "displayName": g.get("displayName"),
                "priceUsdPerHr": price,
            }
        )
    return out


def _create_pod(
    gpu_type_id: str,
    docker_args: str,
    pod_name: str,
    env: list[dict[str, str]],
) -> str:
    mutation = (
        "mutation($input: PodFindAndDeployOnDemandInput!) "
        "{ podFindAndDeployOnDemand(input: $input) { id desiredStatus imageName } }"
    )
    input_body: dict[str, Any] = {
        "cloudType": "ALL",
        "gpuCount": 1,
        "volumeInGb": 0,
        "containerDiskInGb": 30,
        "minVcpuCount": 2,
        "minMemoryInGb": 12,
        "gpuTypeId": gpu_type_id,
        "name": pod_name,
        "imageName": IMAGE,
        "dockerArgs": docker_args,
        "ports": "",
        "volumeMountPath": "",
        "env": env,
    }
    data = _graphql(mutation, {"input": input_body})
    if data.get("errors"):
        raise RuntimeError(f"create_pod errors: {data['errors']}")
    pod = data.get("data", {}).get("podFindAndDeployOnDemand") or {}
    pod_id = pod.get("id")
    if not pod_id:
        raise RuntimeError(f"create_pod returned no id: {data}")
    return str(pod_id)


def _get_pod(pod_id: str) -> dict[str, Any] | None:
    q = (
        f'{{ pod(input: {{ podId: "{pod_id}" }}) '
        "{ id desiredStatus lastStartedAt uptimeSeconds "
        "runtime { uptimeInSeconds "
        "container { cpuPercent memoryPercent } "
        "gpus { id gpuUtilPercent memoryUtilPercent } "
        "} } }"
    )
    data = _graphql(q)
    pod = data.get("data", {}).get("pod")
    if pod is None:
        return None
    return dict(pod)


def _destroy_pod(pod_id: str) -> bool:
    data = _graphql('mutation { podTerminate(input: { podId: "' + pod_id + '" }) }')
    return not data.get("errors")


def _list_s3_objects(prefix: str) -> list[dict[str, Any]]:
    import boto3

    s3 = boto3.client("s3", region_name="us-west-2")
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    return [
        {
            "key": o["Key"],
            "size": o["Size"],
            "last_modified": o["LastModified"].isoformat(),
        }
        for o in resp.get("Contents", [])
    ]


def _fetch_s3_object(key: str) -> str:
    import boto3

    s3 = boto3.client("s3", region_name="us-west-2")
    return str(
        s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"]
        .read()
        .decode("utf-8", "replace")
    )


def _parse_phase_section(diag_text: str) -> list[str]:
    """Extract phase markers from the diag.txt body in recency order."""
    in_section = False
    phases: list[str] = []
    for line in diag_text.splitlines():
        if line.startswith("===== phases"):
            in_section = True
            continue
        if line.startswith("===== ") and in_section:
            break
        if in_section:
            tokens = line.split()
            if tokens and tokens[-1] in PHASES:
                phases.append(tokens[-1])
    return phases


def _parse_rc(diag_text: str) -> str | None:
    in_section = False
    for line in diag_text.splitlines():
        if line.startswith("===== rc"):
            in_section = True
            continue
        if line.startswith("===== ") and in_section:
            break
        if in_section and line.strip():
            return line.strip()
    return None


def _classify(uploads: list[dict[str, Any]]) -> tuple[str, str]:
    if not uploads:
        return (
            "no_uploads",
            "EXIT trap never fired — bash got SIGKILL, OR awscli was not installed in time on first cycle",
        )
    from collections import Counter

    furthest = []
    for u in uploads:
        phases = u.get("phases_in_recency_order") or []
        furthest.append(phases[0] if phases else "?")
    counts = Counter(furthest)
    top, n = counts.most_common(1)[0]
    return (
        "phase_identified",
        f"furthest phase across {len(uploads)} cycles: {dict(counts)} — most common: {top} ({n}/{len(uploads)})",
    )


def main() -> int:
    """Run the (k) phase-bisection probe."""
    _load_env()
    sidecar = Path("tests/live/_c33_probe_k_evidence.json")
    if sidecar.exists():
        sys.stderr.write(f"{sidecar} exists — refusing to overwrite\n")
        return 1

    run_id = f"c33-k-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    s3_prefix = f"boot-logs/{run_id}"
    docker_args, enc_script = _build_docker_args(s3_prefix)
    env = _aws_env() + [{"key": "PROBE_SCRIPT", "value": enc_script}]

    sys.stderr.write(f"[probe] run_id={run_id} s3_prefix={s3_prefix}\n")
    sys.stderr.write(
        f"[probe] dockerArgs len={len(docker_args)} script_b64_len={len(enc_script)}\n"
    )

    pod_id: str | None = None
    used_gpu: dict[str, Any] | None = None
    for gpu_type in GPU_CANDIDATES:
        offers = _list_offers(gpu_type, max_usd_per_hr=0.40)
        if not offers:
            sys.stderr.write(f"[probe] {gpu_type}: no offers\n")
            continue
        offer = offers[0]
        try:
            pod_id = _create_pod(
                offer["gpuTypeId"], docker_args, f"kinoforge-{run_id}", env
            )
            used_gpu = offer
            break
        except RuntimeError as e:
            msg = str(e)
            if "resources to deploy" in msg or "instances available" in msg:
                sys.stderr.write(f"[probe] {gpu_type}: capacity-failed\n")
                continue
            raise

    if pod_id is None:
        sys.stderr.write("[probe] all GPU candidates capacity-failed\n")
        return 3
    sys.stderr.write(f"[probe] pod_id={pod_id} gpu={used_gpu}\n")

    t0 = time.time()
    samples: list[dict[str, Any]] = []
    try:
        while time.time() - t0 < OBSERVATION_WINDOW_S:
            try:
                pod = _get_pod(pod_id)
            except Exception as e:  # noqa: BLE001
                samples.append(
                    {"wall_s": round(time.time() - t0, 1), "err": str(e)[:200]}
                )
                time.sleep(POLL_INTERVAL_S)
                continue
            runtime = (pod or {}).get("runtime") or {}
            container = runtime.get("container") or {}
            gpus = runtime.get("gpus") or []
            samples.append(
                {
                    "wall_s": round(time.time() - t0, 1),
                    "ds": (pod or {}).get("desiredStatus"),
                    "ur": runtime.get("uptimeInSeconds"),
                    "cpu": container.get("cpuPercent"),
                    "mem": container.get("memoryPercent"),
                    "gpu": gpus[0].get("gpuUtilPercent") if gpus else None,
                }
            )
            time.sleep(POLL_INTERVAL_S)
    finally:
        sys.stderr.write(f"[probe] terminating {pod_id}\n")
        ok = _destroy_pod(pod_id)
        sys.stderr.write(f"[probe] destroy ok={ok}\n")

    sys.stderr.write(f"[probe] listing S3 prefix {s3_prefix}\n")
    uploads_meta = _list_s3_objects(s3_prefix)
    uploads_meta.sort(key=lambda u: u["last_modified"])
    sys.stderr.write(f"[probe] found {len(uploads_meta)} S3 uploads\n")

    uploads: list[dict[str, Any]] = []
    for um in uploads_meta:
        body = _fetch_s3_object(um["key"])
        phases = _parse_phase_section(body)
        rc = _parse_rc(body)
        uploads.append(
            {
                "key": um["key"],
                "last_modified": um["last_modified"],
                "size": um["size"],
                "rc": rc,
                "phases_in_recency_order": phases,
                "phases_count": len(phases),
                "body_excerpt": body[:4000],
            }
        )
    verdict, reasoning = _classify(uploads)

    result = {
        "probe": "C33 (k) — provision-phase bisection (awscli / apt / clone / pip)",
        "date_utc": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "s3_prefix": s3_prefix,
        "image": IMAGE,
        "gpu": used_gpu,
        "pod_id": pod_id,
        "observation_window_s": OBSERVATION_WINDOW_S,
        "samples": samples,
        "n_uploads": len(uploads),
        "uploads": uploads,
        "verdict": verdict,
        "reasoning": reasoning,
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({"verdict": verdict, "reasoning": reasoning}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
