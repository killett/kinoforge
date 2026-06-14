# C28 — RunPod container-restart-loop prevention — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the PREVENTION layer for the chronic RunPod container-restart loop that C27 detects but does not fix — diagnostic-first (S3-PUT trap + restart-policy override + classify table), then structural fixes gated on Phase A evidence (Docker Hub public image pre-bake + curl retry hardening).

**Architecture:** Diagnostic-first, gated. Phase A captures stdout/stderr/exit-code from the dying container to S3 and names the root-cause hypothesis. Phase B (image pre-bake) ships when A5 implicates dep / import / pip drift. Phase C (curl retry helper) ships unconditionally — addresses HF-curl flake with zero infrastructure. Phase D closes out the spec lineage. No standing monthly charges (operator constraint).

**Tech Stack:** Python 3.13, pytest, pixi, pydantic v2, RunPod GraphQL API, AWS S3 + IAM, Docker Hub public registry, GitHub Actions, bash (provision-script substrate).

**Spec:** `docs/superpowers/specs/2026-06-13-c28-restart-loop-prevention-design.md`

---

## File map

**New files:**
- `tests/live/_c28_runpod_input_schema_probe.json` — A0 sidecar (schema introspection result).
- `tests/live/test_c28_phase_a_schema_probe_live.py` — A0 live smoke (introspection only, $0).
- `tests/live/test_c28_phase_a_diagnostic_capture_live.py` — A4 live smoke (~$0.20).
- `tests/live/_c28_phase_a_evidence.json` — A4/A5 sidecar.
- `tests/live/cfg_c28_phase_a_diagnostic.yaml` — Phase A cfg (mirrors `cfg_c27_phase_b.yaml` + `diagnostic_mode: true`).
- `tests/live/cfg_c28_phase_b_prebake.yaml` — Phase B cfg (kinoforge/wan-comfyui image).
- `tests/live/test_c28_phase_b_image_prebake_live.py` — B4 live smoke (~$0.30).
- `tests/live/_c28_phase_b_evidence.json` — B4 sidecar.
- `tests/live/test_c28_phase_c_curl_retry_live.py` — C3 live smoke ($0.05; reuses B pod).
- `tests/live/_c28_phase_c_evidence.json` — C3 sidecar.
- `tests/live/test_c28_spec_acceptance_live.py` — spec-level smoke (~$0.30).
- `tests/live/_c28_spec_acceptance_evidence.json` — spec-level sidecar.
- `docker/wan-comfyui/Dockerfile` — pre-baked image.
- `.github/workflows/build-wan-comfyui-image.yml` — manual-dispatch image build.
- `tests/providers/runpod/test_create_pod_restart_policy.py` — A3 unit tests.
- `tests/providers/runpod/test_create_pod_diagnostic_env.py` — A2 + A1 unit tests for env-var injection.
- `tests/engines/comfyui/test_render_provision_diagnostic_trap.py` — A2 trap pre-amble unit tests.
- `tests/engines/comfyui/test_render_provision_slim_mode.py` — B2 slim-mode branch unit tests.
- `tests/engines/comfyui/test_render_provision_kinoforge_download.py` — C1 + C2 helper unit tests.

**Modified files:**
- `src/kinoforge/core/interfaces.py` — `InstanceSpec.restart_policy`, `InstanceSpec.diagnostic_env_vars` fields.
- `src/kinoforge/core/config.py` — `Config.diagnostic_mode: bool = False` knob.
- `src/kinoforge/providers/runpod/__init__.py` — `_create_pod` body: thread `restart_policy`, inject diagnostic env vars; `_CREATE_POD_MUTATION` unchanged (input schema covers both).
- `src/kinoforge/engines/comfyui/__init__.py` — `render_provision`: prepend trap (gated on `diagnostic_mode`), unconditional `_kinoforge_download` helper, slim-mode branch when `image` starts with `kinoforge/wan-comfyui:`.
- `src/kinoforge/cli/_commands.py` — `--diagnostic-mode` flag on `kinoforge deploy`.
- `pixi.toml` — `[tasks] build-image-wan-comfyui = "..."`.
- `tests/live/cfg_c27_phase_b.yaml` — unchanged; C28 cfgs are net-new copies.
- `PROGRESS.md` — §C C28 entry on closeout.
- `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md` — §17 backlink.
- `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md` — §13 backlink.
- `docs/successful-generations.md` — Phase B/C smoke entries.

---

## Task 0: Pre-flight — operator credentials checklist

**Goal:** All credentials required for Phase A and Phase B exist before any task starts coding. Front-loaded per project memory.

**Files:**
- Touch: `.env` (operator-side, never committed).

**Acceptance Criteria:**
- [ ] `RUNPOD_API_KEY` + `RUNPOD_TERMINATE_KEY` present in `.env` (already required by predecessor specs).
- [ ] `HF_TOKEN` present in `.env` (already required by ComfyUI provision script).
- [ ] AWS credentials present and `kinoforge-ci` user has `IAMFullAccess` (per `kinoforge_ci_iamfullaccess` memory — already true).
- [ ] `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN` present in `.env`. Token has R/W/D scope (per user confirmation in brainstorm session).

**Verify:**

```bash
pixi run python -c "
import os
required = ['RUNPOD_API_KEY','RUNPOD_TERMINATE_KEY','HF_TOKEN','DOCKERHUB_USERNAME','DOCKERHUB_TOKEN','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY']
missing = [k for k in required if not os.environ.get(k)]
print(f'PASS: all {len(required)} credentials present' if not missing else f'FAIL: missing {missing}')
"
```

Expected: `PASS: all 7 credentials present`.

**Steps:**

- [ ] **Step 1: Verify all credentials.**

Run the verify command above. If FAIL, the operator adds the missing keys to `.env`; never Write/Edit a `.env` template per `never_write_secret_files` memory — operator creates the file directly.

- [ ] **Step 2: Commit (no file changes, just a marker).**

```bash
git commit --allow-empty -m "chore(c28): pre-flight — operator credentials verified for C28"
```

---

## Task 1: A0 — RunPod GraphQL schema probe

**Goal:** Sidecar capturing whether `restartPolicy`, `networkVolumeId`, and `registryAuthId` are present on `PodFindAndDeployOnDemandInput`. Cost: $0 (introspection-only).

**Files:**
- Create: `tests/live/test_c28_phase_a_schema_probe_live.py`
- Create: `tests/live/_c28_runpod_input_schema_probe.json`

**Acceptance Criteria:**
- [ ] Test gated on `KINOFORGE_LIVE_RUNPOD=1`.
- [ ] Hits the RunPod GraphQL endpoint with introspection query.
- [ ] Writes sidecar with three keys: `restart_policy_supported: bool`, `network_volume_supported: bool`, `registry_auth_supported: bool` plus raw inputFields array.

**Verify:** `pixi run pytest tests/live/test_c28_phase_a_schema_probe_live.py -v` → 1 passed; sidecar exists.

**Steps:**

- [ ] **Step 1: Write the live smoke (test_c28_phase_a_schema_probe_live.py).**

```python
"""C28 Task 1 (A0) — GraphQL introspection probe.

Verify which optional fields PodFindAndDeployOnDemandInput accepts.
Branches the rest of Phase A. Cost: $0 (no pod boot).
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_SIDECAR = Path("tests/live/_c28_runpod_input_schema_probe.json")
_GRAPHQL_URL = "https://api.runpod.io/graphql"
_QUERY = (
    '{ __type(name: "PodFindAndDeployOnDemandInput") { '
    "inputFields { name type { name kind ofType { name } } } } }"
)


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(f"set {_LIVE_GATE_ENV}=1 to run the C28 A0 schema probe")


def test_c28_phase_a_schema_probe_live() -> None:
    api_key = os.environ["RUNPOD_API_KEY"]
    body = json.dumps({"query": _QUERY}).encode()
    req = urllib.request.Request(
        _GRAPHQL_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        payload = json.load(resp)

    fields = (
        payload.get("data", {})
        .get("__type", {})
        .get("inputFields") or []
    )
    field_names = {f["name"] for f in fields}
    sidecar = {
        "captured_at": datetime.now().astimezone().isoformat(),
        "restart_policy_supported": "restartPolicy" in field_names,
        "network_volume_supported": "networkVolumeId" in field_names,
        "registry_auth_supported": "registryAuthId" in field_names,
        "input_fields": sorted(field_names),
        "raw_input_fields": fields,
    }
    _SIDECAR.write_text(json.dumps(sidecar, indent=2) + "\n")
    print(f"\nA0 schema probe → {_SIDECAR}")
    print(json.dumps({k: v for k, v in sidecar.items() if k != "raw_input_fields"}, indent=2))

    assert fields, "RunPod introspection returned no inputFields — schema introspection may be blocked"
```

- [ ] **Step 2: Run test in skip mode (verify gate works).**

Run: `pixi run pytest tests/live/test_c28_phase_a_schema_probe_live.py -v`
Expected: `SKIPPED` with message about `KINOFORGE_LIVE_RUNPOD=1`.

- [ ] **Step 3: Run live.**

Run: `KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_c28_phase_a_schema_probe_live.py -v -s`
Expected: PASS; sidecar `tests/live/_c28_runpod_input_schema_probe.json` written.

- [ ] **Step 4: Commit.**

```bash
git add tests/live/test_c28_phase_a_schema_probe_live.py tests/live/_c28_runpod_input_schema_probe.json
git commit -m "live(c28): A0 RunPod input schema probe — restart_policy/network_volume/registry_auth field presence"
```

---

## Task 2: A1 — S3 ingest bucket + scoped IAM

**Goal:** S3 bucket `kinoforge-pod-diagnostics` exists in `us-west-2` with 7-day lifecycle; `kinoforge-ci` self-grants a scoped `PutObject`-only policy.

**Files:**
- Create: `tools/c28_provision_s3_diagnostics.py` (one-shot provisioner; idempotent).
- Create: `tests/tools/test_c28_provision_s3_diagnostics.py` (unit test mocking boto3).

**Acceptance Criteria:**
- [ ] Bucket `kinoforge-pod-diagnostics` exists in `us-west-2`.
- [ ] Bucket has lifecycle rule deleting objects under `boot-logs/` older than 7 days.
- [ ] IAM policy `kinoforge-c28-diag-put` exists with statement: `Action: s3:PutObject`, `Resource: arn:aws:s3:::kinoforge-pod-diagnostics/boot-logs/*`.
- [ ] Policy attached to `kinoforge-ci` user.
- [ ] Tool is idempotent: re-running succeeds without error when state is already correct.

**Verify:** `pixi run pytest tests/tools/test_c28_provision_s3_diagnostics.py -v` → 4+ passed; then `pixi run -e live-skypilot aws s3 ls s3://kinoforge-pod-diagnostics/` → empty listing (no auth error).

**Steps:**

- [ ] **Step 1: Write unit test first (TDD red).**

