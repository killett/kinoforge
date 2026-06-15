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
from datetime import datetime
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
# 2026-06-14 from runpod.io GraphQL gpuTypes.lowestPrice; refresh if
# upstream rates change.
C30_GPU_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("NVIDIA GeForce RTX 3070", 13),
    ("NVIDIA GeForce RTX 3080", 17),
    ("NVIDIA GeForce RTX 3080 Ti", 18),
    ("NVIDIA RTX 4000 Ada Generation", 20),
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
            if (
                exc.code == "SUPPLY_CONSTRAINT"
                or "resources to deploy" in str(exc).lower()
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
            if (
                exc.code == "SUPPLY_CONSTRAINT"
                or "resources to deploy" in str(exc).lower()
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
            if (
                exc.code == "SUPPLY_CONSTRAINT"
                or "resources to deploy" in str(exc).lower()
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
    deadline_t = start_t + 300
    while _time.time() < deadline_t:
        _time.sleep(15)
        sample = PodStatusPollerExtended(
            client=client, pod_id=bound_pod_id, window_s=0, interval_s=15
        ).poll()
        _elapsed, uptime, last_started_at, _status = sample[0]
        if uptime is not None and uptime >= 90:
            t0_uptime = uptime
            t0_last_started_at = last_started_at
            q = (
                f'query {{ pod(input: {{ podId: "{bound_pod_id}" }}) '
                "{ dockerArgs } }"
            )
            r = client.execute(q, {})
            pod_data = (r.get("data") or {}).get("pod") or {}
            base_docker_args = str(pod_data.get("dockerArgs") or "")
            break

    if t0_uptime is None or base_docker_args is None:
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
            "abort_reason": "pod_failed_to_stabilize_in_300s",
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
    reset = any(t[1] is not None and t[1] < t0_uptime for t in post_trail)

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
