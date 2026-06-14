"""C28 Phase A live smoke — diagnostic_mode trap captures boot log to S3.

Boots a real Wan + ComfyUI pod with ``diagnostic_mode: true`` so the A2
EXIT trap fires on container exit and uploads a diagnostic snapshot
(rc + last_line + system tables + boot.log tail) to S3. Test polls the
bucket for the snapshot, validates the required marker sections are
present, and writes ``_c28_phase_a_evidence.json`` with the captured
``rc_in_trap`` + ``last_line`` so the A5 classifier can match against
the spec §3 hypothesis table.

Retry policy: up to ``_MAX_BOOT_ATTEMPTS`` cold boots. If every attempt
succeeds, the spec directs Phase B + Phase C to ship unconditionally
(NO_REPRODUCTION outcome).

Gated by ``KINOFORGE_LIVE_RUNPOD=1``.
Live spend ceiling: ~$0.20 (one cold boot ~$0.05 + retry headroom).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.20
_CFG_PATH = Path("tests/live/cfg_c28_phase_a_diagnostic.yaml")
_PROMPT_PATH = Path("/workspace/prompt-field-realistic.txt")
_SIDECAR_PATH = Path("tests/live/_c28_phase_a_evidence.json")
_BUCKET = "kinoforge-pod-diagnostics"
_REGION = "us-west-2"
_GEN_TIMEOUT_S = 60.0 * 40.0  # 40 min — cold boot + first frame
_MAX_BOOT_ATTEMPTS = 3
_S3_POLL_MAX_ATTEMPTS = 60
_S3_POLL_INTERVAL_S = 5.0
_REQUIRED_MARKERS: tuple[str, ...] = (
    "===== rc =====",
    "===== last_line =====",
    "===== nvidia-smi =====",
    "===== df -h =====",
    "===== free -m =====",
    "===== ls -la models/diffusion_models =====",
    "===== dpkg -l torch =====",
    "===== boot.log =====",
)


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the C28 Phase A diagnostic smoke "
            f"(~${_BUDGET_USD_CAP} per invocation)",
        )


def _run_kinoforge_generate(
    *,
    state_dir: Path,
    prompt: str,
    run_id: str,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "KINOFORGE_DIAG_PREFIX": f"boot-logs/{run_id}"}
    return subprocess.run(  # noqa: S603
        [
            "pixi",
            "run",
            "kinoforge",
            "--state-dir",
            str(state_dir),
            "generate",
            "-c",
            str(_CFG_PATH),
            "--prompt",
            prompt,
            "--mode",
            "t2v",
            "--run-id",
            run_id,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=_GEN_TIMEOUT_S,
        check=False,
    )


def _poll_s3_for_diag(prefix: str) -> str | None:
    import boto3

    s3 = boto3.client("s3", region_name=_REGION)
    for _ in range(_S3_POLL_MAX_ATTEMPTS):
        resp = s3.list_objects_v2(Bucket=_BUCKET, Prefix=prefix)
        contents = resp.get("Contents") or []
        if contents:
            return str(contents[0]["Key"])
        time.sleep(_S3_POLL_INTERVAL_S)
    return None


def _read_s3(key: str) -> str:
    import boto3

    s3 = boto3.client("s3", region_name=_REGION)
    body_bytes = s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read()
    text: str = body_bytes.decode("utf-8", errors="replace")
    return text


def _section_after(body: str, marker: str) -> str:
    idx = body.index(marker) + len(marker)
    tail = body[idx:].splitlines()
    return tail[1].strip() if len(tail) > 1 else ""


def _destroy_safely(state_dir: Path, pod_id: str | None) -> None:
    if pod_id is None:
        return
    subprocess.run(  # noqa: S603
        [
            "pixi",
            "run",
            "kinoforge",
            "--state-dir",
            str(state_dir),
            "destroy",
            "--id",
            pod_id,
        ],
        check=False,
        timeout=180,
    )


def _extract_pod_id_from_ledger(state_dir: Path) -> str | None:
    ledger_path = state_dir / "ledger.json"
    if not ledger_path.exists():
        return None
    data = json.loads(ledger_path.read_text())
    for entry in data.get("entries", []):
        if entry.get("provider") == "runpod" and entry.get("id"):
            return str(entry["id"])
    return None


def test_c28_phase_a_diagnostic_capture_live(tmp_path: Path) -> None:
    """Cold-boot Wan + ComfyUI with diagnostic_mode; capture S3 snapshot."""
    prompt = _PROMPT_PATH.read_text().strip()
    state_dir = tmp_path / "kinoforge-state"
    state_dir.mkdir()
    attempts: list[dict[str, Any]] = []
    succeeded = 0

    for attempt in range(1, _MAX_BOOT_ATTEMPTS + 1):
        run_id = f"c28-phase-a-{datetime.now().strftime('%Y%m%dT%H%M%S')}-a{attempt}"
        prefix = f"boot-logs/{run_id}"
        proc = _run_kinoforge_generate(
            state_dir=state_dir,
            prompt=prompt,
            run_id=run_id,
        )
        pod_id = _extract_pod_id_from_ledger(state_dir)

        if proc.returncode == 0:
            succeeded += 1
            attempts.append(
                {
                    "attempt": attempt,
                    "run_id": run_id,
                    "rc": proc.returncode,
                    "s3_key": None,
                    "outcome": "clean-boot",
                },
            )
            _destroy_safely(state_dir, pod_id)
            continue

        obj_key = _poll_s3_for_diag(prefix)
        attempts.append(
            {
                "attempt": attempt,
                "run_id": run_id,
                "rc": proc.returncode,
                "s3_key": obj_key,
                "outcome": "captured" if obj_key else "rc-nonzero-no-s3",
                "stderr_tail": proc.stderr[-1000:] if proc.stderr else "",
            },
        )
        _destroy_safely(state_dir, pod_id)

        if obj_key is None:
            continue

        body = _read_s3(obj_key)
        missing = [m for m in _REQUIRED_MARKERS if m not in body]
        assert not missing, (
            f"attempt {attempt}: S3 snapshot missing markers: {missing}\n"
            f"body[:2000]:\n{body[:2000]}"
        )
        rc_value = _section_after(body, "===== rc =====")
        last_line = _section_after(body, "===== last_line =====")
        _SIDECAR_PATH.write_text(
            json.dumps(
                {
                    "outcome": "CAPTURED",
                    "captured_at": datetime.now().astimezone().isoformat(),
                    "run_id": run_id,
                    "s3_key": obj_key,
                    "rc_in_trap": rc_value,
                    "last_line": last_line,
                    "attempts": attempts,
                    "budget_cap_usd": _BUDGET_USD_CAP,
                },
                indent=2,
            )
            + "\n",
        )
        return

    # Every attempt boot-succeeded — no failure to diagnose.
    _SIDECAR_PATH.write_text(
        json.dumps(
            {
                "outcome": "NO_REPRODUCTION",
                "captured_at": datetime.now().astimezone().isoformat(),
                "succeeded_runs": succeeded,
                "attempts": attempts,
                "budget_cap_usd": _BUDGET_USD_CAP,
                "spec_directive": (
                    "ship Phase B + Phase C unconditionally as belt-and-suspenders"
                ),
            },
            indent=2,
        )
        + "\n",
    )