```python
# tests/tools/test_c28_provision_s3_diagnostics.py
"""Unit tests for the C28 S3 + IAM provisioner."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tools.c28_provision_s3_diagnostics import (
    BUCKET_NAME,
    LIFECYCLE_PREFIX,
    POLICY_NAME,
    REGION,
    provision,
)


def test_provision_creates_bucket_when_absent() -> None:
    s3 = MagicMock()
    s3.head_bucket.side_effect = [_not_found(), None]
    iam = MagicMock()
    iam.get_policy.side_effect = _not_found()

    provision(s3=s3, iam=iam, account_id="123456789012")

    s3.create_bucket.assert_called_once_with(
        Bucket=BUCKET_NAME,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )


def test_provision_idempotent_when_bucket_present() -> None:
    s3 = MagicMock()
    iam = MagicMock()
    iam.get_policy.return_value = {"Policy": {"Arn": "arn:aws:iam::123:policy/kinoforge-c28-diag-put"}}

    provision(s3=s3, iam=iam, account_id="123456789012")

    s3.create_bucket.assert_not_called()


def test_provision_sets_7_day_lifecycle() -> None:
    s3 = MagicMock()
    iam = MagicMock()
    iam.get_policy.return_value = {"Policy": {"Arn": "arn:aws:iam::123:policy/kinoforge-c28-diag-put"}}

    provision(s3=s3, iam=iam, account_id="123456789012")

    call = s3.put_bucket_lifecycle_configuration.call_args
    rules = call.kwargs["LifecycleConfiguration"]["Rules"]
    assert any(
        r["Filter"]["Prefix"] == LIFECYCLE_PREFIX
        and r["Expiration"]["Days"] == 7
        for r in rules
    )


def test_provision_iam_policy_is_putobject_only() -> None:
    s3 = MagicMock()
    iam = MagicMock()
    iam.get_policy.side_effect = _not_found()

    provision(s3=s3, iam=iam, account_id="123456789012")

    call = iam.create_policy.call_args
    import json
    doc = json.loads(call.kwargs["PolicyDocument"])
    actions = {s for stmt in doc["Statement"] for s in (stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]])}
    assert actions == {"s3:PutObject"}


def _not_found():
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": "404"}}, "head_bucket")
```

- [ ] **Step 2: Run test, verify it fails (no module yet).**

Run: `pixi run pytest tests/tools/test_c28_provision_s3_diagnostics.py -v`
Expected: ImportError / module not found.

- [ ] **Step 3: Implement the provisioner.**

```python
# tools/c28_provision_s3_diagnostics.py
"""C28 A1 — provision the S3 bucket + scoped IAM policy for diagnostic uploads.

Idempotent: safe to re-run; only acts when state diverges.
"""
from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import ClientError

BUCKET_NAME = "kinoforge-pod-diagnostics"
REGION = "us-west-2"
LIFECYCLE_PREFIX = "boot-logs/"
POLICY_NAME = "kinoforge-c28-diag-put"
TARGET_USER = "kinoforge-ci"


def _iam_policy_doc() -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:PutObject",
                    "Resource": f"arn:aws:s3:::{BUCKET_NAME}/{LIFECYCLE_PREFIX}*",
                }
            ],
        }
    )


def provision(*, s3: Any, iam: Any, account_id: str) -> None:
    _ensure_bucket(s3)
    _ensure_lifecycle(s3)
    _ensure_policy_and_attachment(iam, account_id)


def _ensure_bucket(s3: Any) -> None:
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
    s3.create_bucket(
        Bucket=BUCKET_NAME,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )


def _ensure_lifecycle(s3: Any) -> None:
    s3.put_bucket_lifecycle_configuration(
        Bucket=BUCKET_NAME,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-boot-logs-7d",
                    "Status": "Enabled",
                    "Filter": {"Prefix": LIFECYCLE_PREFIX},
                    "Expiration": {"Days": 7},
                }
            ]
        },
    )


def _ensure_policy_and_attachment(iam: Any, account_id: str) -> None:
    arn = f"arn:aws:iam::{account_id}:policy/{POLICY_NAME}"
    try:
        iam.get_policy(PolicyArn=arn)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchEntity", "404"):
            raise
        iam.create_policy(PolicyName=POLICY_NAME, PolicyDocument=_iam_policy_doc())
    iam.attach_user_policy(UserName=TARGET_USER, PolicyArn=arn)


def main() -> None:
    sts = boto3.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    provision(
        s3=boto3.client("s3", region_name=REGION),
        iam=boto3.client("iam"),
        account_id=account_id,
    )
    print(f"OK: bucket={BUCKET_NAME} policy={POLICY_NAME} user={TARGET_USER}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, verify pass.**

Run: `pixi run pytest tests/tools/test_c28_provision_s3_diagnostics.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run the provisioner against real AWS.**

Run: `pixi run -e live-skypilot python -m tools.c28_provision_s3_diagnostics`
Expected: `OK: bucket=kinoforge-pod-diagnostics policy=kinoforge-c28-diag-put user=kinoforge-ci`.

- [ ] **Step 6: Verify access.**

Run: `pixi run -e live-skypilot aws s3 ls s3://kinoforge-pod-diagnostics/`
Expected: empty listing, no `AccessDenied` error.

- [ ] **Step 7: Commit.**

```bash
git add tools/c28_provision_s3_diagnostics.py tests/tools/test_c28_provision_s3_diagnostics.py
git commit -m "infra(c28): A1 S3 diagnostics bucket + kinoforge-ci PutObject-only IAM policy"
```

---

## Task 3: A2 — render_provision EXIT trap pre-amble (diagnostic_mode gated)

**Goal:** When `cfg.diagnostic_mode == true`, `ComfyUIEngine.render_provision` prepends an EXIT trap that captures stdout/stderr/exit-code/last-line + system snapshots and PUTs to S3. Pure-additive — prod (`diagnostic_mode: false`) is byte-identical.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py:1168` (`render_provision`).
- Create: `tests/engines/comfyui/test_render_provision_diagnostic_trap.py`.

**Acceptance Criteria:**
- [ ] When `cfg["diagnostic_mode"]` is falsy or missing, `render_provision` output is byte-identical to the pre-C28 baseline.
- [ ] When `cfg["diagnostic_mode"] == True`, output begins with the trap pre-amble (defined in spec §5 A2).
- [ ] Trap body references `KINOFORGE_DIAG_BUCKET`, `KINOFORGE_DIAG_PREFIX` env vars (NOT the access-key vars — those are referenced by the `aws s3 cp` AWS-SDK default chain).
- [ ] Trap body never echoes `KINOFORGE_DIAG_ACCESS_KEY` or `KINOFORGE_DIAG_SECRET_KEY`.
- [ ] Trap body wraps `aws s3 cp` in `|| true` so a PUT failure does not propagate.

**Verify:** `pixi run pytest tests/engines/comfyui/test_render_provision_diagnostic_trap.py -v` → 6+ passed.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
# tests/engines/comfyui/test_render_provision_diagnostic_trap.py
"""C28 A2 — render_provision EXIT trap pre-amble, gated on diagnostic_mode."""
from __future__ import annotations

import pytest

from kinoforge.engines.comfyui import ComfyUIEngine

_MIN_CFG = {
    "engine": {"comfyui": {"repo": "https://github.com/comfyanonymous/ComfyUI", "branch": "master"}},
    "models": [],
}


@pytest.fixture
def engine() -> ComfyUIEngine:
    return ComfyUIEngine()


def test_no_diagnostic_mode_no_trap(engine: ComfyUIEngine) -> None:
    out = engine.render_provision({**_MIN_CFG})
    assert "_kinoforge_diag_capture" not in out.script
    assert "trap" not in out.script.splitlines()[0:5]


def test_diagnostic_mode_emits_trap_preamble(engine: ComfyUIEngine) -> None:
    out = engine.render_provision({**_MIN_CFG, "diagnostic_mode": True})
    head = out.script.splitlines()[:20]
    assert any("trap '_kinoforge_diag_capture $?' EXIT" in ln for ln in head)
    assert any("exec > >(tee -a /tmp/boot.log) 2>&1" in ln for ln in head)


def test_trap_captures_required_sections(engine: ComfyUIEngine) -> None:
    out = engine.render_provision({**_MIN_CFG, "diagnostic_mode": True})
    for marker in (
        "===== rc =====",
        "===== last_line =====",
        "===== nvidia-smi =====",
        "===== df -h =====",
        "===== free -m =====",
        "===== ls -la models/diffusion_models =====",
        "===== dpkg -l torch =====",
        "===== boot.log =====",
    ):
        assert marker in out.script, f"missing trap section: {marker}"


def test_trap_references_diag_env_vars(engine: ComfyUIEngine) -> None:
    out = engine.render_provision({**_MIN_CFG, "diagnostic_mode": True})
    assert "${KINOFORGE_DIAG_BUCKET" in out.script
    assert "${KINOFORGE_DIAG_PREFIX" in out.script


def test_trap_never_echoes_access_key(engine: ComfyUIEngine) -> None:
    out = engine.render_provision({**_MIN_CFG, "diagnostic_mode": True})
    # secret material must not appear in the script body at all — it lives in env only
    assert "KINOFORGE_DIAG_ACCESS_KEY" not in out.script
    assert "KINOFORGE_DIAG_SECRET_KEY" not in out.script


def test_trap_aws_cp_swallows_errors(engine: ComfyUIEngine) -> None:
    out = engine.render_provision({**_MIN_CFG, "diagnostic_mode": True})
    # the line invoking aws s3 cp MUST end with || true so failure doesn't
    # masquerade as a "real" exit code
    cp_line = next(ln for ln in out.script.splitlines() if "aws s3 cp" in ln)
    assert cp_line.rstrip().endswith("|| true")
```

- [ ] **Step 2: Run tests, verify fail.**

Run: `pixi run pytest tests/engines/comfyui/test_render_provision_diagnostic_trap.py -v`
Expected: 6 FAILED.

- [ ] **Step 3: Implement the pre-amble in `render_provision`.**

In `src/kinoforge/engines/comfyui/__init__.py` `render_provision`, just before `lines: list[str] = [`, insert:

```python
diagnostic_mode: bool = bool(cfg_dict.get("diagnostic_mode", False))
trap_preamble: list[str] = []
if diagnostic_mode:
    trap_preamble = [
        "set -euo pipefail",
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
        "ls -la /workspace/ComfyUI/models/diffusion_models 2>/dev/null || true;",
        "    echo '===== dpkg -l torch ====='; "
        "dpkg -l 2>/dev/null | grep -iE 'torch|cuda' || true;",
        "    echo '===== boot.log ====='; tail -500 /tmp/boot.log 2>/dev/null || true;",
        "  } > /tmp/diag.txt",
        "  if [ -n \"${KINOFORGE_DIAG_BUCKET:-}\" ]; then",
        "    aws s3 cp /tmp/diag.txt "
        "\"s3://${KINOFORGE_DIAG_BUCKET}/${KINOFORGE_DIAG_PREFIX}/"
        "diag-$(date -u +%Y%m%dT%H%M%SZ).txt\" || true",
        "  fi",
        "}",
    ]
```

Then change the existing `lines: list[str] = [` block: when `trap_preamble` is non-empty, REPLACE the leading `"set -euo pipefail",` line with `*trap_preamble,` (the trap pre-amble already includes `set -euo pipefail` as its first line). When `trap_preamble` is empty, behavior unchanged.

Specifically replace:

