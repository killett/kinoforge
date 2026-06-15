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
    raw = [u for _, u in poll_trail]
    # Negative uptime is non-physical — it appeared in C30 A1a evidence
    # (run c30-a1a-20260614T222804) on a pod that was actively restart-
    # cycling. The S3 EXIT trap did NOT fire (pod was killed before
    # `aws s3 cp` could complete), so fire_count alone underdetects the
    # restart. Treat any non-None negative as a positive restart signal.
    if any(u is not None and u < 0 for u in raw):
        return Verdict.RESTARTED
    if fire_count >= 1:
        return Verdict.AMBIGUOUS
    if len(poll_trail) < 2:
        return Verdict.AMBIGUOUS
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


class GraphQLError(RuntimeError):
    """Raised when a GraphQL response includes ``errors`` or null data."""

    def __init__(self, message: str, code: str | None = None) -> None:
        """Store the GraphQL ``extensions.code`` so callers can dispatch."""
        super().__init__(message)
        self.code = code


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
    cloud_type: str = "ALL",
    container_disk_gb: int = 50,
    volume_gb: int = 0,
    volume_mount: str = "/workspace",
    min_vcpu: int = 2,
    min_memory_gb: int = 15,
    gpu_count: int = 1,
) -> str:
    """Create a stock RunPod pod via direct GraphQL with the C28 trap.

    Mirrors the input shape of
    ``RunPodProvider.create_instance`` (src/kinoforge/providers/runpod/__init__.py)
    so RunPod's GraphQL accepts the request and supply matching works
    (``cloudType=ALL`` matches secure OR community).

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
        run_id: Per-probe identifier; becomes the S3 prefix suffix +
            pod name.
        diag_bucket: Diagnostics S3 bucket name.
        cloud_type: ``"ALL"``/``"SECURE"``/``"COMMUNITY"``. Defaults to
            ``"ALL"`` to maximise supply.
        container_disk_gb: Ephemeral container disk size.
        volume_gb: Persistent volume size; 0 omits the field entirely.
        volume_mount: Volume mount path (ignored when ``volume_gb == 0``).
        min_vcpu: Minimum vCPU count requirement.
        min_memory_gb: Minimum RAM requirement in GiB.
        gpu_count: Number of GPUs per pod (default 1).

    Returns:
        Newly created pod ID.

    Raises:
        GraphQLError: When the response includes an ``errors`` array or
            ``data.podFindAndDeployOnDemand`` is null. ``GraphQLError.code``
            carries the ``extensions.code`` from the first error (e.g.
            ``"SUPPLY_CONSTRAINT"``) when present.
    """
    merged_env = dict(env)
    merged_env["KINOFORGE_DIAG_BUCKET"] = diag_bucket
    merged_env["KINOFORGE_DIAG_PREFIX"] = f"boot-logs/{run_id}"

    full_script = "\n".join([*_C28_TRAP_PREAMBLE_LINES, provision_script])
    docker_args = f'bash -c "{full_script}"'

    input_obj: dict[str, Any] = {
        "cloudType": cloud_type,
        "gpuCount": gpu_count,
        "containerDiskInGb": container_disk_gb,
        "minVcpuCount": min_vcpu,
        "minMemoryInGb": min_memory_gb,
        "gpuTypeId": gpu_type_id,
        "name": run_id,
        "imageName": image,
        "dockerArgs": docker_args,
        "env": [{"key": k, "value": v} for k, v in merged_env.items()],
    }
    if volume_gb > 0:
        input_obj["volumeInGb"] = volume_gb
        input_obj["volumeMountPath"] = volume_mount
    if ports is not None:
        input_obj["ports"] = ports

    result = client.execute(_CREATE_POD_MUTATION, {"input": input_obj})
    errors = result.get("errors") or []
    if errors:
        first = errors[0]
        code = (first.get("extensions") or {}).get("code")
        raise GraphQLError(str(first.get("message", "GraphQL error")), code=code)
    data = result.get("data") or {}
    deployed = data.get("podFindAndDeployOnDemand")
    if deployed is None:
        raise GraphQLError(
            "podFindAndDeployOnDemand returned null with no errors block"
        )
    return str(deployed["id"])


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


