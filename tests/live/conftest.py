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
C30_GPU_TYPE_ID = "NVIDIA RTX A2000"
C30_GPU_CENTS_PER_HR = 10
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
    """
    import atexit

    from kinoforge.diagnostics.c30_probe import (
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
    pod_id = create_probe_pod(
        client,
        image=image,
        ports=ports,
        provision_script=provision_script,
        env=env,
        gpu_type_id=C30_GPU_TYPE_ID,
        run_id=run_id,
        diag_bucket=C30_DIAG_BUCKET,
    )

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
    spend = c30_estimate_spend(elapsed, C30_GPU_CENTS_PER_HR)

    append_spend_entry(
        C30_LEDGER,
        {
            "phase": phase,
            "pod_id": pod_id,
            "gpu_type_id": C30_GPU_TYPE_ID,
            "cents_per_hr": C30_GPU_CENTS_PER_HR,
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
            "s3_prefix": f"boot-logs/{run_id}/",
            "fire_count": fire_count,
            "poll_trail": trail,
            "est_spend_usd": round(spend, 6),
            "captured_at": end_iso,
        },
    )

    destroy_with_retry(client, pod_id=pod_id, attempts=5, sleep_s=3)
    return verdict.value
