r"""Diagnostic: measure civitai download throughput from inside a RunPod pod.

Category-2 investigation (smoke-wan21-weekly, 3/3 Monday failures): every
failing step is a /lora/set_stack whose pod-side download of the 350 MB
``sttcrttn.safetensors`` must finish inside hard wall budgets (~100 s RunPod
proxy ceiling for the branch tests; ~700 s incl. inventory-convergence for
the matrix). This tool boots a minimal pod in the SAME pool the smoke uses,
streams the same civitai URL from inside the pod, and reports throughput —
discriminating "civitai throttles/starves RunPod egress" from "harness
budgets are simply too tight".

Usage::

    pixi run python tools/probe_civitai_throughput.py \
        [--ref civitai:1479320@1673265] [--cloud-type ALL] \
        [--gpu "NVIDIA RTX A5000"] [--max-minutes 18]

Cost: ~$0.03-0.08 (A5000 pool, <= max-minutes). Always destroys the pod in
a ``finally`` block via ``kinoforge destroy --id`` and verifies the pod is
gone via the provider list query.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kinoforge.core.credentials import EnvCredentialProvider  # noqa: E402
from kinoforge.core.dotenv_loader import load_env_file  # noqa: E402
from kinoforge.providers.runpod import (  # noqa: E402
    _CREATE_POD_MUTATION,
    _LIST_PODS_QUERY,
    _make_default_http_seams,
)
from kinoforge.sources.civitai import CivitAISource  # noqa: E402

_GRAPHQL_URL = "https://api.runpod.io/graphql"
_UA = "kinoforge-civitai-probe/0.1"

# Pod-side measurement: stream the URL, record byte counts at fixed
# elapsed-time marks, write JSON to /tmp (served on 8001 by http.server).
_POD_SCRIPT = r"""#!/bin/bash
set -uo pipefail
exec > /tmp/bootstrap.log 2>&1
nohup python3 -m http.server 8001 --directory /tmp >/dev/null 2>&1 &
cat > /tmp/probe.py <<'PYEOF'
import json, os, time, urllib.request

url = os.environ["CIV_URL"]
auth = os.environ.get("CIV_AUTH", "")
headers = {"User-Agent": "kinoforge-pod-download/0.1"}
if auth:
    headers["Authorization"] = auth
out = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
try:
    req = urllib.request.Request(url, headers=headers)
    t0 = time.monotonic()
    n = 0
    marks = {}
    with urllib.request.urlopen(req, timeout=120) as r:
        out["http_status"] = r.status
        out["content_length"] = r.headers.get("Content-Length")
        out["final_url_host"] = r.geturl().split("/")[2]
        while True:
            chunk = r.read(8 * 1024 * 1024)
            if not chunk:
                break
            n += len(chunk)
            el = time.monotonic() - t0
            for w in (30, 60, 120, 300, 600):
                if str(w) not in marks and el >= w:
                    marks[str(w)] = n
            if el > 630:
                out["truncated_at_s"] = 630
                break
    el = time.monotonic() - t0
    out["bytes"] = n
    out["secs"] = round(el, 1)
    out["mb_per_s"] = round(n / el / 1e6, 3) if el > 0 else None
    out["window_bytes"] = marks
    out["complete"] = "truncated_at_s" not in out
except Exception as e:  # noqa: BLE001
    out["error"] = repr(e)[:500]
with open("/tmp/probe.json.tmp", "w") as f:
    json.dump(out, f)
os.replace("/tmp/probe.json.tmp", "/tmp/probe.json")
PYEOF
python3 /tmp/probe.py
echo "[probe] done"
sleep 3600
"""


def _http_get_text(url: str, timeout: int = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def main() -> int:
    """Create probe pod, wait for measurement JSON, print it, destroy pod."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="civitai:1479320@1673265")
    parser.add_argument(
        "--cloud-type", default="ALL", choices=["ALL", "SECURE", "COMMUNITY"]
    )
    parser.add_argument("--gpu", default="NVIDIA RTX A5000")
    parser.add_argument("--max-minutes", type=float, default=18.0)
    args = parser.parse_args()

    load_env_file()
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key:
        print("RUNPOD_API_KEY not set", file=sys.stderr)
        return 2

    arts = CivitAISource().resolve(args.ref, EnvCredentialProvider())
    pick = next((a for a in arts if a.filename.endswith(".safetensors")), arts[0])
    auth = (pick.headers or {}).get("Authorization", "")
    print(
        f"[probe] ref={args.ref} filename={pick.filename} "
        f"size={pick.size} auth_header={'SET len=' + str(len(auth)) if auth else 'ABSENT'}"
    )

    http_post, _ = _make_default_http_seams(api_key)
    encoded_script = base64.b64encode(_POD_SCRIPT.encode()).decode("ascii")
    create_input = {
        "cloudType": args.cloud_type,
        "gpuCount": 1,
        "volumeInGb": 0,
        "containerDiskInGb": 15,
        "minVcpuCount": 2,
        "minMemoryInGb": 8,
        "gpuTypeId": args.gpu,
        "name": "kinoforge-civitai-probe",
        "imageName": "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        "dockerArgs": (
            'bash -c "echo $PROBE_SCRIPT | base64 -d > /tmp/pr.sh && bash /tmp/pr.sh"'
        ),
        "ports": "8001/http",
        "volumeMountPath": "/workspace",
        "env": [
            {"key": "PROBE_SCRIPT", "value": encoded_script},
            {"key": "CIV_URL", "value": pick.url},
            {"key": "CIV_AUTH", "value": auth},
        ],
    }
    resp = http_post(
        _GRAPHQL_URL,
        {"query": _CREATE_POD_MUTATION, "variables": {"input": create_input}},
    )
    if resp.get("errors"):
        print(f"[probe] create failed: {resp['errors']}", file=sys.stderr)
        return 1
    pod_id = resp["data"]["podFindAndDeployOnDemand"]["id"]
    print(f"[probe] pod created: {pod_id} (cloudType={args.cloud_type})")

    deadline = time.monotonic() + args.max_minutes * 60
    result: dict[str, Any] | None = None
    try:
        probe_url = f"https://{pod_id}-8001.proxy.runpod.net/probe.json"
        boot_url = f"https://{pod_id}-8001.proxy.runpod.net/bootstrap.log"
        while time.monotonic() < deadline:
            time.sleep(15)
            status, body = _http_get_text(probe_url)
            elapsed = int(args.max_minutes * 60 - (deadline - time.monotonic()))
            if status == 200 and body:
                result = json.loads(body)
                break
            bstatus, _ = _http_get_text(boot_url)
            print(
                f"[probe] t+{elapsed}s: probe.json={status or 'unreachable'} bootstrap.log={bstatus or 'unreachable'}"
            )
        if result is None:
            print(
                "[probe] TIMED OUT waiting for probe.json — pod boot or measurement hung"
            )
            return 1
        print("[probe] RESULT:")
        print(json.dumps(result, indent=2))
        return 0
    finally:
        print(f"[probe] destroying pod {pod_id}")
        destroy = subprocess.run(  # noqa: S603
            ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],  # noqa: S607
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        print(f"[probe] destroy exit={destroy.returncode}")
        listing = http_post(_GRAPHQL_URL, {"query": _LIST_PODS_QUERY})
        alive = [p["id"] for p in listing["data"]["myself"]["pods"]]
        if pod_id in alive:
            print(f"[probe] WARNING: pod {pod_id} still listed — destroy manually!")
        else:
            print(f"[probe] pod {pod_id} confirmed gone")


if __name__ == "__main__":
    sys.exit(main())