# Inlined verbatim from src/kinoforge/engines/comfyui/__init__.py:1226-1274
# (ComfyUIEngine.render_provision kinoforge_download_helper). C30 must not
# touch production code (spec §2). If the source diverges, sync this constant.
_KINOFORGE_DOWNLOAD_HELPER_LINES: list[str] = [
    "_kinoforge_download() {",
    "  local url=$1; local out=$2",
    "  local expected_sha=${3:-}",
    "  local token_env=${4:-}",
    '  local token_val=""',
    '  if [ -n "$token_env" ] && [ -n "${!token_env:-}" ]; then',
    '    token_val="${!token_env}"',
    "  fi",
    "  local out_dir out_base",
    '  out_dir=$(dirname "$out")',
    '  out_base=$(basename "$out")',
    "  local attempt",
    "  for attempt in 1 2 3; do",
    "    if command -v aria2c >/dev/null 2>&1; then",
    "      local ar_args=(-x16 -s16 --allow-overwrite=true "
    "--continue=true --console-log-level=warn "
    "--summary-interval=30)",
    '      [ -n "$token_val" ] && ar_args+=('
    '--header="Authorization: Bearer $token_val")',
    '      [ -n "$expected_sha" ] && ar_args+=(--checksum=sha-256=$expected_sha)',
    '      if aria2c "${ar_args[@]}" -d "$out_dir" -o "$out_base" "$url"; then',
    "        return 0",
    "      fi",
    "    else",
    '      rm -f "${out}.partial"',
    "      local cu_args=()",
    '      [ -n "$token_val" ] && cu_args+=(-H "Authorization: Bearer $token_val")',
    '      if curl -L --fail --retry 0 -C - "${cu_args[@]}" '
    '"$url" -o "${out}.partial"; then',
    '        if [ -n "$expected_sha" ]; then',
    "          local actual",
    "          actual=$(sha256sum \"${out}.partial\" | awk '{print $1}')",
    '          if [ "$actual" != "$expected_sha" ]; then',
    "            sleep $((5 * attempt))",
    "            continue",
    "          fi",
    "        fi",
    '        mv "${out}.partial" "$out"',
    "        return 0",
    "      fi",
    "    fi",
    "    sleep $((5 * attempt))",
    "  done",
    "  return 1",
    "}",
]


# Sourced from tests/live/cfg_c28_phase_a_diagnostic.yaml — the C28 Phase A
# v5 cfg. Tuples: (repo_url, ref_sha).
_C28_PHASE_A_CUSTOM_NODES: tuple[tuple[str, str], ...] = (
    (
        "https://github.com/kijai/ComfyUI-WanVideoWrapper",
        "088128b224242e110d3906c6750e9a3a348a659b",
    ),
    (
        "https://github.com/kijai/ComfyUI-KJNodes",
        "369c8aee9ad4641823d0ffd7035076bcd297b6f2",
    ),
    (
        "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite",
        "4ee72c065db22c9d96c2427954dc69e7b908444b",
    ),
)


# Sourced from tests/live/cfg_c28_phase_a_diagnostic.yaml `models:` block.
# Resolved against Kijai/WanVideo_comfy HF repo. Targets per ComfyUI
# TARGET_TO_SUBDIR mapping (engines/comfyui/__init__.py).
# Tuples: (url, subdir_relative_to_ComfyUI, filename).
_C28_PHASE_A_MODELS: tuple[tuple[str, str, str], ...] = (
    (
        "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/"
        "Wan2_1-T2V-1_3B_fp8_e4m3fn.safetensors",
        "models/diffusion_models",
        "Wan2_1-T2V-1_3B_fp8_e4m3fn.safetensors",
    ),
    (
        "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/"
        "Wan2_1_VAE_bf16.safetensors",
        "models/vae",
        "Wan2_1_VAE_bf16.safetensors",
    ),
    (
        "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/"
        "umt5-xxl-enc-fp8_e4m3fn.safetensors",
        "models/text_encoders",
        "umt5-xxl-enc-fp8_e4m3fn.safetensors",
    ),
)