```python
lines: list[str] = [
    "set -euo pipefail",
    # Selfterm watchdog — launch BEFORE bootstrap so the dead-man
```

with:

```python
lines: list[str] = [
    *(trap_preamble if trap_preamble else ["set -euo pipefail"]),
    # Selfterm watchdog — launch BEFORE bootstrap so the dead-man
```

- [ ] **Step 4: Run tests, verify pass.**

Run: `pixi run pytest tests/engines/comfyui/test_render_provision_diagnostic_trap.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run the existing render_provision regression suite to confirm no breakage.**

Run: `pixi run pytest tests/engines/comfyui/ -v -k render_provision`
Expected: all green.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/engines/comfyui/__init__.py tests/engines/comfyui/test_render_provision_diagnostic_trap.py
git commit -m "feat(c28): A2 render_provision EXIT trap pre-amble — diagnostic_mode-gated S3 capture"
```

---

## Task 4: A1.5 — Wire diagnostic env vars into `_create_pod`

**Goal:** When the orchestrator passes a cfg with `diagnostic_mode: true`, `_create_pod` injects `KINOFORGE_DIAG_BUCKET`, `KINOFORGE_DIAG_PREFIX`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` into pod env. `AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY` use the standard SDK names so `aws s3 cp` in the trap finds them without explicit `KINOFORGE_DIAG_*` aliases (avoids leaking the credential name).

**Files:**
- Modify: `src/kinoforge/core/interfaces.py:121` (`InstanceSpec`) — add `diagnostic_env: dict[str, str] = field(default_factory=dict)`.
- Modify: `src/kinoforge/providers/runpod/__init__.py:_create_pod` — merge `spec.diagnostic_env` into pod env.
- Modify: `src/kinoforge/core/orchestrator.py:505` — pass `diagnostic_env` from cfg to InstanceSpec.
- Create: `tests/providers/runpod/test_create_pod_diagnostic_env.py`.

**Acceptance Criteria:**
- [ ] `InstanceSpec.diagnostic_env: dict[str, str]` field exists (default `{}`).
- [ ] When non-empty, `_create_pod` merges it into the GraphQL `env` list AFTER `spec.env` (so diagnostic env never overwrites the user's `HF_TOKEN` etc., but DOES override pod-injected defaults).
- [ ] When `spec.diagnostic_env == {}`, pod env wire-shape unchanged.
- [ ] Orchestrator pulls `KINOFORGE_DIAG_*` + AWS keys from `os.environ` / cred provider when `cfg.diagnostic_mode == True` and passes them via `spec.diagnostic_env`.
- [ ] Access-key VALUES never appear in test assertions or commit messages.

**Verify:** `pixi run pytest tests/providers/runpod/test_create_pod_diagnostic_env.py -v` → 3+ passed.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
# tests/providers/runpod/test_create_pod_diagnostic_env.py
"""C28 A1.5 — _create_pod injects diagnostic env vars when InstanceSpec carries them."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec, Lifecycle, Offer
from kinoforge.providers.runpod import RunPodProvider


def _make_provider(captured: dict[str, Any]) -> RunPodProvider:
    """Return a provider whose _http_post records the create-pod input dict."""

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured["body"] = body
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-xyz"}}}

    p = RunPodProvider(creds=None, http_post=_http_post)
    return p


def test_default_diagnostic_env_empty_no_change_to_pod_env() -> None:
    captured: dict[str, Any] = {}
    p = _make_provider(captured)
    spec = InstanceSpec(image="alpine:latest", env={"HF_TOKEN": "x"})
    p._create_pod(spec)
    env_list = captured["body"]["variables"]["input"]["env"]
    keys = {e["key"] for e in env_list}
    assert "KINOFORGE_DIAG_BUCKET" not in keys
    assert "AWS_ACCESS_KEY_ID" not in keys
    assert "HF_TOKEN" in keys


def test_diagnostic_env_merged_into_pod_env() -> None:
    captured: dict[str, Any] = {}
    p = _make_provider(captured)
    spec = InstanceSpec(
        image="alpine:latest",
        env={"HF_TOKEN": "x"},
        diagnostic_env={
            "KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics",
            "KINOFORGE_DIAG_PREFIX": "boot-logs/pod-xyz",
            "AWS_ACCESS_KEY_ID": "AKIA-fake",
            "AWS_SECRET_ACCESS_KEY": "fake",
            "AWS_DEFAULT_REGION": "us-west-2",
        },
    )
    p._create_pod(spec)
    env_list = captured["body"]["variables"]["input"]["env"]
    keys = {e["key"] for e in env_list}
    assert "KINOFORGE_DIAG_BUCKET" in keys
    assert "AWS_ACCESS_KEY_ID" in keys
    assert "HF_TOKEN" in keys  # user env preserved


def test_diagnostic_env_does_not_overwrite_user_env() -> None:
    captured: dict[str, Any] = {}
    p = _make_provider(captured)
    # User explicitly sets KINOFORGE_DIAG_BUCKET — diagnostic overlay must NOT clobber it
    spec = InstanceSpec(
        image="alpine:latest",
        env={"KINOFORGE_DIAG_BUCKET": "user-override"},
        diagnostic_env={"KINOFORGE_DIAG_BUCKET": "default-overlay"},
    )
    p._create_pod(spec)
    env_list = captured["body"]["variables"]["input"]["env"]
    bucket = next(e["value"] for e in env_list if e["key"] == "KINOFORGE_DIAG_BUCKET")
    assert bucket == "user-override"
```

- [ ] **Step 2: Run tests, verify fail.**

Run: `pixi run pytest tests/providers/runpod/test_create_pod_diagnostic_env.py -v`
Expected: 3 FAILED.

- [ ] **Step 3: Add `diagnostic_env` to `InstanceSpec`.**

In `src/kinoforge/core/interfaces.py` near line 133, add the field:

```python
provision_script: str | None = None
run_cmd: list[str] | None = None
spot: bool = False
diagnostic_env: dict[str, str] = field(default_factory=dict)  # C28: opt-in diag env overlay
```

- [ ] **Step 4: Merge into `_create_pod`.**

In `src/kinoforge/providers/runpod/__init__.py:_create_pod`, after `env: dict[str, str] = dict(spec.env)` (line 569), insert:

```python
# C28 A1.5: overlay diagnostic env, but never overwrite caller's explicit env
for k, v in spec.diagnostic_env.items():
    env.setdefault(k, v)
```

- [ ] **Step 5: Run tests, verify pass.**

Run: `pixi run pytest tests/providers/runpod/test_create_pod_diagnostic_env.py -v`
Expected: 3 passed.

- [ ] **Step 6: Orchestrator wiring.**

Find the orchestrator path that builds `InstanceSpec` (search: `rg -n 'InstanceSpec(' src/kinoforge/core/orchestrator.py`). At the construction site, when `cfg.diagnostic_mode == True`, pass `diagnostic_env={...}` populated from `os.environ` keys: `KINOFORGE_DIAG_BUCKET` (default `"kinoforge-pod-diagnostics"`), `KINOFORGE_DIAG_PREFIX` (default `f"boot-logs/{run_id}"`), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` (default `"us-west-2"`). Add a separate orchestrator unit test mirroring the `test_diagnostic_env_*` shape but at the orchestrator boundary.

- [ ] **Step 7: Add `diagnostic_mode` to `Config`.**

In `src/kinoforge/core/config.py`, add to the top-level `Config` BaseModel:

```python
diagnostic_mode: bool = False
```

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/orchestrator.py src/kinoforge/providers/runpod/__init__.py src/kinoforge/core/config.py tests/providers/runpod/test_create_pod_diagnostic_env.py
git commit -m "feat(c28): A1.5 InstanceSpec.diagnostic_env + orchestrator wiring + Config.diagnostic_mode"
```

---

## Task 5: A3 — `restart_policy` field on `InstanceSpec` + `_create_pod`

**Goal:** When the A0 sidecar confirms `restartPolicy` is in the RunPod schema, kinoforge can request "never restart" via cfg. Per-cfg knob + CLI `--diagnostic-mode` flag. When A0 says NO, this task ships the field but `_create_pod` skips it on the wire (no-op).

**Files:**
- Modify: `src/kinoforge/core/interfaces.py:121` (`InstanceSpec`).
- Modify: `src/kinoforge/providers/runpod/__init__.py:_create_pod` body.
- Modify: `src/kinoforge/cli/_commands.py:174` (`_cmd_deploy`).
- Modify: `src/kinoforge/core/config.py` (`ComputeConfig` or equivalent).
- Create: `tests/providers/runpod/test_create_pod_restart_policy.py`.

**Acceptance Criteria:**
- [ ] `InstanceSpec.restart_policy: Literal["always","never"] = "always"`.
- [ ] When `spec.restart_policy == "never"` AND A0 sidecar `restart_policy_supported == True`, `_create_pod` adds `restartPolicy: "NEVER"` to the input dict.
- [ ] When `spec.restart_policy == "never"` AND schema doesn't support it, `_create_pod` logs a warning and skips the field (no error).
- [ ] CLI: `kinoforge deploy --diagnostic-mode` sets both `diagnostic_mode=True` and `restart_policy_override="never"`.
- [ ] Default behavior (no `--diagnostic-mode`) unchanged — `restartPolicy` not on the wire.

**Verify:** `pixi run pytest tests/providers/runpod/test_create_pod_restart_policy.py -v` → 4+ passed.

**Steps:**

- [ ] **Step 1: Write tests.**

```python
# tests/providers/runpod/test_create_pod_restart_policy.py
"""C28 A3 — InstanceSpec.restart_policy + _create_pod wire branch."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kinoforge.core.interfaces import InstanceSpec
from kinoforge.providers.runpod import RunPodProvider

_SIDECAR = Path("tests/live/_c28_runpod_input_schema_probe.json")


def _make_provider(captured: dict[str, Any]) -> RunPodProvider:
    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured["body"] = body
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-xyz"}}}
    return RunPodProvider(creds=None, http_post=_http_post)


def test_default_restart_policy_not_on_wire() -> None:
    captured: dict[str, Any] = {}
    p = _make_provider(captured)
    spec = InstanceSpec(image="alpine:latest")
    p._create_pod(spec)
    assert "restartPolicy" not in captured["body"]["variables"]["input"]


def test_never_on_wire_when_schema_supports(tmp_path: Path) -> None:
    sidecar = tmp_path / "schema.json"
    sidecar.write_text(json.dumps({"restart_policy_supported": True}))
    captured: dict[str, Any] = {}
    p = _make_provider(captured)
    spec = InstanceSpec(image="alpine:latest", restart_policy="never")
    with patch("kinoforge.providers.runpod._RUNPOD_SCHEMA_SIDECAR", sidecar):
        p._create_pod(spec)
    assert captured["body"]["variables"]["input"]["restartPolicy"] == "NEVER"


def test_never_skipped_when_schema_unsupported(tmp_path: Path) -> None:
    sidecar = tmp_path / "schema.json"
    sidecar.write_text(json.dumps({"restart_policy_supported": False}))
    captured: dict[str, Any] = {}
    p = _make_provider(captured)
    spec = InstanceSpec(image="alpine:latest", restart_policy="never")
    with patch("kinoforge.providers.runpod._RUNPOD_SCHEMA_SIDECAR", sidecar):
        p._create_pod(spec)
    assert "restartPolicy" not in captured["body"]["variables"]["input"]


def test_never_skipped_when_sidecar_missing(tmp_path: Path) -> None:
    """Absent sidecar → conservative skip; no error."""
    captured: dict[str, Any] = {}
    p = _make_provider(captured)
    spec = InstanceSpec(image="alpine:latest", restart_policy="never")
    with patch("kinoforge.providers.runpod._RUNPOD_SCHEMA_SIDECAR", tmp_path / "absent.json"):
        p._create_pod(spec)
    assert "restartPolicy" not in captured["body"]["variables"]["input"]
```

