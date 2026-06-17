"""C33 follow-up (l): live container-metric watcher during a kinoforge run.

Pairs with a concurrent `kinoforge generate` invocation. Once the kinoforge
log prints the active pod id, this script polls
``runtime.container.{cpuPercent, memoryPercent, gpuUtilPercent}`` plus
``runtime.uptimeInSeconds`` every 2 seconds for a configurable window.

Output is a JSON sidecar listing samples + cycle markers (each downward
uptime jump). If ``memoryPercent`` saturates (≥ 95 %) within the 5 s window
preceding every cycle's uptime reset, container-OOM is the mechanism. If
``memoryPercent`` stays low across the death event, OOM is RULED OUT.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GRAPHQL_URL = "https://api.runpod.io/graphql"
UA = "kinoforge/0.1 c33-l-metricwatch"

POLL_INTERVAL_S = 2.0
DEFAULT_WINDOW_S = 600.0


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


def _graphql(query: str) -> dict[str, Any]:
    body = json.dumps({"query": query}).encode()
    url = f"{GRAPHQL_URL}?api_key={urllib.parse.quote(_api_key(), safe='')}"
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode()))


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
    return None if pod is None else dict(pod)


_POD_ID_RE = re.compile(r"instance ([a-z0-9]{8,16})\b")


def _find_pod_id_in_log(log_path: Path) -> str | None:
    if not log_path.exists():
        return None
    text = log_path.read_text(errors="replace")
    for line in text.splitlines():
        m = _POD_ID_RE.search(line)
        if m and "provisioner.provision" in line:
            return m.group(1)
    # Fallback: any "instance <id>" match
    matches = _POD_ID_RE.findall(text)
    return matches[-1] if matches else None


def _detect_cycles(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a list of cycle events (downward uptime jumps)."""
    cycles: list[dict[str, Any]] = []
    prev_uptime: int | None = None
    for s in samples:
        u = s.get("ur")
        if u is None:
            prev_uptime = None
            continue
        if prev_uptime is not None and u < prev_uptime - 5:
            cycles.append(
                {
                    "at_wall_s": s["wall_s"],
                    "uptime_before_reset": prev_uptime,
                    "uptime_after_reset": u,
                    "mem_before": s.get("mem_before_reset"),
                    "cpu_before": s.get("cpu_before_reset"),
                }
            )
        prev_uptime = u
    return cycles


def main() -> int:
    """Run the metric watcher."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pod-id",
        default=None,
        help="explicit pod id (skip log discovery)",
    )
    parser.add_argument(
        "--log",
        default="/tmp/c33_l_kinoforge.log",  # noqa: S108
        help="kinoforge log path to scan for pod id",
    )
    parser.add_argument(
        "--window-s",
        type=float,
        default=DEFAULT_WINDOW_S,
        help="observation window in seconds (default 600)",
    )
    parser.add_argument(
        "--sidecar",
        default="tests/live/_c33_probe_l_evidence.json",
    )
    args = parser.parse_args()
    _load_env()

    sidecar = Path(args.sidecar)
    if sidecar.exists():
        sys.stderr.write(f"{sidecar} exists — refusing to overwrite\n")
        return 1

    pod_id = args.pod_id
    if pod_id is None:
        for _tries in range(60):  # wait up to 5 min for kinoforge to create a pod
            pod_id = _find_pod_id_in_log(Path(args.log))
            if pod_id and pod_id != "instance":
                break
            time.sleep(5)
        if pod_id is None:
            sys.stderr.write("[watcher] no pod id found in log\n")
            return 3
    sys.stderr.write(f"[watcher] watching pod_id={pod_id}\n")

    t0 = time.time()
    samples: list[dict[str, Any]] = []
    while time.time() - t0 < args.window_s:
        wall = round(time.time() - t0, 1)
        try:
            pod = _get_pod(pod_id)
        except Exception as e:  # noqa: BLE001
            samples.append({"wall_s": wall, "err": str(e)[:200]})
            time.sleep(POLL_INTERVAL_S)
            continue
        if pod is None:
            samples.append({"wall_s": wall, "pod": None})
            time.sleep(POLL_INTERVAL_S)
            continue
        runtime = pod.get("runtime") or {}
        container = runtime.get("container") or {}
        gpus = runtime.get("gpus") or []
        samples.append(
            {
                "wall_s": wall,
                "ds": pod.get("desiredStatus"),
                "ur": runtime.get("uptimeInSeconds"),
                "lsa": pod.get("lastStartedAt"),
                "cpu": container.get("cpuPercent"),
                "mem": container.get("memoryPercent"),
                "gpu_util": gpus[0].get("gpuUtilPercent") if gpus else None,
                "gpu_mem": gpus[0].get("memoryUtilPercent") if gpus else None,
            }
        )
        if len(samples) % 30 == 0:
            sys.stderr.write(
                f"[watcher] poll {len(samples)} wall={wall} mem={container.get('memoryPercent')} cpu={container.get('cpuPercent')} ur={runtime.get('uptimeInSeconds')}\n"
            )
        time.sleep(POLL_INTERVAL_S)

    # Decorate cycle events with mem/cpu reading just before reset
    for i, s in enumerate(samples):
        if i > 0:
            s["mem_before_reset"] = samples[i - 1].get("mem")
            s["cpu_before_reset"] = samples[i - 1].get("cpu")
    cycles = _detect_cycles(samples)
    result = {
        "probe": "C33 (l) — container-metric watcher during kinoforge run",
        "date_utc": datetime.now(UTC).isoformat(),
        "pod_id": pod_id,
        "window_s": args.window_s,
        "poll_interval_s": POLL_INTERVAL_S,
        "n_samples": len(samples),
        "cycles": cycles,
        "n_cycles": len(cycles),
        "samples": samples,
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(result, indent=2, default=str))
    sys.stderr.write(f"[watcher] {len(cycles)} cycles, sidecar={sidecar}\n")
    print(json.dumps({"cycles": len(cycles), "sidecar": str(sidecar)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
