"""C30 probe helpers for fault-isolation of the RunPod restart loop.

Provides direct-GraphQL pod probes, S3 trap-fire counting, verdict
classification, spend-ledger enforcement, and verify-and-retry destroy.
All public helpers are documented in
``docs/superpowers/specs/2026-06-14-c30-restart-loop-diagnosis-design.md``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

_LOG = logging.getLogger(__name__)

_DIAG_KEY_PATTERN = re.compile(r"/diag-\d{8}T\d{6}Z\.txt$")


class Verdict(Enum):
    """Outcome classes for a 10-minute probe window.

    SURVIVED  — pod stayed up the whole window; no trap fires; uptime
                monotonically increased across all samples.
    RESTARTED — pod cycled >=3 times within the window (trap-fire count
                is the authoritative signal; uptime drops corroborate).
    AMBIGUOUS — evidence cannot distinguish the two; rerun the probe
                or treat as RESTARTED conservatively per spec §3.
    """

    SURVIVED = "survived"
    RESTARTED = "restarted"
    AMBIGUOUS = "ambiguous"


def classify_run(
    poll_trail: Sequence[tuple[float, int | None]],
    fire_count: int,
) -> Verdict:
    """Classify a probe run from its poll trail and S3 trap-fire count.

    Args:
        poll_trail: ``(elapsed_seconds, uptime_in_seconds)`` per sample.
            ``uptime_in_seconds`` may be ``None`` when the GraphQL
            ``pod(podId)`` response lacked a ``runtime`` block (transient).
        fire_count: Number of ``diag-*.txt`` objects under the run's
            S3 prefix.

    Returns:
        Verdict per spec §3 rules.
    """
    if fire_count >= 3:
        return Verdict.RESTARTED
    if fire_count >= 1:
        return Verdict.AMBIGUOUS
    if len(poll_trail) < 2:
        return Verdict.AMBIGUOUS
    raw = [u for _, u in poll_trail]
    if any(u is None for u in raw):
        return Verdict.AMBIGUOUS
    uptimes: list[int] = [u for u in raw if u is not None]
    for prev, curr in zip(uptimes, uptimes[1:], strict=False):
        if curr <= prev:
            return Verdict.AMBIGUOUS
    return Verdict.SURVIVED


def count_trap_fires(
    s3_client: Any,  # noqa: ANN401 — injected boto3 S3 client; avoid SDK import in signature
    bucket: str,
    prefix: str,
) -> int:
    """Count ``diag-YYYYMMDDTHHMMSSZ.txt`` objects under ``bucket/prefix``.

    Args:
        s3_client: A boto3 S3 client (or anything with a compatible
            ``list_objects_v2`` method).
        bucket: S3 bucket name (no scheme).
        prefix: Key prefix. Must include the trailing slash if the
            prefix is a directory.

    Returns:
        Number of diag-pattern objects. Returns 0 on ``NoSuchKey``.
    """
    total = 0
    continuation: str | None = None
    try:
        while True:
            kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation is not None:
                kw["ContinuationToken"] = continuation
            page = s3_client.list_objects_v2(**kw)
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key", "")
                if _DIAG_KEY_PATTERN.search(key):
                    total += 1
            if not page.get("IsTruncated"):
                return total
            continuation = page.get("NextContinuationToken")
            if continuation is None:
                return total
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
            return 0
        raise


class BudgetCapExceeded(RuntimeError):
    """Raised when cumulative spend would meet or exceed the hard cap."""


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"cumulative_usd": 0.0, "entries": []}
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed C30 spend ledger at {path}: {exc}") from exc


def assert_under_cap(path: Path, hard_cap_usd: float) -> None:
    """Raise ``BudgetCapExceeded`` if cumulative spend in ``path`` >= cap."""
    payload = _read_ledger(path)
    cumulative = float(payload.get("cumulative_usd", 0.0))
    if cumulative >= hard_cap_usd:
        raise BudgetCapExceeded(
            f"Cumulative C30 spend ${cumulative:.4f} >= cap ${hard_cap_usd:.2f}"
        )


def append_spend_entry(path: Path, entry: dict[str, Any]) -> None:
    """Append a spend entry and rewrite the ledger.

    Args:
        path: Ledger JSON path.
        entry: Dict with keys ``phase``, ``pod_id``, ``gpu_type_id``,
            ``cents_per_hr``, ``start_ts``, ``end_ts``, ``est_spend_usd``.
            Timestamps must be ISO-8601 with offset.

    Raises:
        ValueError: If ``start_ts`` precedes the last existing entry's
            ``end_ts``.
    """
    payload = _read_ledger(path)
    entries = list(payload.get("entries", []))
    if entries:
        last_end = datetime.fromisoformat(str(entries[-1]["end_ts"]))
        new_start = datetime.fromisoformat(str(entry["start_ts"]))
        if new_start < last_end:
            raise ValueError(
                f"Entry start_ts {entry['start_ts']} is not monotonic vs "
                f"prior entry end_ts {entries[-1]['end_ts']}"
            )
    entries.append(entry)
    cumulative = float(payload.get("cumulative_usd", 0.0)) + float(
        entry["est_spend_usd"]
    )
    path.write_text(
        json.dumps(
            {"cumulative_usd": round(cumulative, 6), "entries": entries},
            indent=2,
        )
        + "\n"
    )


# Inlined verbatim from src/kinoforge/engines/comfyui/__init__.py lines
# 1285-1330 (the diagnostic_mode trap_preamble in ComfyUIEngine.render_provision).
# Inlined rather than imported because C30 must not touch production code
# (spec §2 non-goal). If the source diverges, sync this constant.
_C28_TRAP_PREAMBLE_LINES: list[str] = [
    "set -euo pipefail",
    "command -v aws >/dev/null 2>&1 || pip install -q awscli >/dev/null 2>&1 || true",
    "command -v aria2c >/dev/null 2>&1 || "
    "(apt-get update -qq && apt-get install -y -qq aria2 "
    ">/dev/null 2>&1) || true",
    "exec > >(tee -a /tmp/boot.log) 2>&1",
    "trap '_kinoforge_diag_capture $?' EXIT",
    "_kinoforge_diag_capture() {",
    "  local rc=$1",
    "  local last_line",
    "  last_line=$(tail -1 /tmp/boot.log 2>/dev/null || true)",
    "  {",
    "    echo '===== rc ====='; echo \"$rc\";",
    "    echo '===== last_line ====='; echo \"$last_line\";",
    "    echo '===== nvidia-smi ====='; nvidia-smi || true;",
    "    echo '===== df -h ====='; df -h || true;",
    "    echo '===== free -m ====='; free -m || true;",
    "    echo '===== ls -la models/diffusion_models ====='; "
    "ls -la /workspace/ComfyUI/models/diffusion_models 2>/dev/null"
    " || true;",
    "    echo '===== dpkg -l torch ====='; "
    "dpkg -l 2>/dev/null | grep -iE 'torch|cuda' || true;",
    "    echo '===== boot.log ====='; tail -500 /tmp/boot.log 2>/dev/null || true;",
    "  } > /tmp/diag.txt",
    '  if [ -n "${KINOFORGE_DIAG_BUCKET:-}" ]; then',
    "    aws s3 cp /tmp/diag.txt "
    '"s3://${KINOFORGE_DIAG_BUCKET}/${KINOFORGE_DIAG_PREFIX}/'
    'diag-$(date -u +%Y%m%dT%H%M%SZ).txt" || true',
    "  fi",
    "}",
]


_CREATE_POD_MUTATION = """
mutation podFindAndDeployOnDemand($input: PodFindAndDeployOnDemandInput!) {
  podFindAndDeployOnDemand(input: $input) {
    id
    desiredStatus
    imageName
  }
}
""".strip()


def create_probe_pod(
    client: Any,  # noqa: ANN401 — injected GraphQL client; SDK-agnostic
    *,
    image: str,
    ports: str | None,
    provision_script: str,
    env: dict[str, str],
    gpu_type_id: str,
    run_id: str,
    diag_bucket: str,
) -> str:
    """Create a stock RunPod pod via direct GraphQL with the C28 trap.

    Args:
        client: Object with ``execute(query, variables) -> dict``.
        image: Docker image reference.
        ports: RunPod ``ports`` string (e.g. ``"8188/http"``) or ``None``
            to omit declaration entirely.
        provision_script: Bash to run AFTER the trap pre-amble — the
            actual probe payload (e.g. ``"sleep 600"``).
        env: Additional pod env vars. ``KINOFORGE_DIAG_BUCKET`` and
            ``KINOFORGE_DIAG_PREFIX`` are added/overwritten here.
        gpu_type_id: RunPod GPU type ID string.
        run_id: Per-probe identifier; becomes the S3 prefix suffix.
        diag_bucket: Diagnostics S3 bucket name.

    Returns:
        Newly created pod ID.
    """
    merged_env = dict(env)
    merged_env["KINOFORGE_DIAG_BUCKET"] = diag_bucket
    merged_env["KINOFORGE_DIAG_PREFIX"] = f"boot-logs/{run_id}"

    full_script = "\n".join([*_C28_TRAP_PREAMBLE_LINES, provision_script])
    docker_args = f'bash -c "{full_script}"'

    input_obj: dict[str, Any] = {
        "imageName": image,
        "gpuTypeId": gpu_type_id,
        "dockerArgs": docker_args,
        "env": [{"key": k, "value": v} for k, v in merged_env.items()],
    }
    if ports is not None:
        input_obj["ports"] = ports

    result = client.execute(_CREATE_POD_MUTATION, {"input": input_obj})
    return str(result["data"]["podFindAndDeployOnDemand"]["id"])


_POD_STATUS_QUERY = (
    'query {{ pod(input: {{ podId: "{pod_id}" }}) '
    "{{ id desiredStatus runtime {{ uptimeInSeconds }} }} }}"
)


@dataclass
class PodStatusPoller:
    """Poll ``pod(podId)`` for ``runtime.uptimeInSeconds`` over a window.

    Args:
        client: Object with ``execute(query, variables) -> dict``.
        pod_id: Pod ID to probe.
        window_s: Total polling duration.
        interval_s: Sleep between samples.
        sleep: Injectable sleep (default ``time.sleep``) — enables fast tests.
        clock: Injectable monotonic clock returning seconds (default
            ``time.monotonic``).
    """

    client: Any
    pod_id: str
    window_s: float
    interval_s: float
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)

    def poll(self) -> list[tuple[float, int | None]]:
        """Run the poll loop. Returns trail of ``(elapsed_seconds, uptime)``."""
        trail: list[tuple[float, int | None]] = []
        n_intervals = int(self.window_s // self.interval_s)
        n_samples = n_intervals + 1
        start: float | None = None
        for i in range(n_samples):
            now = self.clock()
            if start is None:
                start = now
            uptime = self._read_uptime()
            trail.append((now - start, uptime))
            if i < n_samples - 1:
                self.sleep(self.interval_s)
        return trail

    def _read_uptime(self) -> int | None:
        q = _POD_STATUS_QUERY.format(pod_id=self.pod_id)
        result = self.client.execute(q, {})
        pod = (result.get("data") or {}).get("pod")
        if pod is None:
            return None
        runtime = pod.get("runtime")
        if runtime is None:
            return None
        val = runtime.get("uptimeInSeconds")
        return int(val) if val is not None else None


_TERMINATE_MUTATION = (
    "mutation podTerminate($podId: String!) { podTerminate(input: { podId: $podId }) }"
)
_LIST_PODS_QUERY = "query { myself { pods { id } } }"


def destroy_with_retry(
    client: Any,  # noqa: ANN401 — injected GraphQL client; SDK-agnostic
    *,
    pod_id: str,
    attempts: int = 5,
    sleep_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Issue ``podTerminate`` and verify the pod has actually left ``myself.pods``.

    Args:
        client: GraphQL client.
        pod_id: Pod to terminate.
        attempts: Maximum terminate calls before giving up. Default 5.
        sleep_s: Sleep between polls. Default 3 s.
        sleep: Injectable sleep callable.

    Returns:
        Number of terminate mutations issued.
    """
    n = 0
    for _ in range(attempts):
        n += 1
        client.execute(_TERMINATE_MUTATION, {"podId": pod_id})
        sleep(sleep_s)
        listing = client.execute(_LIST_PODS_QUERY, {})
        pods = (listing.get("data") or {}).get("myself", {}).get("pods") or []
        if not any(p.get("id") == pod_id for p in pods):
            return n
    _LOG.warning(
        "c30 destroy_with_retry: pod %s still present after %d attempts",
        pod_id,
        attempts,
    )
    return n