- [ ] **Step 2: Run tests, verify fail.**

Run: `pixi run pytest tests/providers/runpod/test_create_pod_restart_policy.py -v`
Expected: 4 FAILED.

- [ ] **Step 3: Add the field.**

In `src/kinoforge/core/interfaces.py:InstanceSpec`, alongside `diagnostic_env` from Task 4:

```python
from typing import Literal
restart_policy: Literal["always", "never"] = "always"  # C28: opt-out of RunPod auto-restart
```

- [ ] **Step 4: Wire `_create_pod`.**

In `src/kinoforge/providers/runpod/__init__.py`, at module top:

```python
from pathlib import Path

_RUNPOD_SCHEMA_SIDECAR = Path("tests/live/_c28_runpod_input_schema_probe.json")


def _restart_policy_supported() -> bool:
    """Read the A0 sidecar to decide whether to emit restartPolicy on the wire."""
    try:
        import json
        return bool(
            json.loads(_RUNPOD_SCHEMA_SIDECAR.read_text()).get("restart_policy_supported")
        )
    except (FileNotFoundError, json.JSONDecodeError):
        return False
```

In `_create_pod` body, after the `body` dict is constructed (after line 643):

```python
if spec.restart_policy == "never" and _restart_policy_supported():
    body["variables"]["input"]["restartPolicy"] = "NEVER"
elif spec.restart_policy == "never":
    import logging
    logging.getLogger(__name__).warning(
        "spec.restart_policy='never' requested but RunPod schema does not surface "
        "restartPolicy (per %s) — falling back to default restart-on-failure",
        _RUNPOD_SCHEMA_SIDECAR,
    )
```

- [ ] **Step 5: CLI flag.**

In `src/kinoforge/cli/_commands.py:_cmd_deploy` argparse block, mirror the `--restart-loop-window-override` pattern (line 229 reference):

```python
parser.add_argument(
    "--diagnostic-mode",
    action="store_true",
    help="C28: enable in-pod EXIT trap + S3 boot-log capture; sets restart_policy=never",
)
```

In the cfg-merge path, when `args.diagnostic_mode`, set `cfg.diagnostic_mode = True` and the resolved InstanceSpec carries `restart_policy="never"`.

- [ ] **Step 6: Run tests, verify pass.**

Run: `pixi run pytest tests/providers/runpod/test_create_pod_restart_policy.py -v`
Expected: 4 passed.

- [ ] **Step 7: Run the existing RunPod provider suite — no regressions.**

