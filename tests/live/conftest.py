"""Session-scoped .env loader for live tests.

Live-test modules gate themselves on ``os.getenv("KINOFORGE_LIVE_TESTS")
== "1"`` plus per-provider credential env vars (RUNPOD_API_KEY,
HF_TOKEN, etc.).  Those checks run at module import time during pytest
collection — before any kinoforge code, and before pixi's
``[activation.env]`` sees the test process.  Without this loader the
operator has to ``source .env`` (or otherwise export every key)
before running ``pixi run pytest tests/live/...``, which is brittle
and quietly skips tests when forgotten.

Loading is silent if ``.env`` is absent or the kinoforge package isn't
importable for any reason — tests then fall back to whatever the
operator has exported in the shell.  ``override`` is left ``False`` so
explicit exports always beat the file.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest

from kinoforge.providers.runpod.util import _default_http_post

_REPO_ROOT = Path(__file__).resolve().parents[2]

try:
    from kinoforge.core.dotenv_loader import load_env_file
except Exception:  # noqa: BLE001
    load_env_file = None  # type: ignore[assignment]

if load_env_file is not None:
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        load_env_file(env_file)


# ---------------------------------------------------------------------------
# C30 fault-isolation live-test shared fixtures + helpers.
# ---------------------------------------------------------------------------

C30_LEDGER = Path(__file__).parent / "_c30_spend_ledger.json"
C30_DIAG_BUCKET = "kinoforge-pod-diagnostics"
C30_HARD_CAP_USD = 1.50
C30_PER_PROBE_CAP_USD = 0.10
# Ranked cheap-GPU candidates with on-demand cents/hr (community cloud).
# create_probe_pod iterates until one succeeds; supply-constrained GPUs
# (e.g. RTX A2000) are absent here intentionally. Pricing snapshot
# 2026-06-15 from runpod.io GraphQL gpuTypes.lowestPrice; refresh if
# upstream rates change. All listed cents/hr ≤ 23 keeps a 10-min probe
# under the C33 per-probe cap of $0.05.
C30_GPU_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("NVIDIA RTX A5000", 16),
    ("NVIDIA GeForce RTX 3080", 17),
    ("NVIDIA RTX A4000", 17),
    ("NVIDIA GeForce RTX 4070 Ti", 19),
    ("NVIDIA RTX A4500", 19),
    ("NVIDIA GeForce RTX 3090", 22),
    ("Tesla V100-SXM2-16GB", 23),
)
C30_GRAPHQL_URL = "https://api.runpod.io/graphql"


class _C30GraphQLClient:
    """Minimal RunPod GraphQL client with ``execute(query, variables) -> dict``.

    Wraps ``_default_http_post`` from kinoforge.providers.runpod.util so the
    Bearer auth header + JSON encode/decode logic stays one source of truth.
    """

    def __init__(self, api_key: str, url: str = C30_GRAPHQL_URL) -> None:
        self._post = _default_http_post(api_key)
        self._url = url

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        return self._post(self._url, payload)


@pytest.fixture(scope="session", autouse=False)
def c30_preflight() -> None:
    """Run ``pixi run preflight`` once per session before any live spend."""
    result = subprocess.run(
        ["pixi", "run", "preflight"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"preflight failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture
def c30_client(c30_preflight: None) -> _C30GraphQLClient:
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        pytest.skip("RUNPOD_API_KEY not set")
    return _C30GraphQLClient(api_key=api_key)


@pytest.fixture
def c30_s3() -> Any:
    return boto3.client("s3")


def c30_run_id(phase: str) -> str:
    return f"c30-{phase}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"


def c30_estimate_spend(elapsed_s: float, cents_per_hr: int) -> float:
    return (elapsed_s / 3600.0) * (cents_per_hr / 100.0)


def c30_sidecar_path(phase: str) -> Path:
    return Path(__file__).parent / f"_c30_phase_{phase}_evidence.json"


def c30_read_predecessor(phase: str) -> dict[str, Any] | None:
    p = c30_sidecar_path(phase)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def c30_write_sidecar(phase: str, payload: dict[str, Any]) -> None:
    p = c30_sidecar_path(phase)
    p.write_text(json.dumps(payload, indent=2) + "\n")


def c30_execute_phase(
    client: Any,
    s3: Any,
    *,
    phase: str,
    image: str,
    ports: str | None,
    provision_script: str,
    env: dict[str, str],
    window_s: int = 600,
    interval_s: int = 30,
) -> str:
    """Run one C30 probe end-to-end. Returns the resulting Verdict value.

    Orchestrates the full 8-step flow (cap-check → create pod →
    register destroy atexit → poll → S3 count → classify → ledger
    append → sidecar write → final destroy). Caller is responsible for
    the predecessor-sidecar gate decision BEFORE calling this.

    Iterates over ``C30_GPU_CANDIDATES`` until a pod-create succeeds; if
    every candidate hits ``SUPPLY_CONSTRAINT``, raises the last error.
    """
    import atexit

    from kinoforge.diagnostics.c30_probe import (
        GraphQLError,
        PodStatusPoller,
        append_spend_entry,
        assert_under_cap,
        classify_run,
        count_trap_fires,
        create_probe_pod,
        destroy_with_retry,
    )

    assert_under_cap(C30_LEDGER, hard_cap_usd=C30_HARD_CAP_USD)

    run_id = c30_run_id(phase)
    pod_id: str | None = None
    gpu_type_id_used = ""
    cents_per_hr_used = 0
    last_err: GraphQLError | None = None
    for candidate_id, candidate_cents in C30_GPU_CANDIDATES:
        try:
            pod_id = create_probe_pod(
                client,
                image=image,
                ports=ports,
                provision_script=provision_script,
                env=env,
                gpu_type_id=candidate_id,
                run_id=run_id,
                diag_bucket=C30_DIAG_BUCKET,
            )
        except GraphQLError as exc:
            _msg = str(exc).lower()
            if (
                exc.code == "SUPPLY_CONSTRAINT"
                or "resources to deploy" in _msg
                or "instances available" in _msg
            ):
                last_err = exc
                continue
            raise
        gpu_type_id_used = candidate_id
        cents_per_hr_used = candidate_cents
        break
    if pod_id is None:
        assert last_err is not None
        raise last_err

    def _safe_destroy() -> None:
        try:
            destroy_with_retry(client, pod_id=pod_id, attempts=5, sleep_s=3)
        except Exception:  # noqa: BLE001
            # atexit must never raise; the operator's external guardian
            # is the last line of defense for leaked pods.
            pass

    atexit.register(_safe_destroy)

    start_iso = datetime.now().astimezone().isoformat()
    start_t = datetime.now().timestamp()
    trail = PodStatusPoller(
        client, pod_id=pod_id, window_s=window_s, interval_s=interval_s
    ).poll()
    end_t = datetime.now().timestamp()
    end_iso = datetime.now().astimezone().isoformat()

    fire_count = count_trap_fires(s3, C30_DIAG_BUCKET, f"boot-logs/{run_id}/")
    verdict = classify_run(trail, fire_count)

    elapsed = end_t - start_t
    spend = c30_estimate_spend(elapsed, cents_per_hr_used)

    append_spend_entry(
        C30_LEDGER,
        {
            "phase": phase,
            "pod_id": pod_id,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "start_ts": start_iso,
            "end_ts": end_iso,
            "est_spend_usd": round(spend, 6),
        },
    )
    c30_write_sidecar(
        phase,
        {
            "phase": phase,
            "verdict": verdict.value,
            "run_id": run_id,
            "pod_id": pod_id,
            "image": image,
            "ports": ports,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "s3_prefix": f"boot-logs/{run_id}/",
            "fire_count": fire_count,
            "poll_trail": trail,
            "est_spend_usd": round(spend, 6),
            "captured_at": end_iso,
        },
    )

    destroy_with_retry(client, pod_id=pod_id, attempts=5, sleep_s=3)
    return verdict.value


# ---------------------------------------------------------------------------
# C33 — podEditJob restart-cause investigation. Reuses _C30GraphQLClient,
# c30_client, c30_s3, c30_preflight, C30_LEDGER, C30_DIAG_BUCKET,
# C30_GPU_CANDIDATES. Spec:
# docs/superpowers/specs/2026-06-15-podeditjob-restart-investigation-design.md
# ---------------------------------------------------------------------------

C33_HARD_CAP_USD = 5.00
C33_PER_PROBE_CAP_USD = 0.05
C33_IMAGE = "ubuntu:22.04"


def c33_sidecar_path(phase: str) -> Path:
    """Return the absolute path to the C33 sidecar JSON for ``phase``."""
    return Path(__file__).parent / f"_c33_probe_{phase}_evidence.json"


def c33_run_id(phase: str) -> str:
    """Return a unique C33 run id keyed to phase + local timestamp."""
    return f"c33-{phase}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"


def _c33_count_advances(samples: list[str | None]) -> int:
    """Count strictly-increasing transitions in a sequence of ISO strings."""
    advances = 0
    seen: str | None = None
    for v in samples:
        if v is None:
            continue
        if seen is not None and v > seen:
            advances += 1
        seen = v if (seen is None or v >= seen) else seen
    return advances


def _c33_count_negative_uptimes(samples: list[int | None]) -> int:
    """Count samples with strictly-negative uptime."""
    return sum(1 for u in samples if u is not None and u < 0)


def _c33_count_null_uptimes(samples: list[int | None]) -> int:
    """Count samples missing an uptime value."""
    return sum(1 for u in samples if u is None)


def c33_execute_p0(
    client: Any,  # noqa: ANN401
    s3: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """Run C33 P0 orphan-disambiguation probe end-to-end."""
    import atexit

    from kinoforge.diagnostics.c30_probe import (
        GraphQLError,
        PodStatusPollerExtended,
        _classify_p0,
        append_spend_entry,
        assert_under_cap,
        count_trap_fires,
        create_probe_pod,
        destroy_with_retry,
    )

    assert_under_cap(C30_LEDGER, hard_cap_usd=C33_HARD_CAP_USD)

    phase = "p0"
    run_id = c33_run_id(phase)
    pod_id: str | None = None
    gpu_type_id_used = ""
    cents_per_hr_used = 0
    last_err: GraphQLError | None = None
    for candidate_id, candidate_cents in C30_GPU_CANDIDATES:
        try:
            pod_id = create_probe_pod(
                client,
                image=C33_IMAGE,
                ports=None,
                provision_script="sleep 600",
                env={},
                gpu_type_id=candidate_id,
                run_id=run_id,
                diag_bucket=C30_DIAG_BUCKET,
            )
        except GraphQLError as exc:
            _msg = str(exc).lower()
            if (
                exc.code == "SUPPLY_CONSTRAINT"
                or "resources to deploy" in _msg
                or "instances available" in _msg
            ):
                last_err = exc
                continue
            raise
        gpu_type_id_used = candidate_id
        cents_per_hr_used = candidate_cents
        break
    if pod_id is None:
        assert last_err is not None
        raise last_err

    bound_pod_id: str = pod_id

    def _safe_destroy() -> None:
        try:
            destroy_with_retry(client, pod_id=bound_pod_id, attempts=5, sleep_s=3)
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_safe_destroy)

    start_iso = datetime.now().astimezone().isoformat()
    start_t = datetime.now().timestamp()
    trail = PodStatusPollerExtended(
        client=client, pod_id=bound_pod_id, window_s=600, interval_s=30
    ).poll()
    end_t = datetime.now().timestamp()
    end_iso = datetime.now().astimezone().isoformat()

    fire_count = count_trap_fires(s3, C30_DIAG_BUCKET, f"boot-logs/{run_id}/")
    last_started_samples = [t[2] for t in trail]
    uptime_samples = [t[1] for t in trail]
    n_adv = _c33_count_advances(last_started_samples)
    n_neg = _c33_count_negative_uptimes(uptime_samples)
    n_null = _c33_count_null_uptimes(uptime_samples)
    sidecar = {
        "phase": phase,
        "run_id": run_id,
        "pod_id": bound_pod_id,
        "image": C33_IMAGE,
        "gpu_type_id": gpu_type_id_used,
        "cents_per_hr": cents_per_hr_used,
        "s3_prefix": f"boot-logs/{run_id}/",
        "fire_count": fire_count,
        "poll_trail": trail,
        "n_last_started_at_advances": n_adv,
        "n_negative_uptime_samples": n_neg,
        "n_null_uptime_samples": n_null,
        "verdict": _classify_p0(
            {
                "n_last_started_at_advances": n_adv,
                "n_negative_uptime_samples": n_neg,
            }
        ).value,
        "est_spend_usd": round(
            c30_estimate_spend(end_t - start_t, cents_per_hr_used), 6
        ),
        "captured_at": end_iso,
    }

    append_spend_entry(
        C30_LEDGER,
        {
            "phase": phase,
            "pod_id": bound_pod_id,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "start_ts": start_iso,
            "end_ts": end_iso,
            "est_spend_usd": sidecar["est_spend_usd"],
        },
    )
    c33_sidecar_path(phase).write_text(json.dumps(sidecar, indent=2) + "\n")

    destroy_with_retry(client, pod_id=bound_pod_id, attempts=5, sleep_s=3)
    return sidecar


def c33_execute_p1(
    client: Any,  # noqa: ANN401
    s3: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """Run C33 P1 main-hypothesis A/B probe end-to-end."""
    import atexit
    import time as _time

    from kinoforge.diagnostics.c30_probe import (
        GraphQLError,
        PodStatusPollerExtended,
        _classify_p1,
        append_spend_entry,
        assert_under_cap,
        create_probe_pod,
        destroy_with_retry,
        issue_single_pod_edit_job,
    )
    from kinoforge.providers.runpod.heartbeat import _merge_marker

    _ = s3  # unused — kept for fixture-signature symmetry with c33_execute_p0
    assert_under_cap(C30_LEDGER, hard_cap_usd=C33_HARD_CAP_USD)

    phase = "p1"
    run_id = c33_run_id(phase)
    pod_id: str | None = None
    gpu_type_id_used = ""
    cents_per_hr_used = 0
    last_err: GraphQLError | None = None
    for candidate_id, candidate_cents in C30_GPU_CANDIDATES:
        try:
            pod_id = create_probe_pod(
                client,
                image=C33_IMAGE,
                ports=None,
                provision_script="sleep 600",
                env={},
                gpu_type_id=candidate_id,
                run_id=run_id,
                diag_bucket=C30_DIAG_BUCKET,
            )
        except GraphQLError as exc:
            _msg = str(exc).lower()
            if (
                exc.code == "SUPPLY_CONSTRAINT"
                or "resources to deploy" in _msg
                or "instances available" in _msg
            ):
                last_err = exc
                continue
            raise
        gpu_type_id_used = candidate_id
        cents_per_hr_used = candidate_cents
        break
    if pod_id is None:
        assert last_err is not None
        raise last_err

    bound_pod_id: str = pod_id

    def _safe_destroy() -> None:
        try:
            destroy_with_retry(client, pod_id=bound_pod_id, attempts=5, sleep_s=3)
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_safe_destroy)

    start_iso = datetime.now().astimezone().isoformat()
    start_t = datetime.now().timestamp()

    base_docker_args: str | None = None
    t0_uptime: int | None = None
    t0_last_started_at: str | None = None
    pre_stabilize_trail: list[tuple[float, int | None, str | None, str | None]] = []
    deadline_t = start_t + 600
    running_since_t: float | None = None
    stable_reason: str | None = None
    while _time.time() < deadline_t:
        _time.sleep(15)
        now_t = _time.time()
        sample = PodStatusPollerExtended(
            client=client, pod_id=bound_pod_id, window_s=0, interval_s=15
        ).poll()
        _elapsed, uptime, last_started_at, status = sample[0]
        pre_stabilize_trail.append((now_t - start_t, uptime, last_started_at, status))

        uptime_gate = uptime is not None and uptime >= 90
        if status == "RUNNING":
            if running_since_t is None:
                running_since_t = now_t
            status_gate = (now_t - running_since_t) >= 90
        else:
            running_since_t = None
            status_gate = False

        if uptime_gate or status_gate:
            t0_uptime = uptime
            t0_last_started_at = last_started_at
            stable_reason = "uptime>=90" if uptime_gate else "status=RUNNING for >=90s"
            q = (
                f'query {{ pod(input: {{ podId: "{bound_pod_id}" }}) '
                "{ dockerArgs } }"
            )
            r = client.execute(q, {})
            pod_data = (r.get("data") or {}).get("pod") or {}
            base_docker_args = str(pod_data.get("dockerArgs") or "")
            break

    if base_docker_args is None:
        end_iso = datetime.now().astimezone().isoformat()
        end_t = datetime.now().timestamp()
        sidecar_abort: dict[str, Any] = {
            "phase": phase,
            "run_id": run_id,
            "pod_id": bound_pod_id,
            "image": C33_IMAGE,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "s3_prefix": f"boot-logs/{run_id}/",
            "verdict": "ambiguous",
            "abort_reason": "pod_failed_to_stabilize_in_600s",
            "pre_stabilize_trail": pre_stabilize_trail,
            "est_spend_usd": round(
                c30_estimate_spend(end_t - start_t, cents_per_hr_used), 6
            ),
            "captured_at": end_iso,
        }
        append_spend_entry(
            C30_LEDGER,
            {
                "phase": phase,
                "pod_id": bound_pod_id,
                "gpu_type_id": gpu_type_id_used,
                "cents_per_hr": cents_per_hr_used,
                "start_ts": start_iso,
                "end_ts": end_iso,
                "est_spend_usd": sidecar_abort["est_spend_usd"],
            },
        )
        c33_sidecar_path(phase).write_text(json.dumps(sidecar_abort, indent=2) + "\n")
        destroy_with_retry(client, pod_id=bound_pod_id, attempts=5, sleep_s=3)
        return sidecar_abort

    t0_snapshot_at_iso = datetime.now().astimezone().isoformat()
    mut_ts = datetime.now().astimezone()
    new_docker_args = _merge_marker(base_docker_args, mut_ts)
    mutation_issued_at_iso = datetime.now().astimezone().isoformat()
    mutation_response = issue_single_pod_edit_job(
        client, pod_id=bound_pod_id, new_docker_args=new_docker_args
    )

    post_trail = PodStatusPollerExtended(
        client=client, pod_id=bound_pod_id, window_s=90, interval_s=10
    ).poll()

    advanced = any(
        t[2] is not None
        and t0_last_started_at is not None
        and t[2] > t0_last_started_at
        for t in post_trail
    )
    advance_first_at: float | None = next(
        (
            float(t[0])
            for t in post_trail
            if t[2] is not None
            and t0_last_started_at is not None
            and t[2] > t0_last_started_at
        ),
        None,
    )
    reset = (
        any(t[1] is not None and t[1] < t0_uptime for t in post_trail)
        if t0_uptime is not None
        else False
    )

    non_null_uptimes = [t[1] for t in post_trail if t[1] is not None]
    monotonic = all(
        curr >= prev - 2
        for prev, curr in zip(non_null_uptimes, non_null_uptimes[1:], strict=False)
    )

    end_iso = datetime.now().astimezone().isoformat()
    end_t = datetime.now().timestamp()
    sidecar: dict[str, Any] = {
        "phase": phase,
        "run_id": run_id,
        "pod_id": bound_pod_id,
        "image": C33_IMAGE,
        "gpu_type_id": gpu_type_id_used,
        "cents_per_hr": cents_per_hr_used,
        "s3_prefix": f"boot-logs/{run_id}/",
        "t0_last_started_at": t0_last_started_at,
        "t0_uptime": t0_uptime,
        "t0_snapshot_at": t0_snapshot_at_iso,
        "stable_reason": stable_reason,
        "pre_stabilize_trail": pre_stabilize_trail,
        "mutation_issued_at": mutation_issued_at_iso,
        "mutation_response": mutation_response,
        "post_mutation_trail": post_trail,
        "last_started_at_advanced": advanced,
        "last_started_at_advance_observed_at_elapsed_s": advance_first_at,
        "uptime_reset_observed": reset,
        "uptime_monotonic_for_90s": monotonic,
        "verdict": _classify_p1(
            {
                "last_started_at_advanced": advanced,
                "uptime_reset_observed": reset,
                "uptime_monotonic_for_90s": monotonic,
            }
        ).value,
        "est_spend_usd": round(
            c30_estimate_spend(end_t - start_t, cents_per_hr_used), 6
        ),
        "captured_at": end_iso,
    }

    append_spend_entry(
        C30_LEDGER,
        {
            "phase": phase,
            "pod_id": bound_pod_id,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "start_ts": start_iso,
            "end_ts": end_iso,
            "est_spend_usd": sidecar["est_spend_usd"],
        },
    )
    c33_sidecar_path(phase).write_text(json.dumps(sidecar, indent=2) + "\n")
    destroy_with_retry(client, pod_id=bound_pod_id, attempts=5, sleep_s=3)
    return sidecar


# ---------------------------------------------------------------------------
# C33 Q1 — top-level `uptimeSeconds` vs wall-clock estimate.
# C33 Q2 — P0-style probe on cloudType=SECURE.
#
# Filed 2026-06-15 to substantiate (or refute) the C33 closeout claim that
# `runtime.uptimeInSeconds` is "broken" on RunPod community cloud. Q1 tests
# whether the top-level `Pod.uptimeSeconds` field (per RunPod GraphQL spec)
# is reliable on the same tier, and whether `now_utc - lastStartedAt` makes
# a sound fallback. Q2 tests whether the uptime-field weirdness is a
# community-cloud quirk by repeating P0 on `cloudType=SECURE`.
# ---------------------------------------------------------------------------

C33_Q1_WINDOW_S = 300  # 5 min — Q1 is cheaper than P0 because we only need
C33_Q1_INTERVAL_S = 30  # to test field agreement, not survival statistics.

_POD_DUAL_UPTIME_QUERY = (
    'query {{ pod(input: {{ podId: "{pod_id}" }}) '
    "{{ id desiredStatus lastStartedAt uptimeSeconds "
    "runtime {{ uptimeInSeconds }} }} }}"
)

# Top-cheap SECURE-cloud candidates, snapshot 2026-06-15 from
# `gpuTypes.lowestPrice(input: { gpuCount: 1, secureCloud: true })`. With
# a 7-min Q2 window, A40 @ 44 c/hr is the cap-edge candidate
# (44 c/hr * 7/60 h = $0.0513).
C33_Q2_GPU_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("NVIDIA RTX A4000", 25),
    ("NVIDIA RTX 4000 Ada Generation", 26),
    ("NVIDIA L4", 39),
    ("NVIDIA A40", 44),
)
C33_Q2_WINDOW_S = 420  # 7 min — keeps the worst-case candidate (A40 @ 44 c/hr)
C33_Q2_INTERVAL_S = 30  # within the relaxed Q2 per-probe cap of $0.06.


def _parse_last_started_at_utc(iso_z: str) -> datetime:
    """Parse a RunPod ``lastStartedAt`` Z-suffixed ISO string to aware UTC."""

    if iso_z.endswith("Z"):
        iso_z = iso_z[:-1] + "+00:00"
    return datetime.fromisoformat(iso_z).astimezone(UTC)


def c33_execute_q1(
    client: Any,  # noqa: ANN401
    s3: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """Run C33 Q1 dual-uptime probe end-to-end."""
    import atexit
    import time as _time

    from kinoforge.diagnostics.c30_probe import (
        GraphQLError,
        append_spend_entry,
        assert_under_cap,
        create_probe_pod,
        destroy_with_retry,
    )

    _ = s3  # unused — kept for fixture symmetry
    assert_under_cap(C30_LEDGER, hard_cap_usd=C33_HARD_CAP_USD)

    phase = "q1"
    run_id = c33_run_id(phase)
    pod_id: str | None = None
    gpu_type_id_used = ""
    cents_per_hr_used = 0
    last_err: GraphQLError | None = None
    for candidate_id, candidate_cents in C30_GPU_CANDIDATES:
        try:
            pod_id = create_probe_pod(
                client,
                image=C33_IMAGE,
                ports=None,
                provision_script="sleep 600",
                env={},
                gpu_type_id=candidate_id,
                run_id=run_id,
                diag_bucket=C30_DIAG_BUCKET,
            )
        except GraphQLError as exc:
            _msg = str(exc).lower()
            if (
                exc.code == "SUPPLY_CONSTRAINT"
                or "resources to deploy" in _msg
                or "instances available" in _msg
            ):
                last_err = exc
                continue
            raise
        gpu_type_id_used = candidate_id
        cents_per_hr_used = candidate_cents
        break
    if pod_id is None:
        assert last_err is not None
        raise last_err

    bound_pod_id: str = pod_id

    def _safe_destroy() -> None:
        try:
            destroy_with_retry(client, pod_id=bound_pod_id, attempts=5, sleep_s=3)
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_safe_destroy)

    start_iso = datetime.now().astimezone().isoformat()
    start_t = datetime.now().timestamp()
    n_intervals = C33_Q1_WINDOW_S // C33_Q1_INTERVAL_S
    n_samples = n_intervals + 1
    samples: list[dict[str, Any]] = []
    for i in range(n_samples):
        sampled_at_utc = datetime.now(UTC)
        q = _POD_DUAL_UPTIME_QUERY.format(pod_id=bound_pod_id)
        try:
            r = client.execute(q, {})
        except Exception as exc:  # noqa: BLE001
            samples.append(
                {
                    "sampled_at_utc": sampled_at_utc.isoformat(),
                    "error": str(exc),
                }
            )
            if i < n_samples - 1:
                _time.sleep(C33_Q1_INTERVAL_S)
            continue
        pod_data = (r.get("data") or {}).get("pod") or {}
        nested = (pod_data.get("runtime") or {}).get("uptimeInSeconds")
        top = pod_data.get("uptimeSeconds")
        lsa = pod_data.get("lastStartedAt")
        status = pod_data.get("desiredStatus")
        estimated_s: float | None
        if lsa is not None:
            try:
                lsa_utc = _parse_last_started_at_utc(str(lsa))
                estimated_s = (sampled_at_utc - lsa_utc).total_seconds()
            except (ValueError, TypeError):
                estimated_s = None
        else:
            estimated_s = None
        samples.append(
            {
                "sampled_at_utc": sampled_at_utc.isoformat(),
                "desiredStatus": status,
                "lastStartedAt": lsa,
                "runtime.uptimeInSeconds": nested,
                "top.uptimeSeconds": top,
                "estimated_uptime_s_from_lastStartedAt": estimated_s,
            }
        )
        if i < n_samples - 1:
            _time.sleep(C33_Q1_INTERVAL_S)

    def _agreement(field_key: str) -> dict[str, Any]:
        diffs: list[float] = []
        compared = 0
        for s in samples:
            v = s.get(field_key)
            est = s.get("estimated_uptime_s_from_lastStartedAt")
            if v is None or est is None:
                continue
            diffs.append(abs(float(v) - float(est)))
            compared += 1
        if not diffs:
            return {"compared": 0, "max_abs_diff_s": None, "mean_abs_diff_s": None}
        return {
            "compared": compared,
            "max_abs_diff_s": max(diffs),
            "mean_abs_diff_s": sum(diffs) / len(diffs),
        }

    nested_agreement = _agreement("runtime.uptimeInSeconds")
    top_agreement = _agreement("top.uptimeSeconds")
    n_top_present = sum(1 for s in samples if s.get("top.uptimeSeconds") is not None)
    n_nested_present = sum(
        1 for s in samples if s.get("runtime.uptimeInSeconds") is not None
    )
    n_nested_negative = sum(
        1
        for s in samples
        if (v := s.get("runtime.uptimeInSeconds")) is not None and v < 0
    )
    n_top_negative = sum(
        1 for s in samples if (v := s.get("top.uptimeSeconds")) is not None and v < 0
    )

    end_t = datetime.now().timestamp()
    end_iso = datetime.now().astimezone().isoformat()
    sidecar: dict[str, Any] = {
        "phase": phase,
        "run_id": run_id,
        "pod_id": bound_pod_id,
        "image": C33_IMAGE,
        "gpu_type_id": gpu_type_id_used,
        "cents_per_hr": cents_per_hr_used,
        "s3_prefix": f"boot-logs/{run_id}/",
        "window_s": C33_Q1_WINDOW_S,
        "interval_s": C33_Q1_INTERVAL_S,
        "samples": samples,
        "n_samples": len(samples),
        "n_runtime_uptime_present": n_nested_present,
        "n_runtime_uptime_negative": n_nested_negative,
        "n_top_uptime_present": n_top_present,
        "n_top_uptime_negative": n_top_negative,
        "agreement_nested_vs_estimate": nested_agreement,
        "agreement_top_vs_estimate": top_agreement,
        "est_spend_usd": round(
            c30_estimate_spend(end_t - start_t, cents_per_hr_used), 6
        ),
        "captured_at": end_iso,
    }
    append_spend_entry(
        C30_LEDGER,
        {
            "phase": phase,
            "pod_id": bound_pod_id,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "start_ts": start_iso,
            "end_ts": end_iso,
            "est_spend_usd": sidecar["est_spend_usd"],
        },
    )
    c33_sidecar_path(phase).write_text(json.dumps(sidecar, indent=2) + "\n")
    destroy_with_retry(client, pod_id=bound_pod_id, attempts=5, sleep_s=3)
    return sidecar


def c33_execute_q2(
    client: Any,  # noqa: ANN401
    s3: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """Run C33 Q2 P0-style probe on cloudType=SECURE."""
    import atexit

    from kinoforge.diagnostics.c30_probe import (
        GraphQLError,
        PodStatusPollerExtended,
        _classify_p0,
        append_spend_entry,
        assert_under_cap,
        count_trap_fires,
        create_probe_pod,
        destroy_with_retry,
    )

    assert_under_cap(C30_LEDGER, hard_cap_usd=C33_HARD_CAP_USD)

    phase = "q2"
    run_id = c33_run_id(phase)
    pod_id: str | None = None
    gpu_type_id_used = ""
    cents_per_hr_used = 0
    last_err: GraphQLError | None = None
    for candidate_id, candidate_cents in C33_Q2_GPU_CANDIDATES:
        try:
            pod_id = create_probe_pod(
                client,
                image=C33_IMAGE,
                ports=None,
                provision_script="sleep 600",
                env={},
                gpu_type_id=candidate_id,
                run_id=run_id,
                diag_bucket=C30_DIAG_BUCKET,
                cloud_type="SECURE",
            )
        except GraphQLError as exc:
            _msg = str(exc).lower()
            if (
                exc.code == "SUPPLY_CONSTRAINT"
                or "resources to deploy" in _msg
                or "instances available" in _msg
            ):
                last_err = exc
                continue
            raise
        gpu_type_id_used = candidate_id
        cents_per_hr_used = candidate_cents
        break
    if pod_id is None:
        assert last_err is not None
        raise last_err

    bound_pod_id: str = pod_id

    def _safe_destroy() -> None:
        try:
            destroy_with_retry(client, pod_id=bound_pod_id, attempts=5, sleep_s=3)
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_safe_destroy)

    start_iso = datetime.now().astimezone().isoformat()
    start_t = datetime.now().timestamp()
    trail = PodStatusPollerExtended(
        client=client,
        pod_id=bound_pod_id,
        window_s=C33_Q2_WINDOW_S,
        interval_s=C33_Q2_INTERVAL_S,
    ).poll()
    end_t = datetime.now().timestamp()
    end_iso = datetime.now().astimezone().isoformat()

    fire_count = count_trap_fires(s3, C30_DIAG_BUCKET, f"boot-logs/{run_id}/")
    last_started_samples = [t[2] for t in trail]
    uptime_samples = [t[1] for t in trail]
    n_adv = _c33_count_advances(last_started_samples)
    n_neg = _c33_count_negative_uptimes(uptime_samples)
    n_null = _c33_count_null_uptimes(uptime_samples)
    sidecar = {
        "phase": phase,
        "run_id": run_id,
        "pod_id": bound_pod_id,
        "image": C33_IMAGE,
        "cloud_type": "SECURE",
        "gpu_type_id": gpu_type_id_used,
        "cents_per_hr": cents_per_hr_used,
        "s3_prefix": f"boot-logs/{run_id}/",
        "fire_count": fire_count,
        "poll_trail": trail,
        "n_last_started_at_advances": n_adv,
        "n_negative_uptime_samples": n_neg,
        "n_null_uptime_samples": n_null,
        "verdict": _classify_p0(
            {
                "n_last_started_at_advances": n_adv,
                "n_negative_uptime_samples": n_neg,
            }
        ).value,
        "est_spend_usd": round(
            c30_estimate_spend(end_t - start_t, cents_per_hr_used), 6
        ),
        "captured_at": end_iso,
    }
    append_spend_entry(
        C30_LEDGER,
        {
            "phase": phase,
            "pod_id": bound_pod_id,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "start_ts": start_iso,
            "end_ts": end_iso,
            "est_spend_usd": sidecar["est_spend_usd"],
        },
    )
    c33_sidecar_path(phase).write_text(json.dumps(sidecar, indent=2) + "\n")
    destroy_with_retry(client, pod_id=bound_pod_id, attempts=5, sleep_s=3)
    return sidecar