def _custom_node_lines(url: str, ref: str) -> list[str]:
    """Mirror engines/comfyui/__init__.py:1376-1389 for a single pinned node."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return [
        f"[ ! -d custom_nodes/{name} ] && "
        f"git clone {url} custom_nodes/{name} && "
        f"cd custom_nodes/{name} && git checkout {ref} && cd ../..",
        f"[ -f custom_nodes/{name}/requirements.txt ] && "
        f"pip install -q -r custom_nodes/{name}/requirements.txt || true",
    ]


def _model_download_lines(url: str, subdir: str, filename: str) -> list[str]:
    """Mirror engines/comfyui/__init__.py:1421-1427 for a single model entry.

    No sha256 verification (HF redirects make it brittle); token env is
    always ``HF_TOKEN`` for this repo. Empty third argument signals
    no-sha to the helper.
    """
    return [
        f"mkdir -p {subdir}",
        f"[ ! -f {subdir}/{filename} ] && "
        f"_kinoforge_download '{url}' '{subdir}/{filename}' '' 'HF_TOKEN'",
    ]


# A2: stock pod, cd, sleep — exercises selfterm-free pre-amble only.
PROVISION_A2_LINES: list[str] = [
    "cd /workspace",
    "sleep 600",
]


# A3: A2 + ComfyUI clone (mirrors engines/comfyui/__init__.py:1358-1361).
PROVISION_A3_LINES: list[str] = [
    "cd /workspace",
    "[ ! -d ComfyUI ] && git clone --depth 1 --branch master "
    "https://github.com/comfyanonymous/ComfyUI ComfyUI",
    "sleep 600",
]


# A4: A3 + ComfyUI requirements pip install (line 1362).
PROVISION_A4_LINES: list[str] = [
    "cd /workspace",
    "[ ! -d ComfyUI ] && git clone --depth 1 --branch master "
    "https://github.com/comfyanonymous/ComfyUI ComfyUI",
    "cd ComfyUI && pip install -q -r requirements.txt",
    "cd /workspace",
    "sleep 600",
]


def _build_a5_lines() -> list[str]:
    lines: list[str] = [
        "cd /workspace",
        "[ ! -d ComfyUI ] && git clone --depth 1 --branch master "
        "https://github.com/comfyanonymous/ComfyUI ComfyUI",
        "cd ComfyUI && pip install -q -r requirements.txt",
        "cd /workspace",
        "cd /workspace/ComfyUI",
    ]
    for url, ref in _C28_PHASE_A_CUSTOM_NODES:
        lines.extend(_custom_node_lines(url, ref))
    lines.append("sleep 600")
    return lines


# A5: A4 + three pinned C28-Phase-A custom-node clones + their pip installs.
PROVISION_A5_LINES: list[str] = _build_a5_lines()


def _build_a6_lines() -> list[str]:
    lines: list[str] = list(_KINOFORGE_DOWNLOAD_HELPER_LINES)
    lines.extend(
        [
            "cd /workspace",
            "[ ! -d ComfyUI ] && git clone --depth 1 --branch master "
            "https://github.com/comfyanonymous/ComfyUI ComfyUI",
            "cd ComfyUI && pip install -q -r requirements.txt",
            "cd /workspace",
            "cd /workspace/ComfyUI",
        ]
    )
    for url, ref in _C28_PHASE_A_CUSTOM_NODES:
        lines.extend(_custom_node_lines(url, ref))
    for url, subdir, filename in _C28_PHASE_A_MODELS:
        lines.extend(_model_download_lines(url, subdir, filename))
    lines.append(
        "cd /workspace/ComfyUI && exec python main.py --listen 0.0.0.0 --port 8188"
    )
    return lines


# A6: A5 minus sleep + download helper + three Wan models + ComfyUI exec.
PROVISION_A6_LINES: list[str] = _build_a6_lines()