Run: `pixi run pytest tests/providers/runpod/ -v`
Expected: all green.

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/providers/runpod/__init__.py src/kinoforge/cli/_commands.py tests/providers/runpod/test_create_pod_restart_policy.py
git commit -m "feat(c28): A3 InstanceSpec.restart_policy + _create_pod schema-gated wire + --diagnostic-mode CLI"
```

---

## Task 6: A4 — Phase A live smoke RED scaffold

**Goal:** Test scaffold for the Phase A diagnostic capture smoke COMMITTED before any live spend (per CLAUDE.md durability rule). Scaffold is RED (skip if `KINOFORGE_LIVE_RUNPOD != 1`).

**Files:**
- Create: `tests/live/cfg_c28_phase_a_diagnostic.yaml`.
- Create: `tests/live/test_c28_phase_a_diagnostic_capture_live.py`.

**Acceptance Criteria:**
- [ ] Cfg file mirrors `cfg_c27_phase_b.yaml` + adds `diagnostic_mode: true` at top level.
- [ ] Test imports kinoforge, sets up an S3 client, runs `kinoforge generate` with `--diagnostic-mode`, polls S3 for the boot-log object, asserts required sections.
- [ ] Skipped under default env (no live spend).
- [ ] Budget cap encoded: `_BUDGET_USD_CAP = 0.20`.

**Verify:** `pixi run pytest tests/live/test_c28_phase_a_diagnostic_capture_live.py -v` → 1 skipped.

**Steps:**

- [ ] **Step 1: Write cfg.**

Copy `tests/live/cfg_c27_phase_b.yaml` to `tests/live/cfg_c28_phase_a_diagnostic.yaml`, add the top-level key:

```yaml
diagnostic_mode: true
# rest mirrors cfg_c27_phase_b.yaml
```

- [ ] **Step 2: Write the scaffold test.**

```python
# tests/live/test_c28_phase_a_diagnostic_capture_live.py
"""C28 Phase A live smoke — diagnostic_mode trap captures boot log to S3.

Cost cap: $0.20 (one cold boot to failure ~$0.05 + retry headroom).
Gated by KINOFORGE_LIVE_RUNPOD=1.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.20
_CFG = Path("tests/live/cfg_c28_phase_a_diagnostic.yaml")
_SIDECAR = Path("tests/live/_c28_phase_a_evidence.json")
_REQUIRED_MARKERS = [
    "===== rc =====",
    "===== last_line =====",
    "===== nvidia-smi =====",
    "===== df -h =====",
    "===== free -m =====",
    "===== ls -la models/diffusion_models =====",
    "===== dpkg -l torch =====",
    "===== boot.log =====",
]
_MAX_BOOT_ATTEMPTS = 3  # retry policy per spec §5 A4


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the Phase A diagnostic smoke "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


def test_c28_phase_a_diagnostic_capture_live() -> None:
    """Boot a real Wan+ComfyUI pod with diagnostic_mode; capture failure log to S3.

    Retry policy: up to 3 boots; if all succeed, spec gates B+C unconditionally.
    """
    import boto3

    s3 = boto3.client("s3", region_name="us-west-2")
    captured_objects: list[dict[str, Any]] = []
    succeeded_runs = 0

    for attempt in range(1, _MAX_BOOT_ATTEMPTS + 1):
        run_id = f"c28-phase-a-{datetime.now().strftime('%Y%m%dT%H%M%S')}-a{attempt}"
        prefix = f"boot-logs/{run_id}"
        env = {**os.environ, "KINOFORGE_DIAG_PREFIX": prefix}
        proc = subprocess.run(
            [
                "pixi", "run", "kinoforge", "generate",
                "--config", str(_CFG),
                "--diagnostic-mode",
                "--run-id", run_id,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=2400,
        )
        # Poll S3 for the boot-log object (trap PUTs from inside pod)
        obj_key = None
        for _ in range(60):
            resp = s3.list_objects_v2(
                Bucket="kinoforge-pod-diagnostics",
                Prefix=prefix,
            )
            if resp.get("Contents"):
                obj_key = resp["Contents"][0]["Key"]
                break
            time.sleep(5)
        captured_objects.append({
            "attempt": attempt,
            "run_id": run_id,
            "rc": proc.returncode,
            "s3_key": obj_key,
        })
        if proc.returncode == 0 and obj_key is None:
            succeeded_runs += 1
            continue  # boot succeeded, no failure to capture; retry
        # We have a failure boot with a captured log — extract and break
        assert obj_key, f"attempt {attempt}: kinoforge generate failed (rc={proc.returncode}) but no S3 object appeared in 5 min"
        body = s3.get_object(Bucket="kinoforge-pod-diagnostics", Key=obj_key)["Body"].read().decode()
        missing = [m for m in _REQUIRED_MARKERS if m not in body]
        assert not missing, f"S3 log missing markers: {missing}\nfull body:\n{body[:2000]}"
        # Extract last_line + rc for the sidecar
        rc_idx = body.index("===== rc =====") + len("===== rc =====")
        rc_val = body[rc_idx:].splitlines()[1].strip()
        ll_idx = body.index("===== last_line =====") + len("===== last_line =====")
        last_line = body[ll_idx:].splitlines()[1].strip()
        _SIDECAR.write_text(json.dumps({
            "outcome": "CAPTURED",
            "captured_at": datetime.now().astimezone().isoformat(),
            "run_id": run_id,
            "s3_key": obj_key,
            "rc_in_trap": rc_val,
            "last_line": last_line,
            "attempts": captured_objects,
        }, indent=2) + "\n")
        return

    # All 3 attempts succeeded — no failure to diagnose
    _SIDECAR.write_text(json.dumps({
        "outcome": "NO_REPRODUCTION",
        "captured_at": datetime.now().astimezone().isoformat(),
        "attempts": captured_objects,
        "spec_directive": "ship Phase B + Phase C unconditionally as belt-and-suspenders",
    }, indent=2) + "\n")
```

- [ ] **Step 3: Verify scaffold is RED (skipped without env).**

Run: `pixi run pytest tests/live/test_c28_phase_a_diagnostic_capture_live.py -v`
Expected: 1 skipped.

- [ ] **Step 4: Commit (RED scaffold, pre-spend, per durability rule).**

```bash
git add tests/live/cfg_c28_phase_a_diagnostic.yaml tests/live/test_c28_phase_a_diagnostic_capture_live.py
git commit -m "live(c28): A4 Phase A RED scaffold — diagnostic capture smoke + cfg"
```

---

## Task 7: A4 + A5 — Run Phase A live smoke + classify

**Goal:** Boot real Wan + ComfyUI with `diagnostic_mode=true`, capture S3 boot-log, extract `rc` and `last_line`, name the root-cause hypothesis from the §3 classify table. Sidecar `_c28_phase_a_evidence.json` updates with the matched hypothesis.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Run: `tests/live/test_c28_phase_a_diagnostic_capture_live.py`
- Update: `tests/live/_c28_phase_a_evidence.json` (already created by the test).
- Update (manually after smoke): same sidecar with `matched_hypothesis: H1|H2|H3|H4|H5|H6|Hn` + cited evidence lines.

**Acceptance Criteria:**
- [ ] `pixi run preflight` returns exit 0 BEFORE the live smoke (per CLAUDE.md durability rule).
- [ ] Live smoke runs and produces `tests/live/_c28_phase_a_evidence.json` with `outcome` field in `{CAPTURED, NO_REPRODUCTION}`.
- [ ] If `CAPTURED`: sidecar has non-empty `rc_in_trap`, `last_line`, `s3_key`.
- [ ] Sidecar has `matched_hypothesis` field naming one of H1-H6 (or Hn for "new hypothesis") with `evidence_lines` array citing specific lines from the captured boot.log.
- [ ] Total spend ≤ $0.20.

**Verify:** `cat tests/live/_c28_phase_a_evidence.json | jq '.outcome, .matched_hypothesis, .last_line'` → outcome + hypothesis + last line all populated.

**Steps:**

- [ ] **Step 1: Preflight.**

Run: `pixi run preflight`
Expected: exit 0. If non-zero, fix the offending check (RunPod creds present, no active pods, clean tree) and re-run.

- [ ] **Step 2: Run the live smoke.**

Run: `KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_c28_phase_a_diagnostic_capture_live.py -v -s`
Expected: 1 passed (or skipped if the smoke encountered an unrecoverable infra error — re-run after fixing).

- [ ] **Step 3: Inspect the captured S3 object.**

```bash
pixi run -e live-skypilot aws s3 cp \
  "s3://kinoforge-pod-diagnostics/$(jq -r .s3_key tests/live/_c28_phase_a_evidence.json)" \
  /tmp/c28_phase_a_diag.txt
less /tmp/c28_phase_a_diag.txt
```

- [ ] **Step 4: Classify per §3 table.**

Compare `last_line` against the table in the spec (`docs/superpowers/specs/2026-06-13-c28-restart-loop-prevention-design.md` §3). Identify the matched hypothesis.

- [ ] **Step 5: Update sidecar with classification.**

Append to `tests/live/_c28_phase_a_evidence.json`:

```json
{
  "matched_hypothesis": "H1",  // <-- ACTUAL value from the table
  "matched_table_row": "Traceback ... ImportError",
  "evidence_lines": [
    "Line 247 of boot.log: ModuleNotFoundError: No module named 'foo'",
    "Line 245: from kijai_wan_wrapper import nodes"
  ],
  "phase_b_triggered": true,
  "phase_c_triggered": true  // always — Phase C is unconditional
}
```

- [ ] **Step 6: Commit evidence.**

```bash
git add tests/live/_c28_phase_a_evidence.json
git commit -m "live(c28): A4 Phase A PROVEN — diagnostic capture + A5 hypothesis classification"
```

---

## Task 8: GATE — branch on A5 evidence

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Read the A5 sidecar and decide which downstream phases ship. Phase C (Task 14-16) is unconditional. Phase B (Tasks 9-13) ships when `matched_hypothesis` is one of H1, H2, H4, or `outcome == NO_REPRODUCTION`.

**Files:**
- Read: `tests/live/_c28_phase_a_evidence.json`.
- Touch: `docs/superpowers/plans/2026-06-13-c28-restart-loop-prevention.md` (gate decision recorded as a markdown comment for executor visibility).

**Acceptance Criteria:**
- [ ] Sidecar `matched_hypothesis` field is one of `{H1, H2, H3, H4, H5, H6, Hn, NO_REPRODUCTION}`.
- [ ] If `H6` → STOP this plan; raise a follow-up issue to re-open C25 wire-fix in a new spec. NEITHER B nor C ships.
- [ ] If `H5` → STOP Phase B; ship a new sub-task to refactor selfterm injection; ship Phase C as scheduled.
- [ ] If `H3` AND no other hypothesis matched → SKIP Phase B; ship Phase C only.
- [ ] Otherwise → ship Phase B + Phase C.

**Verify:** `pixi run python -c "import json; print(json.load(open('tests/live/_c28_phase_a_evidence.json'))['matched_hypothesis'])"` → matches one of the expected values; gate decision committed.

**Steps:**

- [ ] **Step 1: Read the sidecar.**

```bash
jq '.matched_hypothesis, .outcome' tests/live/_c28_phase_a_evidence.json
```

- [ ] **Step 2: Decide.**

| `matched_hypothesis` | Action |
| -------------------- | ------ |
| H1 / H2 / H4 / NO_REPRODUCTION | Continue to Task 9 (Phase B) and Task 14 (Phase C) |
| H3 only | Skip Tasks 9-13; jump to Task 14 |
| H5 | Skip Tasks 9-13; add a "refactor selfterm injection" task before Task 14 |
| H6 | STOP plan; open follow-up "C29 — C25 wire-fix re-open" |
| Hn (new) | Decide based on the new hypothesis's nature; document inline |

- [ ] **Step 3: Record the gate decision in the plan.**

Edit this plan's `## Task 8` to add a "Decision:" line citing the matched hypothesis and which downstream tasks are skipped or extra.

- [ ] **Step 4: Commit.**

```bash
git add docs/superpowers/plans/2026-06-13-c28-restart-loop-prevention.md
git commit -m "gate(c28): A5 classification → phase B/C routing decision recorded"
```

---

## Task 9: B0 — Dockerfile + local build smoke

**Goal:** A `docker/wan-comfyui/Dockerfile` that builds a kinoforge-pre-baked image containing ComfyUI + Kijai Wan + KJNodes + VHS at pinned refs, with a build-time `import comfy` smoke. SKIP this task and 10-13 if Task 8 routed away from Phase B.

**Files:**
- Create: `docker/wan-comfyui/Dockerfile`.
- Create: `docker/wan-comfyui/README.md`.
- Create: `tests/tools/test_wan_comfyui_dockerfile_lint.py` (validates ARG/RUN structure).

**Acceptance Criteria:**
- [ ] Dockerfile builds locally: `docker build -t kinoforge/wan-comfyui:test docker/wan-comfyui/`.
- [ ] Build-time `python -c "import sys; sys.path.insert(0,'.'); import comfy"` runs and exits 0.
- [ ] Image size ≤ 25 GB.
- [ ] Refs pinned via `ARG`: `COMFYUI_REF`, `KIJAI_WAN_REF`, `KJNODES_REF`, `VHS_REF` (taken from `tests/live/cfg_c27_phase_b.yaml`).

**Verify:** `docker build -t kinoforge/wan-comfyui:test docker/wan-comfyui/ && docker images kinoforge/wan-comfyui:test` → image exists, size < 25 GB.

**Steps:**

- [ ] **Step 1: Write Dockerfile.**

```dockerfile
# docker/wan-comfyui/Dockerfile
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

ARG COMFYUI_REF=v0.3.10
ARG KIJAI_WAN_REF=088128b224242e110d3906c6750e9a3a348a659b
ARG KJNODES_REF=369c8aee9ad4641823d0ffd7035076bcd297b6f2
ARG VHS_REF=4ee72c065db22c9d96c2427954dc69e7b908444b

WORKDIR /workspace

RUN git clone --depth 1 --branch ${COMFYUI_REF} \
      https://github.com/comfyanonymous/ComfyUI /workspace/ComfyUI && \
    cd /workspace/ComfyUI && \
    pip install --no-cache-dir -r requirements.txt

RUN cd /workspace/ComfyUI && \
    git clone https://github.com/kijai/ComfyUI-WanVideoWrapper \
      custom_nodes/ComfyUI-WanVideoWrapper && \
    cd custom_nodes/ComfyUI-WanVideoWrapper && \
    git checkout ${KIJAI_WAN_REF} && \
    pip install --no-cache-dir -r requirements.txt

RUN cd /workspace/ComfyUI && \
    git clone https://github.com/kijai/ComfyUI-KJNodes custom_nodes/ComfyUI-KJNodes && \
    cd custom_nodes/ComfyUI-KJNodes && \
    git checkout ${KJNODES_REF} && \
    pip install --no-cache-dir -r requirements.txt

RUN cd /workspace/ComfyUI && \
    git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite \
      custom_nodes/ComfyUI-VideoHelperSuite && \
    cd custom_nodes/ComfyUI-VideoHelperSuite && \
    git checkout ${VHS_REF} && \
    pip install --no-cache-dir -r requirements.txt

# Build-time import smoke — broken combos fail `docker build`
RUN cd /workspace/ComfyUI && \
    python -c "import sys; sys.path.insert(0,'.'); import comfy"

# Build-time tag stamp for traceability
ARG IMAGE_TAG=unknown
ENV KINOFORGE_IMAGE_TAG=${IMAGE_TAG}

# Image is now ready; provision script (kinoforge-emitted) supplies the
# launch step at pod boot.
```

- [ ] **Step 2: Write lint test.**

```python
# tests/tools/test_wan_comfyui_dockerfile_lint.py
"""Lint the kinoforge/wan-comfyui Dockerfile structure."""
from pathlib import Path

_DF = Path("docker/wan-comfyui/Dockerfile")


def test_dockerfile_exists() -> None:
    assert _DF.is_file()


def test_dockerfile_pins_all_required_refs() -> None:
    body = _DF.read_text()
    for arg in ("COMFYUI_REF", "KIJAI_WAN_REF", "KJNODES_REF", "VHS_REF"):
        assert f"ARG {arg}" in body, f"missing ARG {arg}"


def test_dockerfile_uses_kinoforge_base() -> None:
    body = _DF.read_text()
    assert body.splitlines()[0].startswith("FROM runpod/pytorch:2.4.0")


def test_dockerfile_has_build_time_import_smoke() -> None:
    body = _DF.read_text()
    assert 'python -c "import sys; sys.path.insert(0,' in body
    assert "import comfy" in body
```

- [ ] **Step 3: Run lint tests, verify pass.**

Run: `pixi run pytest tests/tools/test_wan_comfyui_dockerfile_lint.py -v`
Expected: 4 passed.

- [ ] **Step 4: Build locally.**

Run: `docker build -t kinoforge/wan-comfyui:test docker/wan-comfyui/`
Expected: build succeeds; last layer runs the `import comfy` smoke and exits 0.

- [ ] **Step 5: Verify image size.**

Run: `docker images kinoforge/wan-comfyui:test --format '{{.Size}}'`
Expected: ≤ 25 GB.

- [ ] **Step 6: Commit.**

```bash
git add docker/wan-comfyui/Dockerfile tests/tools/test_wan_comfyui_dockerfile_lint.py
git commit -m "build(c28): B0 Dockerfile — kinoforge/wan-comfyui with build-time import smoke"
```

---

## Task 10: B1 — push pipeline (`pixi run build-image-wan-comfyui` + GH Actions)

**Goal:** A reproducible build/push pipeline. Local task for fast iteration; GitHub Actions workflow with `workflow_dispatch` for cleaner builds. Push to public `kinoforge/wan-comfyui:<tag>`.

**Files:**
- Modify: `pixi.toml` — add `[tasks] build-image-wan-comfyui`.
- Create: `.github/workflows/build-wan-comfyui-image.yml`.

**Acceptance Criteria:**
- [ ] `pixi run build-image-wan-comfyui` builds + pushes when env has `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN`.
- [ ] Tag scheme: `${COMFYUI_REF}-${KIJAI_SHA8}-cu124` + moving `latest`.
- [ ] GH Actions workflow is `workflow_dispatch` only (NOT on push).
- [ ] Workflow uses `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN` from GitHub Actions secrets.

**Verify:** `pixi run build-image-wan-comfyui --dry-run` (a `--help`-style no-op) prints the resolved tag without pushing.

**Steps:**

- [ ] **Step 1: Add pixi task.**

In `pixi.toml`:

```toml
[tasks]
build-image-wan-comfyui = """
  bash -c '
    set -euo pipefail
    TAG=${TAG:-v0.3.10-088128b2-cu124}
    docker build \
      --build-arg COMFYUI_REF=v0.3.10 \
      --build-arg KIJAI_WAN_REF=088128b224242e110d3906c6750e9a3a348a659b \
      --build-arg KJNODES_REF=369c8aee9ad4641823d0ffd7035076bcd297b6f2 \
      --build-arg VHS_REF=4ee72c065db22c9d96c2427954dc69e7b908444b \
      --build-arg IMAGE_TAG=${TAG} \
      -t kinoforge/wan-comfyui:${TAG} \
      -t kinoforge/wan-comfyui:latest \
      docker/wan-comfyui/
    echo \"${DOCKERHUB_TOKEN}\" | docker login --username \"${DOCKERHUB_USERNAME}\" --password-stdin
    docker push kinoforge/wan-comfyui:${TAG}
    docker push kinoforge/wan-comfyui:latest
  '
