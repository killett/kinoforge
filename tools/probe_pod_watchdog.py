"""Live-verify the in-pod selfterm watchdog actually fires.

Boots a minimal RunPod pod with a tiny ``max_lifetime`` (60 s) and a
no-op bootstrap that just launches the selfterm watchdog + sleeps;
polls the RunPod REST API until the pod disappears OR a wall-clock cap
expires; reports SUCCESS only if the pod went away within the
selfterm window (which is roughly ``max_lifetime - time_buffer``) WITHOUT
this script issuing a destroy call.

If the pod is still present at the wall-clock cap, the probe fails AND
issues a fallback REST DELETE so no leak is left billing.

Cost: ~$0.005-0.01 per probe (60-120 s × $0.27/hr).

Usage::

    pixi run probe-watchdog

Outputs::

    probe: pod boot
    probe: pod abc123 alive at 30s
    probe: pod abc123 alive at 60s
    probe: pod abc123 GONE at 75s — selfterm fired
    probe: PASS

Pre-conditions enforced by ``main()``:

* ``RUNPOD_API_KEY``, ``RUNPOD_TERMINATE_KEY`` set (auto-loaded from ``.env``).
* Zero active RunPod pods at start (otherwise this probe's poll cannot
  distinguish "our probe pod" from a leaked pod).

The probe uses a stub bootstrap script that does NOT clone ComfyUI or
fetch any weights — only launches the selfterm watchdog and sleeps.
This isolates the question "does the launch line in render_provision
actually execute?" from the unrelated boot-time of a real workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools._redact import safe_print  # noqa: E402  sys.path edit above

#: How long we poll after pod creation before declaring the probe failed.
_PROBE_WALL_CAP_S: float = 150.0

#: Poll interval — fast enough to catch the self-destroy edge, slow enough
#: not to hammer RunPod's REST API.
_PROBE_POLL_INTERVAL_S: float = 5.0


def _build_probe_bootstrap() -> str:
    """Return the bash bootstrap used by the probe pod.

    Mirrors the selfterm launch lines that ``ComfyUIEngine.render_provision``
    + ``DiffusersEngine.render_provision`` prepend in production; replaces
    the engine-specific clone/install/exec tail with a long ``sleep`` so
    the pod stays alive until selfterm kills it (or the wall-clock cap
    triggers our fallback destroy).
    """
    return (
        "set -euo pipefail\n"
        'if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]; then '
        "python3 -c \"import os; open('/tmp/selfterm.py','w')"
        ".write(os.environ['KINOFORGE_SELFTERM_SCRIPT'])\" && "
        "nohup python3 /tmp/selfterm.py > /tmp/selfterm.log 2>&1 & "
        "fi\n"
        "exec sleep 600\n"
    )


def _list_pod_ids_runpod(api_key: str) -> list[str]:
    """Default REST pod-lister; returns just IDs.

    Args:
        api_key: RUNPOD_API_KEY for Bearer auth.

    Returns:
        List of currently-active pod IDs.
    """
    req = urllib.request.Request(
        "https://rest.runpod.io/v1/pods",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "kinoforge-probe-watchdog/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read().decode()
    parsed = json.loads(body)
    if not isinstance(parsed, list):
        return []
    return [p["id"] for p in parsed if isinstance(p, dict) and "id" in p]


def _destroy_via_rest(api_key: str, pod_id: str) -> int:
    """Best-effort REST DELETE on the given pod. Returns HTTP status code.

    Args:
        api_key: RUNPOD_API_KEY for Bearer auth.
        pod_id: ID of the pod to destroy.
    """
    req = urllib.request.Request(
        f"https://rest.runpod.io/v1/pods/{pod_id}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "kinoforge-probe-watchdog/1.0",
        },
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def run_probe(
    *,
    create_pod_and_get_id: Callable[[], str],
    list_pod_ids: Callable[[], list[str]],
    destroy_pod: Callable[[str], int],
    sleep: Callable[[float], None],
    wall_cap_s: float = _PROBE_WALL_CAP_S,
    poll_interval_s: float = _PROBE_POLL_INTERVAL_S,
) -> tuple[int, list[str]]:
    """Run the watchdog probe with injected I/O seams.

    Args:
        create_pod_and_get_id: Boots the probe pod and returns its ID.
        list_pod_ids: Returns IDs of all currently-active pods.
        destroy_pod: Fallback DELETE for the probe pod if it persists.
        sleep: Sleep seam between polls (real sleep in CLI;
            instantaneous in tests).
        wall_cap_s: Max seconds to wait for self-destroy.
        poll_interval_s: Seconds between polls.

    Returns:
        ``(exit_code, lines)`` — ``0`` if the pod self-destroyed within
        the window without our intervention; ``1`` otherwise.
    """
    lines: list[str] = ["probe: pod boot"]
    pod_id = create_pod_and_get_id()
    lines.append(f"probe: created pod {pod_id}")

    elapsed = 0.0
    while elapsed < wall_cap_s:
        sleep(poll_interval_s)
        elapsed += poll_interval_s
        active = list_pod_ids()
        if pod_id not in active:
            lines.append(
                f"probe: pod {pod_id} GONE at ~{int(elapsed)}s — selfterm fired"
            )
            lines.append("probe: PASS")
            return (0, lines)
        lines.append(f"probe: pod {pod_id} alive at {int(elapsed)}s")

    # Wall cap exceeded — pod still present, selfterm didn't fire (or fired
    # but the terminate call failed). Fallback DELETE so no leak survives.
    status = destroy_pod(pod_id)
    lines.append(
        f"probe: pod {pod_id} STILL ACTIVE after {int(wall_cap_s)}s — "
        f"fallback DELETE returned HTTP {status}"
    )
    lines.append("probe: FAIL — selfterm watchdog did not fire")
    return (1, lines)


def main() -> int:
    """CLI entrypoint.

    Pre-conditions: ``RUNPOD_API_KEY`` + ``RUNPOD_TERMINATE_KEY`` set
    (auto-loaded from ``.env``) AND zero active pods at start.
    """
    parser = argparse.ArgumentParser(prog="probe_pod_watchdog")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(_REPO_ROOT) / ".env",
        help="Path to .env file to auto-load (default: .env).",
    )
    args = parser.parse_args()

    from kinoforge.core.dotenv_loader import load_env_file

    if args.env_file.exists():
        load_env_file(args.env_file)

    for k in ("RUNPOD_API_KEY", "RUNPOD_TERMINATE_KEY"):
        if not os.environ.get(k):
            safe_print(f"probe_pod_watchdog: missing env var: {k}")
            return 1

    api_key = os.environ["RUNPOD_API_KEY"]

    # Pre-flight pod-list sanity: probe assumes 0 active pods so the
    # poll can unambiguously attribute "pod gone" to selfterm. If another
    # pod is running, refuse to start — operator must reconcile first.
    existing = _list_pod_ids_runpod(api_key)
    if existing:
        safe_print(
            f"probe_pod_watchdog: refusing to run — {len(existing)} pod(s) "
            f"already active: {existing}. Run `pixi run preflight` to triage."
        )
        return 1

    # Lazy-import kinoforge so registries don't load when env-gate fails.
    import kinoforge._adapters  # noqa: F401  registers providers
    from kinoforge.core import registry
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import (
        HardwareRequirements,
        InstanceSpec,
        Lifecycle,
    )
    from kinoforge.providers.runpod.selfterm import RENDER as render_selfterm

    creds = EnvCredentialProvider()
    provider = registry.get_provider("runpod")()
    provider._creds = creds  # type: ignore[attr-defined]

    reqs = HardwareRequirements(min_vram_gb=8, max_usd_per_hr=0.50)
    offers = provider.find_offers(reqs)
    if not offers:
        safe_print("probe_pod_watchdog: no offers under $0.50/hr with ≥8 GB VRAM")
        return 1
    offer = offers[0]
    print(
        f"probe: cheapest offer = {offer.gpu_type} @ ${offer.cost_rate_usd_per_hr:.2f}/hr"
    )

    bootstrap = _build_probe_bootstrap()
    selfterm_script = render_selfterm(
        idle_timeout=30.0,
        max_lifetime=60.0,
        job_timeout=10.0,
        time_buffer=10.0,
    )
    spec = InstanceSpec(
        image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        offer=offer,
        ports=("8188",),
        volume_gb=10,
        volume_mount="/workspace",
        lifecycle=Lifecycle(
            idle_timeout_s=30.0,
            max_lifetime_s=60.0,
            job_timeout_s=10.0,
            time_buffer_s=10.0,
        ),
        env={"KINOFORGE_SELFTERM_SCRIPT": selfterm_script},
        tags={"mode": "pod", "kinoforge_purpose": "selfterm_probe"},
        run_id="kinoforge-watchdog-probe",
        provision_script=bootstrap,
        run_cmd=["sleep", "600"],
    )

    def _create_pod_and_get_id() -> str:
        instance = provider.create_instance(spec)
        return instance.id

    def _list_pod_ids() -> list[str]:
        return _list_pod_ids_runpod(api_key)

    def _destroy_pod(pid: str) -> int:
        return _destroy_via_rest(api_key, pid)

    code, lines = run_probe(
        create_pod_and_get_id=_create_pod_and_get_id,
        list_pod_ids=_list_pod_ids,
        destroy_pod=_destroy_pod,
        sleep=time.sleep,
    )
    for ln in lines:
        print(ln)
    return code


if __name__ == "__main__":
    sys.exit(main())
