# mypy: ignore-errors
# ruff: noqa: S310, D103, D205, E702, B905, ANN001, ANN201
"""Minimal repro of RunPod GraphQL uptime field returning 0/negative
on a demonstrably-running pod.

Requires RUNPOD_API_KEY in env. Spend ~$0.01 on the cheapest available
GPU. Destroys the pod on exit.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime

API = "https://api.runpod.io/graphql"
_API_KEY = os.environ.get("RUNPOD_API_KEY")
if not _API_KEY:
    sys.stderr.write(
        "error: RUNPOD_API_KEY not set. Export it first:\n"
        "    export RUNPOD_API_KEY=<your_api_key>\n"
    )
    sys.exit(2)
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {_API_KEY}",
    "User-Agent": "runpod-uptime-bug-repro/0.1",
}


def gql(q):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": q}).encode(),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


# Pick the cheapest currently-available GPU.
gpus = gql(
    "{ gpuTypes { id lowestPrice(input: { gpuCount: 1 }) { uninterruptablePrice } } }"
)
avail = sorted(
    (g["lowestPrice"]["uninterruptablePrice"], g["id"])
    for g in gpus["data"]["gpuTypes"]
    if g.get("lowestPrice", {}).get("uninterruptablePrice")
)
_, gpu_id = avail[0]

# Create the simplest possible pod. Use a GraphQL variable for dockerArgs
# so escaping is the JSON library's problem, not f-string's.
create_req = urllib.request.Request(
    API,
    data=json.dumps(
        {
            "query": """
mutation Create($input: PodFindAndDeployOnDemandInput!) {
  podFindAndDeployOnDemand(input: $input) { id }
}""",
            "variables": {
                "input": {
                    "cloudType": "ALL",
                    "gpuCount": 1,
                    "containerDiskInGb": 5,
                    "minVcpuCount": 1,
                    "minMemoryInGb": 1,
                    "gpuTypeId": gpu_id,
                    "name": "uptime-bug-repro",
                    "imageName": "mirror.gcr.io/library/ubuntu:22.04",
                    "dockerArgs": 'bash -c "sleep 600"',
                }
            },
        }
    ).encode(),
    headers=HEADERS,
    method="POST",
)
with urllib.request.urlopen(create_req) as r:  # noqa: S310
    create = json.loads(r.read())
pod_id = create["data"]["podFindAndDeployOnDemand"]["id"]
print(f"Created pod {pod_id} on {gpu_id}")

try:
    for i in range(11):
        time.sleep(30)
        now = datetime.now(UTC)
        r = gql(
            f'''query {{ pod(input: {{ podId: "{pod_id}" }}) {{
          desiredStatus lastStartedAt uptimeSeconds
          runtime {{ uptimeInSeconds }}
        }} }}'''
        )
        p = r["data"]["pod"] or {}
        nested = (p.get("runtime") or {}).get("uptimeInSeconds")
        top = p.get("uptimeSeconds")
        lsa = p.get("lastStartedAt", "")
        est = (
            (now - datetime.fromisoformat(lsa.replace("Z", "+00:00"))).total_seconds()
            if lsa
            else None
        )
        print(
            f"t={i * 30:>3}s  status={p.get('desiredStatus')}  "
            f"runtime.uptimeInSeconds={nested}  Pod.uptimeSeconds={top}  "
            f"now-lastStartedAt={est:.1f}s"
        )
finally:
    gql(f'mutation {{ podTerminate(input: {{ podId: "{pod_id}" }}) }}')
    print(f"Destroyed {pod_id}")