"""
```

- [ ] **Step 2: Add GH Actions workflow.**

```yaml
# .github/workflows/build-wan-comfyui-image.yml
name: build-wan-comfyui-image

on:
  workflow_dispatch:
    inputs:
      tag:
        description: "Image tag (e.g. v0.3.10-088128b2-cu124)"
        required: true
        default: "v0.3.10-088128b2-cu124"

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: docker/wan-comfyui
          push: true
          tags: |
            kinoforge/wan-comfyui:${{ inputs.tag }}
            kinoforge/wan-comfyui:latest
          build-args: |
            COMFYUI_REF=v0.3.10
            KIJAI_WAN_REF=088128b224242e110d3906c6750e9a3a348a659b
            KJNODES_REF=369c8aee9ad4641823d0ffd7035076bcd297b6f2
            VHS_REF=4ee72c065db22c9d96c2427954dc69e7b908444b
            IMAGE_TAG=${{ inputs.tag }}
```

- [ ] **Step 3: Run pixi task locally to validate build + push.**

Run (after `docker login` succeeded via env vars): `TAG=v0.3.10-088128b2-cu124 pixi run build-image-wan-comfyui`
Expected: image built + pushed to Docker Hub; tag visible at `hub.docker.com/r/kinoforge/wan-comfyui/tags`.

- [ ] **Step 4: Commit.**

```bash
git add pixi.toml pixi.lock .github/workflows/build-wan-comfyui-image.yml
git commit -m "build(c28): B1 build-image-wan-comfyui pixi task + GH Actions workflow_dispatch"
```

---

## Task 11: B2 — `render_provision` slim-mode branch

**Goal:** When `cfg.engine.comfyui.image` starts with `kinoforge/wan-comfyui:`, `render_provision` SKIPS `git clone ComfyUI`, the requirements `pip install`, every custom-node clone, and each custom-node `pip install`. Pure-additive — other image prefixes are unchanged.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py:render_provision`.
- Create: `tests/engines/comfyui/test_render_provision_slim_mode.py`.

**Acceptance Criteria:**
- [ ] When `cfg.engine.comfyui.image` starts with `kinoforge/wan-comfyui:`, the rendered script contains NO `git clone ComfyUI`, NO `pip install -r requirements.txt`, NO `git clone custom_nodes/`.
- [ ] When the image does NOT start with `kinoforge/wan-comfyui:`, output is byte-identical to the pre-task-11 baseline.
- [ ] Slim-mode script DOES still emit: selfterm bootstrap, model downloads, `exec python main.py`.

**Verify:** `pixi run pytest tests/engines/comfyui/test_render_provision_slim_mode.py -v` → 4+ passed.

**Steps:**

- [ ] **Step 1: Write failing tests.**

```python
# tests/engines/comfyui/test_render_provision_slim_mode.py
"""C28 B2 — render_provision slim-mode branch for kinoforge/wan-comfyui: images."""
from __future__ import annotations

from kinoforge.engines.comfyui import ComfyUIEngine

_CUSTOM_NODE_CFG = {
    "engine": {
        "comfyui": {
            "image": "{IMAGE}",
            "custom_nodes": [
                {"git": "https://github.com/kijai/ComfyUI-WanVideoWrapper", "ref": "abc"},
            ],
        }
    },
    "models": [],
}


def _render(image: str) -> str:
    eng = ComfyUIEngine()
    cfg = {**_CUSTOM_NODE_CFG, "engine": {**_CUSTOM_NODE_CFG["engine"]}}
    cfg["engine"]["comfyui"] = {**cfg["engine"]["comfyui"], "image": image}
    return eng.render_provision(cfg).script


def test_stock_image_still_clones_comfyui() -> None:
    script = _render("runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04")
    assert "git clone --depth 1 --branch" in script
    assert "ComfyUI/requirements.txt" in script


def test_prebake_image_skips_comfyui_clone() -> None:
    script = _render("kinoforge/wan-comfyui:v0.3.10-088128b2-cu124")
    assert "git clone --depth 1 --branch" not in script
    assert "pip install -q -r requirements.txt" not in script


def test_prebake_image_skips_custom_node_clones() -> None:
    script = _render("kinoforge/wan-comfyui:v0.3.10-088128b2-cu124")
    assert "git clone https://github.com/kijai/ComfyUI-WanVideoWrapper" not in script
    assert "custom_nodes/ComfyUI-WanVideoWrapper" not in script


def test_prebake_image_still_emits_selfterm_and_exec() -> None:
    script = _render("kinoforge/wan-comfyui:v0.3.10-088128b2-cu124")
    assert "KINOFORGE_SELFTERM_SCRIPT" in script
    assert "exec python main.py" in script
```

- [ ] **Step 2: Run tests, verify fail.**

Run: `pixi run pytest tests/engines/comfyui/test_render_provision_slim_mode.py -v`
Expected: 3 FAILED (test_stock_image stays green from the existing path).

- [ ] **Step 3: Implement slim-mode branch.**

In `render_provision`, after `image: str = comfyui_cfg.get("image", _DEFAULT_RUNPOD_IMAGE)` and before the `lines: list[str] = [` assembly, add:

```python
slim_mode: bool = image.startswith("kinoforge/wan-comfyui:")
```

Then wrap the four offending block segments in `if not slim_mode:`. Specifically:

- The line `f"[ ! -d ComfyUI ] && git clone --depth 1 --branch {branch} {repo} ComfyUI",`
- The line `"cd ComfyUI && pip install -q -r requirements.txt",`
- The entire `for node in custom_nodes:` loop body

Only emit them when `not slim_mode`. In slim mode, replace those with a single line:

```python
if slim_mode:
    lines.append("cd /workspace/ComfyUI")  # already exists in pre-baked image
```

- [ ] **Step 4: Run tests, verify pass.**

Run: `pixi run pytest tests/engines/comfyui/test_render_provision_slim_mode.py -v`
Expected: 4 passed.

- [ ] **Step 5: Re-run full render_provision regression suite — no regressions.**

Run: `pixi run pytest tests/engines/comfyui/ -v -k render_provision`
Expected: all green.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/engines/comfyui/__init__.py tests/engines/comfyui/test_render_provision_slim_mode.py
git commit -m "feat(c28): B2 render_provision slim-mode branch for kinoforge/wan-comfyui: images"
```

---

## Task 12: B3 — Phase B cfg + RED scaffold

**Goal:** Cfg + scaffold test for the Phase B image-prebake live smoke. RED-committed pre-spend.

**Files:**
- Create: `tests/live/cfg_c28_phase_b_prebake.yaml`.
- Create: `tests/live/test_c28_phase_b_image_prebake_live.py`.

**Acceptance Criteria:**
- [ ] Cfg file mirrors `cfg_c27_phase_b.yaml` but with `image: "kinoforge/wan-comfyui:v0.3.10-088128b2-cu124"`.
- [ ] Test gated on `KINOFORGE_LIVE_RUNPOD=1`; budget cap encoded `_BUDGET_USD_CAP = 0.30`.
- [ ] Test asserts: `wait_for_ready` 200, `RESTART_LOOP_REAP` does NOT fire, ONE asset produced.

**Verify:** `pixi run pytest tests/live/test_c28_phase_b_image_prebake_live.py -v` → 1 skipped.

**Steps:**

- [ ] **Step 1: Write cfg.**

Copy `tests/live/cfg_c27_phase_b.yaml`; change `image:` to the pre-baked tag; keep `restart_loop_reap_enabled: true` so the test exercises C27's predicate against the new image.

- [ ] **Step 2: Write scaffold test.**

```python
# tests/live/test_c28_phase_b_image_prebake_live.py
"""C28 Phase B live smoke — Wan + ComfyUI on the kinoforge-prebuilt image.

Cost cap: $0.30 (one full Wan gen on the pre-baked image).
Gated by KINOFORGE_LIVE_RUNPOD=1.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.30
_CFG = Path("tests/live/cfg_c28_phase_b_prebake.yaml")
_SIDECAR = Path("tests/live/_c28_phase_b_evidence.json")


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the Phase B image-prebake smoke "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


def test_c28_phase_b_image_prebake_live() -> None:
    """Boot on the kinoforge-prebuilt image; assert ready + asset + no reap."""
    run_id = f"c28-phase-b-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    proc = subprocess.run(
        [
            "pixi", "run", "kinoforge", "generate",
            "--config", str(_CFG),
            "--run-id", run_id,
            "--prompt-file", "/workspace/prompt-field-realistic.txt",
        ],
        capture_output=True,
        text=True,
        timeout=2400,
    )
    sidecar = {
        "outcome": "PROVEN" if proc.returncode == 0 else "FAILED",
        "captured_at": datetime.now().astimezone().isoformat(),
        "run_id": run_id,
        "returncode": proc.returncode,
        "stderr_tail": proc.stderr[-4000:],
        "stdout_tail": proc.stdout[-4000:],
    }
    _SIDECAR.write_text(json.dumps(sidecar, indent=2) + "\n")
    assert proc.returncode == 0, (
        f"kinoforge generate failed (rc={proc.returncode}); stderr tail:\n{proc.stderr[-2000:]}"
    )
    # Inspection of stdout for the expected ready + asset + no-reap markers
    assert "wait_for_ready returned" in proc.stdout
    assert "RESTART_LOOP_REAP" not in proc.stdout
    assert "Cancelled" not in proc.stderr
```

- [ ] **Step 3: Verify skipped without env.**

Run: `pixi run pytest tests/live/test_c28_phase_b_image_prebake_live.py -v`
Expected: 1 skipped.

- [ ] **Step 4: Commit (RED, pre-spend).**

```bash
git add tests/live/cfg_c28_phase_b_prebake.yaml tests/live/test_c28_phase_b_image_prebake_live.py
git commit -m "live(c28): B3 Phase B RED scaffold — image-prebake smoke + cfg"
```

---

## Task 13: B4 — Run Phase B live smoke

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Real Wan + ComfyUI cold pod on the pre-baked image reaches ready, generates one asset, C27 predicate stays silent. Closes the H1/H2/H4 portion of the spec's prevention surface.

**Files:**
- Run: `tests/live/test_c28_phase_b_image_prebake_live.py`.
- Update: `tests/live/_c28_phase_b_evidence.json`.

**Acceptance Criteria:**
- [ ] `pixi run preflight` exit 0 before smoke.
- [ ] Live smoke `outcome` field = `PROVEN`.
- [ ] `RESTART_LOOP_REAP` does NOT appear in stdout.
- [ ] `Cancelled` does NOT appear in stderr.
- [ ] One asset file produced under the configured output store.
- [ ] Sidecar contains `returncode`, `stderr_tail`, `stdout_tail`.
- [ ] Total spend ≤ $0.30.

**Verify:** `jq '.outcome' tests/live/_c28_phase_b_evidence.json` → `"PROVEN"`.

**Steps:**

- [ ] **Step 1: Preflight.**

Run: `pixi run preflight`
Expected: exit 0.

- [ ] **Step 2: Run live smoke.**

Run: `KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_c28_phase_b_image_prebake_live.py -v -s`
Expected: 1 passed; sidecar written.

- [ ] **Step 3: Commit evidence.**

```bash
git add tests/live/_c28_phase_b_evidence.json
git commit -m "live(c28): B4 Phase B PROVEN — kinoforge/wan-comfyui prebake passes Wan T2V cold boot"
```

---

## Task 14: C1 — `_kinoforge_download` helper + unit tests

**Goal:** Pure-bash helper function with 3-attempt retry, exponential backoff, sha verification, partial-file cleanup. Lives at the top of `render_provision` output unconditionally.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py:render_provision`.
- Create: `tests/engines/comfyui/test_render_provision_kinoforge_download.py`.

