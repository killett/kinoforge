"""U36 live probe — does the ADVERTISED rate predict the REALIZED rate, per pool?

U36 records that RunPod's advertised ``lowestPrice`` under-predicts the realized
pod rate by 5-10x, turning a routine run into a create-then-destroy loop against
S4's ``_enforce_rate_cap``.  The 2026-09-12 session then observed a *secure* host
billing $0.27/hr against community pods realizing $0.49-$0.59 and filed the
question this probe answers: **is the gap a community-pool artifact?**

The offline half is already measured and needs no pod (see
``probe_advertised_by_pool``): RunPod's ``lowestPrice(input: {gpuCount: 1})`` —
which is exactly what ``RunPodProvider.find_offers`` sends — returns the
COMMUNITY floor on every GPU that exists in both pools, while the same query with
``secureCloud: true`` returns a rate 1.5-2.3x higher.  Every shipped RunPod cfg
pins ``cloud_type: secure``.  So the price kinoforge filters and ranks on
describes a pool it never books into.

That is a hypothesis about a mechanism, not a measurement of the outcome.  This
probe measures the outcome: it books one real pod per (gpu, pool) arm, reads the
pod's own ``costPerHr`` off the GraphQL list query, and prints it beside both
advertised numbers.  The arms are chosen so the two hypotheses DISAGREE on a
field rather than merely predicting the same shape (the U34 lesson):

- ``4090 / SECURE``    — advertised(all) $0.34, advertised(secure) $0.74.
  2026-09-11 recorded a realized $0.74 on a 4090.  If the secure-filtered
  number is what gets billed, this arm reproduces it exactly.
- ``4090 / COMMUNITY`` — advertised(all) $0.34 == advertised(community) $0.34.
  The control.  If realized tracks the pool, this arm bills ~$0.34 and the
  unfiltered query was right all along *for the pool it describes*.
- ``L4 / SECURE``      — advertised(secure) $0.49.  U36 attributes a realized
  $2.39/hr to the L4; the catalog says the L4 is $0.49 in both pools and that
  $2.39 is the AMD MI300X's SECURE rate.  This arm tests that attribution.
- ``MI300X / SECURE``  — advertised(all) $0.50, advertised(secure) $2.39.  The
  MI300X ranks SECOND in the shipped 1.3B grid cfg's offer list (it passes a
  ``min_cuda`` filter it cannot satisfy, because ``find_offers`` hardcodes
  ``cuda="12.8"`` on every RunPod offer).  If this bills $2.39, U36's
  "realized $2.39" was this machine, not the L4.

Cost: each arm is created and terminated as soon as ``costPerHr`` is readable —
seconds of billing.  The MI300X arm is the expensive one at $2.39/hr, and it is
still under a cent for the ~15 s it lives.  Every arm terminates in a ``finally``
and the probe re-verifies an empty pod list before it exits.

Read-only safety: the realized rate is read from ``myself { pods { costPerHr } }``
— the LIST query — not from RunPod's REST pod-detail endpoint, which echoes
``HF_TOKEN``/``GH_TOKEN`` in cleartext.

Run:  pixi run python tests/live/probes/u36_pool_price_probe.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from kinoforge.core.dotenv_loader import load_env_file

# The four arms, in the order they are booked.  ``gpu`` values are RunPod's own
# catalog ids (``gpuTypes.id``) — NOT the names the shipped cfgs use, which is
# its own defect: ``filter_offers`` ranks by exact string equality, so a cfg
# naming "NVIDIA RTX 4090" against a catalog id of "NVIDIA GeForce RTX 4090"
# ranks that GPU no higher than an unlisted one.
ARMS: tuple[tuple[str, str], ...] = (
    ("NVIDIA GeForce RTX 4090", "SECURE"),
    ("NVIDIA GeForce RTX 4090", "COMMUNITY"),
    ("NVIDIA L4", "SECURE"),
    ("AMD Instinct MI300X OAM", "SECURE"),
)

# Small public image.  Nothing is provisioned and nothing runs — the pod exists
# only long enough to report the rate its machine bills.  RunPod assigns
# costPerHr at placement, so the image never needs to finish pulling.
PROBE_IMAGE = "alpine:latest"

# Hard ceiling on how long any one arm may live before we give up and terminate.
READ_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 5.0


def _endpoint() -> str:
    """Return the authenticated RunPod GraphQL URL.

    Returns:
        The GraphQL endpoint with the API key appended as a query parameter,
        matching the provider's own auth seam.

    Raises:
        KeyError: If ``RUNPOD_API_KEY`` is absent from the environment.
    """
    return f"https://api.runpod.io/graphql?api_key={os.environ['RUNPOD_API_KEY']}"


def gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST a GraphQL query to RunPod and return the decoded body.

    A non-2xx response is returned as a dict rather than raised, so a caller can
    distinguish an HTTP-level refusal (Cloudflare, payload size) from a
    GraphQL-level ``errors[]`` body.  A probe whose negative result cannot name
    its own cause is not a measurement (the U34 lesson).

    Args:
        query: The GraphQL document to send.
        variables: Optional variables object for the document.

    Returns:
        The decoded JSON body, or ``{"__http_error": <code>, "body": <text>}``
        when the server answered non-2xx.
    """
    payload: dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    req = urllib.request.Request(
        _endpoint(),
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            # RunPod fronts its API with Cloudflare, which 403s the default
            # ``Python-urllib`` User-Agent (diagnosed live on U34).
            "User-Agent": "kinoforge/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body: dict[str, Any] = json.loads(resp.read())
            return body
    except urllib.error.HTTPError as exc:  # noqa: PERF203 — one call, one guard
        return {"__http_error": exc.code, "body": exc.read().decode()[:1000]}


def advertised(gpu_ids: set[str]) -> dict[str, dict[str, float | None]]:
    """Read the advertised hourly rate for *gpu_ids* under each pool filter.

    Args:
        gpu_ids: RunPod catalog ids to report on.

    Returns:
        Mapping of gpu id -> ``{"all": price, "secure": price,
        "community": price}``, where a price is ``None`` when RunPod reports no
        availability for that GPU in that pool.
    """
    out: dict[str, dict[str, float | None]] = {g: {} for g in gpu_ids}
    for label, extra in (
        ("all", ""),
        ("secure", ", secureCloud: true"),
        ("community", ", secureCloud: false"),
    ):
        doc = (
            "{ gpuTypes { id memoryInGb "
            f"lowestPrice(input: {{ gpuCount: 1{extra} }}) "
            "{ uninterruptablePrice } } }"
        )
        for gpu in gql(doc)["data"]["gpuTypes"]:
            if gpu["id"] not in gpu_ids:
                continue
            price = (gpu.get("lowestPrice") or {}).get("uninterruptablePrice")
            out[gpu["id"]][label] = None if price is None else float(price)
    return out


def create_pod(gpu_id: str, cloud_type: str, name: str) -> tuple[str | None, str]:
    """Book one minimal pod and return its id.

    The input mirrors ``RunPodProvider._create_pod``'s shape in the fields that
    affect PLACEMENT (``cloudType``, ``gpuCount``, ``gpuTypeId``,
    ``minVcpuCount``, ``minMemoryInGb``) and nothing else — no env, no
    dockerArgs, no ports.  A minimal create body is also the documented way to
    rule out payload size when a create 500s.

    Args:
        gpu_id: RunPod catalog id of the GPU to book.
        cloud_type: ``"SECURE"`` or ``"COMMUNITY"``.
        name: Pod name, used to identify the row in the list query.

    Returns:
        ``(pod_id, detail)`` — ``pod_id`` is ``None`` when the create failed,
        and ``detail`` always carries a human-readable reason.
    """
    mutation = (
        "mutation($input: PodFindAndDeployOnDemandInput!) "
        "{ podFindAndDeployOnDemand(input: $input) { id desiredStatus } }"
    )
    variables = {
        "input": {
            "cloudType": cloud_type,
            "gpuCount": 1,
            "volumeInGb": 0,
            "containerDiskInGb": 10,
            "minVcpuCount": 2,
            "minMemoryInGb": 8,
            "gpuTypeId": gpu_id,
            "name": name,
            "imageName": PROBE_IMAGE,
        }
    }
    resp = gql(mutation, variables)
    if "__http_error" in resp:
        return None, f"HTTP {resp['__http_error']}: {resp['body'][:200]}"
    if resp.get("errors"):
        return None, f"graphql error: {resp['errors'][0].get('message', '?')[:200]}"
    node = (resp.get("data") or {}).get("podFindAndDeployOnDemand")
    if not node or not node.get("id"):
        return None, f"no pod id in response: {json.dumps(resp)[:200]}"
    return str(node["id"]), "created"


def read_cost(pod_id: str) -> float | None:
    """Return the pod's own ``costPerHr``, or ``None`` if not yet reported.

    Reads the LIST query deliberately: RunPod's REST pod-detail endpoint echoes
    credential env vars in cleartext, so it must never be used here.

    Args:
        pod_id: The pod to look up.

    Returns:
        The billed hourly rate, or ``None`` when the pod is absent from the list
        or its rate is not yet populated.
    """
    resp = gql("{ myself { pods { id name costPerHr } } }")
    if "__http_error" in resp or resp.get("errors"):
        return None
    pods = ((resp.get("data") or {}).get("myself") or {}).get("pods") or []
    for pod in pods:
        if pod.get("id") == pod_id:
            cost = pod.get("costPerHr")
            return None if cost is None else float(cost)
    return None


def terminate(pod_id: str) -> str:
    """Terminate *pod_id*, returning a short outcome string.

    Args:
        pod_id: The pod to terminate.

    Returns:
        ``"terminated"``, or a description of why the call did not succeed.  A
        ``POD_NOT_FOUND`` reads as a GraphQL error but means the host already
        reclaimed the pod — that indirection is exactly what made U42 hard to
        see, so it is named explicitly here.
    """
    resp = gql(
        "mutation($input: PodTerminateInput!) { podTerminate(input: $input) }",
        {"input": {"podId": pod_id}},
    )
    if "__http_error" in resp:
        return f"HTTP {resp['__http_error']}"
    if resp.get("errors"):
        msg = str(resp["errors"][0].get("message", "?"))
        if "NOT_FOUND" in msg.upper():
            return "already gone (host reclaimed it — U42 shape)"
        return f"graphql error: {msg[:120]}"
    return "terminated"


@dataclass
class ArmResult:
    """One arm's measurement.

    Attributes:
        gpu: RunPod catalog id booked.
        pool: ``"SECURE"`` or ``"COMMUNITY"``.
        pod_id: The pod booked, when the create succeeded.
        realized: The pod's own ``costPerHr``, when it was readable.
        note: Why an arm produced no number, when it did not.
        teardown: Outcome of the terminate call.
    """

    gpu: str
    pool: str
    pod_id: str | None = None
    realized: float | None = None
    note: str = ""
    teardown: str = "not attempted"
    polls: list[float] = field(default_factory=list)


def run_arm(gpu: str, pool: str, idx: int) -> ArmResult:
    """Book one pod, read its realized rate, and terminate it.

    Args:
        gpu: RunPod catalog id to book.
        pool: ``"SECURE"`` or ``"COMMUNITY"``.
        idx: Arm index, used to build a distinguishable pod name.

    Returns:
        The populated :class:`ArmResult`.
    """
    result = ArmResult(gpu=gpu, pool=pool)
    name = f"u36-pool-probe-{idx}"
    pod_id, detail = create_pod(gpu, pool, name)
    if pod_id is None:
        result.note = detail
        return result
    result.pod_id = pod_id
    print(f"    created {pod_id} — polling for costPerHr", flush=True)
    try:
        deadline = time.monotonic() + READ_TIMEOUT_S
        while time.monotonic() < deadline:
            cost = read_cost(pod_id)
            if cost is not None and cost > 0:
                result.realized = cost
                break
            result.polls.append(0.0 if cost is None else cost)
            time.sleep(POLL_INTERVAL_S)
        else:
            result.note = f"costPerHr never populated within {READ_TIMEOUT_S:.0f}s"
    finally:
        result.teardown = terminate(pod_id)
        print(f"    teardown: {result.teardown}", flush=True)
    return result


def main() -> None:
    """Run every arm, then print the advertised-vs-realized table."""
    load_env_file()
    gpu_ids = {gpu for gpu, _ in ARMS}
    ads = advertised(gpu_ids)

    print("ADVERTISED (RunPod lowestPrice, $/hr):")
    for gpu in sorted(gpu_ids):
        row = ads[gpu]
        fmt = lambda p: " none " if p is None else f"{p:>6.3f}"  # noqa: E731
        print(
            f"  {gpu:<26} all={fmt(row.get('all'))} "
            f"secure={fmt(row.get('secure'))} community={fmt(row.get('community'))}"
        )

    results: list[ArmResult] = []
    for idx, (gpu, pool) in enumerate(ARMS):
        print(f"\n[arm {idx}] {gpu} / {pool}", flush=True)
        results.append(run_arm(gpu, pool, idx))

    print("\n" + "=" * 78)
    print("ADVERTISED vs REALIZED")
    print("=" * 78)
    print(
        f"{'gpu / pool':<36} {'adv(all)':>9} {'adv(pool)':>10} {'realized':>9}  ratio"
    )
    for res in results:
        row = ads[res.gpu]
        adv_all = row.get("all")
        adv_pool = row.get("secure" if res.pool == "SECURE" else "community")
        if res.realized is None:
            print(
                f"{res.gpu + ' / ' + res.pool:<36} {'':>9} {'':>10} {'FAILED':>9}  {res.note}"
            )
            continue
        ratio_all = f"{res.realized / adv_all:.2f}x vs all" if adv_all else "n/a"
        ratio_pool = f"{res.realized / adv_pool:.2f}x vs pool" if adv_pool else "n/a"
        fmt = lambda p: "  none  " if p is None else f"{p:>9.3f}"  # noqa: E731
        print(
            f"{res.gpu + ' / ' + res.pool:<36} {fmt(adv_all)} {fmt(adv_pool)} "
            f"{res.realized:>9.3f}  {ratio_all}, {ratio_pool}"
        )

    print("\nteardown verification (expect an empty pod list):")
    resp = gql("{ myself { pods { id name costPerHr } } }")
    pods = ((resp.get("data") or {}).get("myself") or {}).get("pods") or []
    if not pods:
        print("  no pods remain")
    else:
        for pod in pods:
            print(
                f"  STILL RUNNING: {pod['id']} {pod.get('name')} ${pod.get('costPerHr')}/hr"
            )


if __name__ == "__main__":
    main()