**Acceptance Criteria:**
- [ ] Rendered script always contains the `_kinoforge_download` helper function definition.
- [ ] Helper retries up to 3 times.
- [ ] Helper uses exponential backoff: 5s, 10s, 15s.
- [ ] Helper cleans `${out}.partial` between attempts.
- [ ] Helper does sha verify when called with a third arg.
- [ ] Helper passes `-H "Authorization: Bearer $HF_TOKEN"` only when `$HF_TOKEN` is set.

**Verify:** `pixi run pytest tests/engines/comfyui/test_render_provision_kinoforge_download.py -v` → 6+ passed.

**Steps:**

- [ ] **Step 1: Write failing tests.**

```python
# tests/engines/comfyui/test_render_provision_kinoforge_download.py
"""C28 C1 — _kinoforge_download helper rendered into provision script."""
from __future__ import annotations

from kinoforge.engines.comfyui import ComfyUIEngine

_MIN_CFG = {"engine": {"comfyui": {}}, "models": []}


def _render() -> str:
    return ComfyUIEngine().render_provision(_MIN_CFG).script


def test_helper_present() -> None:
    assert "_kinoforge_download()" in _render()


def test_helper_three_attempts() -> None:
    script = _render()
    # match the loop header exactly to avoid false positives
    assert "for attempt in 1 2 3" in script


def test_helper_exponential_backoff() -> None:
    script = _render()
    assert "sleep $((5 * attempt))" in script


def test_helper_cleans_partial_between_attempts() -> None:
    script = _render()
    assert 'rm -f "${out}.partial"' in script


def test_helper_sha_verify_branch() -> None:
    script = _render()
    assert "sha256sum" in script
    assert "$actual" in script
    assert "$expected_sha" in script


def test_helper_authorization_optional() -> None:
    script = _render()
    # bash parameter expansion form so HF_TOKEN absence skips the header
    assert "${HF_TOKEN:+-H \"Authorization: Bearer $HF_TOKEN\"}" in script
```

- [ ] **Step 2: Run, verify fail.**

Run: `pixi run pytest tests/engines/comfyui/test_render_provision_kinoforge_download.py -v`
Expected: 6 FAILED.

- [ ] **Step 3: Add helper to `render_provision`.**

After the trap pre-amble logic (Task 3), before the `lines: list[str] = [` assembly, insert the helper template:

```python
_KINOFORGE_DOWNLOAD_HELPER = [
    "_kinoforge_download() {",
    "  local url=$1; local out=$2; local expected_sha=${3:-}",
    "  local attempt",
    "  for attempt in 1 2 3; do",
    '    rm -f "${out}.partial"',
    '    if curl -L --fail --retry 0 -C - \\',
    '         ${HF_TOKEN:+-H "Authorization: Bearer $HF_TOKEN"} \\',
    '         "$url" -o "${out}.partial"; then',
    '      if [ -n "$expected_sha" ]; then',
    "        local actual",
    "        actual=$(sha256sum \"${out}.partial\" | awk '{print $1}')",
    '        if [ "$actual" != "$expected_sha" ]; then',
    '          echo "sha mismatch attempt $attempt: $actual vs $expected_sha" >&2',
    "          sleep $((5 * attempt))",
    "          continue",
    "        fi",
    "      fi",
    '      mv "${out}.partial" "$out"',
    "      return 0",
    "    fi",
    "    sleep $((5 * attempt))",
    "  done",
    "  return 1",
    "}",
]
```

Add it to `lines` after the trap pre-amble (or right after `set -euo pipefail` in non-diagnostic mode), before the selfterm bootstrap.

- [ ] **Step 4: Run tests, verify pass.**

Run: `pixi run pytest tests/engines/comfyui/test_render_provision_kinoforge_download.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/engines/comfyui/__init__.py tests/engines/comfyui/test_render_provision_kinoforge_download.py
git commit -m "feat(c28): C1 _kinoforge_download helper — retry+backoff+sha+partial-cleanup"
```

---

## Task 15: C2 — Replace inline curl with helper call

**Goal:** Every `curl -L --fail` in the model-download loop becomes a `_kinoforge_download` call. Threads `models[i].sha256` (or empty string when unknown) as the verify arg.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py:render_provision` (model-download loop).
- Update test: `tests/engines/comfyui/test_render_provision_kinoforge_download.py`.

**Acceptance Criteria:**
- [ ] Model-download loop emits `_kinoforge_download '<url>' '<out>' '<sha_or_empty>'` instead of inline `curl`.
- [ ] When `models[i].sha256` is present, the third arg is the sha string; when absent, the third arg is `""`.
- [ ] Existing fixture tests for `render_provision` still pass.

**Verify:** `pixi run pytest tests/engines/comfyui/ -v -k 'render_provision or kinoforge_download'` → all green.

**Steps:**

- [ ] **Step 1: Add inline-replacement assertion to existing test file.**

Append to `test_render_provision_kinoforge_download.py`:

```python
def test_model_loop_uses_helper_not_inline_curl() -> None:
    cfg = {
        "engine": {"comfyui": {}},
        "models": [
            {
                "ref": "hf:test/wan:wan-14b.safetensors",
                "kind": "base",
                "target": "diffusion_models",
            }
        ],
    }
    script = ComfyUIEngine().render_provision(cfg).script
    # Old inline form must be gone for model downloads
    assert "curl -L --fail" not in script
    # New helper-call form must be present
    assert "_kinoforge_download " in script
```

- [ ] **Step 2: Run test, verify fail (inline curl still in script).**

Run: `pixi run pytest tests/engines/comfyui/test_render_provision_kinoforge_download.py::test_model_loop_uses_helper_not_inline_curl -v`
Expected: FAIL.

- [ ] **Step 3: Replace inline curl in `render_provision` model loop.**

In `render_provision` (around the `for entry in models_raw:` block), replace:

```python
lines.append(
    f"[ ! -f {subdir}/{filename} ] && "
    f"curl -L --fail{auth_header} '{artifact.url}' -o {subdir}/{filename}"
)
```

with:

```python
sha = artifact.sha256 or ""
lines.append(
    f"[ ! -f {subdir}/{filename} ] && "
    f"_kinoforge_download '{artifact.url}' '{subdir}/{filename}' '{sha}'"
)
```

Drop the unused `auth_header` variable (helper now handles `HF_TOKEN` internally via `${HF_TOKEN:+-H ...}`).

- [ ] **Step 4: Run full render_provision suite.**

Run: `pixi run pytest tests/engines/comfyui/ -v -k render_provision`
Expected: all green (existing tests + 7 new ones).

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/engines/comfyui/__init__.py tests/engines/comfyui/test_render_provision_kinoforge_download.py
git commit -m "refactor(c28): C2 render_provision model loop calls _kinoforge_download (retry+sha)"
```

---

## Task 16: C3 — Phase C live smoke (curl-retry path exercised)

**Goal:** Live smoke that re-uses the Phase B pod and forces the retry path by replacing one model URL with a known-404 endpoint. Asserts three attempts, exponential backoff, clean failure, no partial files left behind. Cheap because pod is shared.

**Files:**
- Create: `tests/live/test_c28_phase_c_curl_retry_live.py`.
- Create: `tests/live/_c28_phase_c_evidence.json` (created by the test).

**Acceptance Criteria:**
- [ ] Test gated on `KINOFORGE_LIVE_RUNPOD=1`.
- [ ] Test triggers the retry helper via a forced 404 (e.g. unreachable HF URL on a known-404 model ref).
- [ ] Captures pod stdout via the C27 heartbeat hook or by SSH-fetching `/tmp/boot.log`.
- [ ] Asserts log shows three attempts with timestamps ~5s, ~10s, ~15s apart.
- [ ] Asserts no `.partial` files remain under `models/diffusion_models/`.

**Verify:** `pixi run pytest tests/live/test_c28_phase_c_curl_retry_live.py -v` → 1 skipped (no live env).

**Steps:**

- [ ] **Step 1: Write the test.**

```python
# tests/live/test_c28_phase_c_curl_retry_live.py
"""C28 Phase C live smoke — _kinoforge_download helper exercises 3-attempt retry."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.05
_CFG = Path("tests/live/cfg_c28_phase_c_curl_retry.yaml")
_SIDECAR = Path("tests/live/_c28_phase_c_evidence.json")


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the Phase C curl-retry smoke "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


def test_c28_phase_c_curl_retry_live() -> None:
    """Force a model URL 404 and verify the helper retries 3x with backoff."""
    run_id = f"c28-phase-c-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    proc = subprocess.run(
        [
            "pixi", "run", "kinoforge", "generate",
            "--config", str(_CFG),
            "--run-id", run_id,
            "--prompt-file", "/workspace/prompt-field-realistic.txt",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    # We EXPECT non-zero — the forced 404 makes the helper return 1 → script exits
    # via set -e. The behavioural assertion is on stderr/stdout shape, not rc.
    log = proc.stdout + proc.stderr
    # Three attempts with expected backoff
    attempts = log.count("sha mismatch attempt") + log.count("curl: (")
    assert attempts >= 3, f"expected at least 3 retry signals, got {attempts}:\n{log[-2000:]}"

    sidecar = {
        "outcome": "PROVEN" if attempts >= 3 else "FAILED",
        "captured_at": datetime.now().astimezone().isoformat(),
        "run_id": run_id,
        "attempts_observed": attempts,
        "returncode": proc.returncode,
        "log_tail": log[-4000:],
    }
    _SIDECAR.write_text(json.dumps(sidecar, indent=2) + "\n")
```

- [ ] **Step 2: Create the cfg with a forced 404.**

```yaml
# tests/live/cfg_c28_phase_c_curl_retry.yaml
# Mirrors cfg_c28_phase_b_prebake.yaml but swaps one model ref for a
# guaranteed-404 to exercise the C28 C1/C2 retry helper.

engine:
  kind: comfyui
  comfyui:
    image: "kinoforge/wan-comfyui:v0.3.10-088128b2-cu124"
    custom_nodes: []  # slim mode — image carries them

models:
  - ref: "hf:nonexistent-user/nonexistent-repo:nonexistent.safetensors"
    kind: base
    target: diffusion_models

compute:
  provider: runpod
  image: "kinoforge/wan-comfyui:v0.3.10-088128b2-cu124"
  mode: pod
  requirements:
    min_vram_gb: 24
    min_cuda: "12.4"
    max_usd_per_hr: 0.50
    disk_gb: 80
  lifecycle:
    idle_timeout: 5m
    job_timeout: 5m
    time_buffer: 1m
    max_lifetime: 15m
    boot_timeout: 5m
    budget: 0.05
    heartbeat_interval_s: 15
    restart_loop_reap_enabled: true
    restart_loop_window_s: 180
    restart_loop_uptime_threshold_s: 90

# spec/params copied from cfg_c27_phase_b.yaml — they are not exercised
# because boot fails on the 404 download
```

- [ ] **Step 3: Verify scaffold skipped without env.**

Run: `pixi run pytest tests/live/test_c28_phase_c_curl_retry_live.py -v`
Expected: 1 skipped.

- [ ] **Step 4: Commit RED scaffold.**

```bash
git add tests/live/cfg_c28_phase_c_curl_retry.yaml tests/live/test_c28_phase_c_curl_retry_live.py
git commit -m "live(c28): C3 Phase C RED scaffold — forced-404 retry-helper smoke"
```

- [ ] **Step 5: Run live (after Task 13 closed so pre-baked image is on Docker Hub).**

Run: `KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_c28_phase_c_curl_retry_live.py -v -s`
Expected: 1 passed; sidecar `_c28_phase_c_evidence.json` written with `outcome: PROVEN`.

- [ ] **Step 6: Commit evidence.**

```bash
git add tests/live/_c28_phase_c_evidence.json
git commit -m "live(c28): C3 Phase C PROVEN — _kinoforge_download retries 3x with backoff"
```

---

## Task 17: Spec-level acceptance — three consecutive cold boots + C27 PB re-fire

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Demonstrate the prevention surface holds: three consecutive Wan + ComfyUI cold-pod boots reach `wait_for_ready` 200 with C27's predicate silent on all three; ONE re-fire of the C27 Phase B test flips its acceptance from `PROVEN-PROTECTION` → `PROVEN` (gen2 cold-skip ratio < 0.7), closing the deferred C25 Task 4 / C26 Task 14 generation gate.

**Files:**
- Create: `tests/live/test_c28_spec_acceptance_live.py`.
- Create: `tests/live/_c28_spec_acceptance_evidence.json`.

**Acceptance Criteria:**
- [ ] Three consecutive cold pod boots from `cfg_c28_phase_b_prebake.yaml` reach `wait_for_ready` 200 in < `boot_timeout_s`.
- [ ] On all three boots, `consecutive_low_uptime_count` (from the ledger touch records) stays at 0 throughout the boot.
- [ ] `tests/live/test_c27_phase_b_wan_warm_reuse_live.py` re-fired with cfg flipped to `cfg_c28_phase_b_prebake.yaml` produces sidecar with `acceptance_path: "PROVEN"` and `cold_skip_ratio < 0.7`.
- [ ] Total spend ≤ $0.30.

**Verify:** `jq '.outcome' tests/live/_c28_spec_acceptance_evidence.json` → `"PROVEN"`.

**Steps:**

- [ ] **Step 1: Write the spec-acceptance test.**

```python
# tests/live/test_c28_spec_acceptance_live.py
"""C28 spec-level acceptance — 3 cold boots + C27 PB re-fire on prebake cfg."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.30
_CFG = Path("tests/live/cfg_c28_phase_b_prebake.yaml")
_SIDECAR = Path("tests/live/_c28_spec_acceptance_evidence.json")


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the C28 spec-level acceptance smoke "
            f"(~${_BUDGET_USD_CAP} spend per invocation)"
        )


def test_c28_spec_acceptance_live() -> None:
    """Three consecutive cold boots + C27 PB re-fire flips PROVEN-PROTECTION → PROVEN."""
    boot_results = []
    for i in range(1, 4):
        run_id = f"c28-spec-cold-{i}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
        proc = subprocess.run(
            [
                "pixi", "run", "kinoforge", "generate",
                "--config", str(_CFG),
                "--run-id", run_id,
                "--prompt-file", "/workspace/prompt-field-realistic.txt",
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        boot_results.append({
            "boot_n": i,
            "run_id": run_id,
            "returncode": proc.returncode,
            "restart_loop_observed": "RESTART_LOOP_REAP" in proc.stdout,
            "wait_for_ready_observed": "wait_for_ready returned" in proc.stdout,
        })
        assert proc.returncode == 0, (
            f"boot {i} failed (rc={proc.returncode}); stderr tail:\n{proc.stderr[-2000:]}"
        )
        assert "RESTART_LOOP_REAP" not in proc.stdout

    # C27 PB re-fire — re-use the same prebake cfg
    refire = subprocess.run(
        [
            "pixi", "run", "pytest",
            "tests/live/test_c27_phase_b_wan_warm_reuse_live.py",
            "-v", "-s",
            "--", "--c28-cfg-override",
        ],
        env={**os.environ, "KINOFORGE_C27_PB_CFG": str(_CFG)},
        capture_output=True,
        text=True,
        timeout=3600,
    )
    sidecar = {
        "outcome": "PROVEN" if refire.returncode == 0 else "FAILED",
        "captured_at": datetime.now().astimezone().isoformat(),
        "cold_boots": boot_results,
        "c27_pb_refire_returncode": refire.returncode,
        "c27_pb_refire_log_tail": (refire.stdout + refire.stderr)[-4000:],
    }
    _SIDECAR.write_text(json.dumps(sidecar, indent=2) + "\n")
    assert refire.returncode == 0
```

- [ ] **Step 2: Commit RED scaffold.**

```bash
git add tests/live/test_c28_spec_acceptance_live.py
git commit -m "live(c28): spec-acceptance RED scaffold — 3 cold boots + C27 PB re-fire"
```

- [ ] **Step 3: Preflight + run live.**

Run: `pixi run preflight && KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_c28_spec_acceptance_live.py -v -s`
Expected: 1 passed; sidecar `outcome: PROVEN`.

- [ ] **Step 4: Commit evidence.**

```bash
git add tests/live/_c28_spec_acceptance_evidence.json
git commit -m "live(c28): spec-acceptance PROVEN — closes deferred C25/C26 generation gate"
```

---

## Task 18: D1-D4 — Closeout

**Goal:** Close C28 in the project bookkeeping. PROGRESS.md C28 entry; backlinks in C26 §17 and C27 §13; entry (or "See also") in successful-generations.md.

**Files:**
- Modify: `PROGRESS.md`.
- Modify: `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md`.
- Modify: `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md`.
- Modify: `docs/successful-generations.md`.

**Acceptance Criteria:**
- [ ] PROGRESS.md has the C28 §C entry per spec §8.
- [ ] C26 spec §17 has a one-line C28 closure pointer.
- [ ] C27 spec §13 has a one-line C28 closure pointer.
- [ ] successful-generations.md updated per the CLAUDE.md rule (new section OR "See also" line).

**Verify:** `rg -c 'C28' PROGRESS.md docs/superpowers/specs/2026-06-13-c2[67]-*.md` → counts ≥ 1 on each.

**Steps:**

- [ ] **Step 1: PROGRESS.md C28 entry.**

Edit `PROGRESS.md` §C; insert at the §C top (above C27 entry):

```markdown
- **C28. RunPod container-restart-loop prevention.** CLOSED. Spec:
  `docs/superpowers/specs/2026-06-13-c28-restart-loop-prevention-design.md`.
  Plan: `docs/superpowers/plans/2026-06-13-c28-restart-loop-prevention.md`.
  Diagnostic-first uplift: S3 PUT in `EXIT` trap (`boot-logs/<pod>/` with
  7-day lifecycle) + restart-policy=Never (when RunPod input supports it)
  + classify table maps `last_line` → hypothesis. Structural fixes gated
  on Phase A evidence: image pre-bake (kinoforge/wan-comfyui Docker Hub
  public) + curl retry + sha verify in render_provision. Phase A through
  spec-level smokes all PROVEN. Closes the C27-protected-but-unfixed
  restart-loop class. Closes deferred C25 Task 4 / C26 Task 14
  generation gate.
```

- [ ] **Step 2: C27 §13 backlink.**

Append to `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md` end-of-§13:

```markdown
- C28 closes the restart-loop class C27 protected against. See
  `2026-06-13-c28-restart-loop-prevention-design.md` §8.
```

- [ ] **Step 3: C26 §17 backlink.**

Append to `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md` §17:

```markdown
- C28 closes the deferred Task 14 generation gate via image pre-bake +
  curl retry hardening. See
  `2026-06-13-c28-restart-loop-prevention-design.md` §8.
```

- [ ] **Step 4: successful-generations.md.**

Per CLAUDE.md durability rule: tuple is `(runpod, comfyui, wan-2.1-14b, t2v)`. If an existing section covers this tuple, append a "See also" line citing the new `engine_variant` (image-prebake). Otherwise add a new section per the file preamble's schema.

- [ ] **Step 5: Commit everything.**

```bash
git add PROGRESS.md docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md docs/successful-generations.md
git commit -m "docs(c28): closeout — PROGRESS §C C28 CLOSED + C26 §17 + C27 §13 backlinks + successful-generations entry"
```

---

## Self-review

- **Spec coverage:**
  - §3 hypothesis table → Task 7 (A5 classification consumes it).
  - §4 phase architecture → Tasks 1-17 follow A/B/C/D.
  - §5 A0/A1/A2/A3/A4/A5 → Tasks 1-7.
  - §6 B0/B1/B2/B3/B4/B5 → Tasks 9-13 (B5 is deferred to C29 per spec; out of scope here).
  - §7 C1/C2/C3 → Tasks 14-16.
  - §8 D1/D2/D3/D4 → Task 18.
  - §9 acceptance gates → Task 17 (spec-level smoke).
  - §10 risks → mitigations in respective tasks (retry policy in Task 6; `|| true` in Task 3; schema-gated wire in Task 5).
  - §11 out-of-scope → respected (no network volume, no `registryAuthId`).
  - §12 wire-discovery → Task 1 (A0 probe).
  - §13 budget → per-task cost caps documented in `_BUDGET_USD_CAP`.
  - §15 plan ordering → Tasks 1-18 follow the spec's suggested sequence.

- **Placeholder scan:** No TBD/TODO/"fill in later" markers in steps. The one deliberate placeholder is in Task 9's Dockerfile (`# Pin transitive deps identified by A5 evidence` is an explicit gating placeholder filled in at the moment A5 runs — flagged in the comment, not hidden).

- **Type consistency:** `InstanceSpec.diagnostic_env` (Task 4), `InstanceSpec.restart_policy` (Task 5), `_RUNPOD_SCHEMA_SIDECAR` (Task 5), `_kinoforge_download` (Tasks 14-15) names match across tasks.

User-gate tasks: Tasks 7 (A4+A5 live smoke), 8 (gate decision), 13 (B4 live smoke), 17 (spec-level acceptance). All four carry the USER-ORDERED GATE banner.
