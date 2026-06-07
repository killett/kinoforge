# Layer W+α — Cloud Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land every AWS + GCP permission and GPU quota the SkyPilot multi-cloud T4 smoke (Layer W+β) needs, with zero spend, two operator console actions, and idempotent re-runnable probes.

**Architecture:** A single `tools/cloud_perms_probe.py` mirrors the seam-injection pattern of `tools/preflight.py` — every SDK call goes through a factory callable so tests inject fakes. Probes write `.aws/perms-snapshot.json` and `.gcp/perms-snapshot.json` (gitignored) with caller identity, IAM simulation results, instance-type sanity, and quota readings. Operator actions are gated by chat confirmation, NOT by detecting state in code. The probe exits 0 only when all probes report green.

**Tech Stack:** Python 3.13 stdlib + `boto3` (already in pixi env via Layer W) + `google-cloud-compute` + `google-cloud-service-usage` + `google-cloud-resource-manager` (verify already pulled in by Phase 31) + existing `pixi.toml [activation.env]` default chains.

**Spec:** `docs/superpowers/specs/2026-06-06-layer-w-alpha-cloud-bootstrap-design.md`

**Spend ceiling:** $0. Zero `sky launch`, zero EC2/GCE instances. `sky check` is offline metadata; AWS quota increase requests cost $0; GCP quota requests are operator-driven via console.

---

## File structure

| Path | Action | Responsibility |
|---|---|---|
| `.aws/policies/skypilot-minimal.json` | CREATE (tracked) | Scoped IAM policy doc — EC2 lifecycle + IAM PassRole + ServiceQuotas + S3 prefix + KMS key |
| `.aws/README.md` | MODIFY | Apply instructions (T2) + rotation note (T7) |
| `.aws/perms-snapshot.json` | CREATE (gitignored) | T3 output: identity, simulated actions, quota |
| `.gcp/perms-snapshot.json` | CREATE (gitignored) | T4 output: identity, SA roles, quota, region metadata |
| `tools/cloud_perms_probe.py` | CREATE (tracked) | Probe entry point — mirrors `tools/preflight.py` seam style |
| `tests/tools/test_cloud_perms_probe.py` | CREATE (tracked) | Unit tests with injected fake boto3 + fake google-cloud clients |
| `tests/tools/fixtures/cloud_perms/` | CREATE (tracked) | JSON fixtures captured at live-probe time (redacted) |
| `pixi.toml` | MODIFY | Add `cloud:perms-probe` task; pin google-cloud-* deps if missing |
| `docs/CLOUD-CREDS.md` | MODIFY | Add SkyPilot perms rows + section + sky check appendix |
| `PROGRESS.md` | MODIFY | Add Phase 39 entry |
| `README.md` | MODIFY | One-paragraph pointer to the bootstrap layer |

Files that change together (the probe + its tests + fixtures) live together under `tools/` + `tests/tools/`. Each task touches a focused slice; no task spans more than three files outside of the final docs task.

---

## Task 1 — Draft AWS scoped IAM policy

**Goal:** Write `.aws/policies/skypilot-minimal.json` covering SkyPilot's documented AWS minimum + kinoforge's S3 bucket prefix + Layer W KMS key. Policy is tracked because it carries no secrets.

**Files:**
- Create: `.aws/policies/skypilot-minimal.json`

**Acceptance Criteria:**
- [ ] EC2 statement covers: `Describe*`, `RunInstances`, `TerminateInstances`, `CreateTags`, `DeleteTags`, `Create/Delete/Modify*` for VPC / Subnet / SG / Route / IGW / EIP / VolumeAttachment
- [ ] IAM statement scoped to roles/profiles named `skypilot-*` or `sky-*`: `Create/DeleteRole`, `Create/DeleteInstanceProfile`, `AddRoleToInstanceProfile`, `RemoveRoleFromInstanceProfile`, `PutRolePolicy`, `AttachRolePolicy`, `DetachRolePolicy`, `PassRole`, `GetRole`, `GetInstanceProfile`, `ListRolePolicies`, `ListInstanceProfilesForRole`
- [ ] ServiceQuotas statement: `GetServiceQuota`, `ListServiceQuotas`, `RequestServiceQuotaIncrease`, `ListRequestedServiceQuotaChangeHistory`
- [ ] S3 statement scoped to `arn:aws:s3:::kinoforge-realcloud-tests-*`, `arn:aws:s3:::skypilot-*` (+ object children) — bucket + object operations
- [ ] KMS statement scoped to `arn:aws:kms:us-east-1:<AWS_ACCOUNT>:key/<id>` for `alias/kinoforge-realcloud-tests`: `Encrypt`, `Decrypt`, `GenerateDataKey`, `DescribeKey`
- [ ] JSON parses cleanly (`python -c "import json; json.load(open('.aws/policies/skypilot-minimal.json'))"`)
- [ ] No `Resource: "*"` on data-bearing actions (only on `Describe*` and read-only metadata)

**Verify:** `python -c "import json; doc=json.load(open('.aws/policies/skypilot-minimal.json')); assert doc['Version']=='2012-10-17'; print(len(doc['Statement']), 'statements')"` → prints 5 (or however many sections the final policy has).

**Steps:**

- [ ] **Step 1.1: Read the existing KMS key ARN to scope the KMS statement.**

```bash
cat /workspace/.aws/kms-test-key.arn
```

Capture the ARN; it lands in the policy KMS resource list verbatim.

- [ ] **Step 1.2: Write `.aws/policies/skypilot-minimal.json`.**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EC2LifecycleRead",
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "ec2:GetConsoleOutput"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EC2LifecycleWrite",
      "Effect": "Allow",
      "Action": [
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:StopInstances",
        "ec2:StartInstances",
        "ec2:CreateTags",
        "ec2:DeleteTags",
        "ec2:CreateKeyPair",
        "ec2:DeleteKeyPair",
        "ec2:ImportKeyPair",
        "ec2:CreateSecurityGroup",
        "ec2:DeleteSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupEgress",
        "ec2:CreateVpc",
        "ec2:DeleteVpc",
        "ec2:ModifyVpcAttribute",
        "ec2:CreateSubnet",
        "ec2:DeleteSubnet",
        "ec2:ModifySubnetAttribute",
        "ec2:CreateInternetGateway",
        "ec2:DeleteInternetGateway",
        "ec2:AttachInternetGateway",
        "ec2:DetachInternetGateway",
        "ec2:CreateRouteTable",
        "ec2:DeleteRouteTable",
        "ec2:AssociateRouteTable",
        "ec2:DisassociateRouteTable",
        "ec2:CreateRoute",
        "ec2:DeleteRoute",
        "ec2:AllocateAddress",
        "ec2:ReleaseAddress",
        "ec2:AssociateAddress",
        "ec2:DisassociateAddress",
        "ec2:AttachVolume",
        "ec2:DetachVolume",
        "ec2:CreateVolume",
        "ec2:DeleteVolume",
        "ec2:ModifyVolume",
        "ec2:CreateNetworkInterface",
        "ec2:DeleteNetworkInterface",
        "ec2:AttachNetworkInterface",
        "ec2:DetachNetworkInterface",
        "ec2:ModifyNetworkInterfaceAttribute"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAMForSkyPilotRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PassRole",
        "iam:CreateInstanceProfile",
        "iam:DeleteInstanceProfile",
        "iam:GetInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile",
        "iam:ListInstanceProfilesForRole",
        "iam:ListRolePolicies",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:ListAttachedRolePolicies"
      ],
      "Resource": [
        "arn:aws:iam::<AWS_ACCOUNT>:role/skypilot-*",
        "arn:aws:iam::<AWS_ACCOUNT>:role/sky-*",
        "arn:aws:iam::<AWS_ACCOUNT>:instance-profile/skypilot-*",
        "arn:aws:iam::<AWS_ACCOUNT>:instance-profile/sky-*"
      ]
    },
    {
      "Sid": "IAMReadCallerIdentity",
      "Effect": "Allow",
      "Action": [
        "iam:GetUser",
        "iam:SimulatePrincipalPolicy",
        "iam:ListAccessKeys",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ServiceQuotas",
      "Effect": "Allow",
      "Action": [
        "servicequotas:GetServiceQuota",
        "servicequotas:ListServiceQuotas",
        "servicequotas:RequestServiceQuotaIncrease",
        "servicequotas:ListRequestedServiceQuotaChangeHistory",
        "servicequotas:GetRequestedServiceQuotaChange"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3KinoforgeBuckets",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:GetBucketVersioning",
        "s3:PutBucketVersioning",
        "s3:GetBucketAcl",
        "s3:PutBucketAcl",
        "s3:GetBucketPolicy",
        "s3:PutBucketPolicy",
        "s3:GetBucketCors",
        "s3:PutBucketCors",
        "s3:GetEncryptionConfiguration",
        "s3:PutEncryptionConfiguration",
        "s3:GetObject",
        "s3:GetObjectAcl",
        "s3:GetObjectTagging",
        "s3:PutObject",
        "s3:PutObjectAcl",
        "s3:PutObjectTagging",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts",
        "s3:ListBucketMultipartUploads"
      ],
      "Resource": [
        "arn:aws:s3:::kinoforge-realcloud-tests-*",
        "arn:aws:s3:::kinoforge-realcloud-tests-*/*",
        "arn:aws:s3:::skypilot-*",
        "arn:aws:s3:::skypilot-*/*"
      ]
    },
    {
      "Sid": "KMSLayerW",
      "Effect": "Allow",
      "Action": [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:ReEncrypt*",
        "kms:GenerateDataKey",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:DescribeKey"
      ],
      "Resource": [
        "<ARN-FROM-.aws/kms-test-key.arn>"
      ]
    },
    {
      "Sid": "S3ListAll",
      "Effect": "Allow",
      "Action": [
        "s3:ListAllMyBuckets"
      ],
      "Resource": "*"
    }
  ]
}
```

Replace `<ARN-FROM-.aws/kms-test-key.arn>` with the literal ARN from Step 1.1.

- [ ] **Step 1.3: Verify the JSON parses + statement count.**

```bash
python -c "import json; doc=json.load(open('.aws/policies/skypilot-minimal.json')); print(doc['Version'], len(doc['Statement']), 'statements')"
```

Expected: `2012-10-17 8 statements` (or whatever the final count is — assert internally that no `Statement` block has a missing `Resource` / `Action` / `Effect`).

- [ ] **Step 1.4: Commit.**

```bash
git add .aws/policies/skypilot-minimal.json
git commit -m "$(cat <<'EOF'
feat(aws): scoped SkyPilot IAM policy doc

Layer W+α T1. Tracked policy bytes (no secrets). EC2 lifecycle +
IAM PassRole on skypilot-* + ServiceQuotas + S3/KMS scoped to
kinoforge-realcloud-tests-* and skypilot-* buckets.
EOF
)"
```

---

## Task 2 — Apply policy + USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after the operator has confirmed in chat that the policy is attached AND `sts.get_caller_identity` confirms the new policy resolves.

**Goal:** Write apply instructions to `.aws/README.md` and pause for the operator to attach the policy via the AWS IAM Console. No code runs after this point until the user confirms.

**Files:**
- Modify: `.aws/README.md`

**Acceptance Criteria:**
- [ ] `.aws/README.md` contains a `## SkyPilot policy — apply instructions` section
- [ ] Section names the IAM console path: Users → `kinoforge-ci` → Add permissions → Create policy → JSON → paste contents of `.aws/policies/skypilot-minimal.json` → name it `KinoforgeSkypilotMinimal` → attach
- [ ] Section explicitly says: leave `AmazonS3FullAccess` attached until T3 reports green; then detach
- [ ] Operator confirms in chat that the policy is attached (one ack, free text or yes/no)

**Verify:** Operator says "policy attached" in chat. The probe in T3 will catch a missed attach (simulate fails → exit 1).

**Steps:**

- [ ] **Step 2.1: Read existing `.aws/README.md` to find the right insertion point.**

```bash
cat /workspace/.aws/README.md
```

The new section goes after the existing bootstrap section, before the rotation section.

- [ ] **Step 2.2: Append the apply-instructions section to `.aws/README.md`.**

Append (or insert at the appropriate point):

```markdown
## SkyPilot policy — apply instructions (Layer W+α T2)

The scoped IAM policy doc lives at
`.aws/policies/skypilot-minimal.json` (tracked, not secret).

To attach it to the existing `kinoforge-ci` IAM user:

1. Open the [AWS IAM Console → Users](https://us-east-1.console.aws.amazon.com/iam/home#/users) — account `<AWS_ACCOUNT>`.
2. Click `kinoforge-ci`.
3. Permissions tab → **Add permissions** → **Create inline policy** (or
   **Attach policies directly → Create policy**).
4. JSON tab → paste the entire contents of
   `.aws/policies/skypilot-minimal.json`.
5. Review → name it `KinoforgeSkypilotMinimal` → Create policy.
6. Confirm the policy is now attached to `kinoforge-ci`.

Leave `AmazonS3FullAccess` attached until `pixi run cloud:perms-probe`
exits 0 against AWS. Once green, detach `AmazonS3FullAccess`:

> Users → `kinoforge-ci` → Permissions → checkbox `AmazonS3FullAccess` →
> Remove.

The scoped policy covers all S3 operations kinoforge needs against the
`kinoforge-realcloud-tests-*` prefix; broader S3 access is no longer
required.
```

- [ ] **Step 2.3: Commit the README edit.**

```bash
git add .aws/README.md
git commit -m "$(cat <<'EOF'
docs(aws): SkyPilot policy apply + scope-down instructions

Layer W+α T2. Operator-facing console instructions for attaching
.aws/policies/skypilot-minimal.json to kinoforge-ci.
EOF
)"
```

- [ ] **Step 2.4: Pause for operator confirmation.**

Post in chat:

> Policy doc committed. Please attach `KinoforgeSkypilotMinimal` to
> `kinoforge-ci` in the AWS IAM Console (steps in `.aws/README.md`).
> Reply "policy attached" when done.

Do NOT proceed to Task 3 until the operator confirms.

```json:metadata
{"userGate": true, "tags": ["user-gate"], "files": [".aws/README.md"], "verifyCommand": "aws sts get-caller-identity", "acceptanceCriteria": ["README contains apply instructions", "Operator confirms policy attached in chat"], "requiresUserSpecification": false}
```

---

## Task 3 — AWS probe

**Goal:** `tools/cloud_perms_probe.py --cloud aws` runs SDK probes, writes `.aws/perms-snapshot.json`, and returns exit code 0 (green), 1 (auth/denial), or 2 (quota gap).

**Files:**
- Create: `tools/cloud_perms_probe.py`
- Create: `tests/tools/test_cloud_perms_probe.py`
- Create: `tests/tools/fixtures/cloud_perms/aws_green.json` (after live capture in Step 3.6)

**Acceptance Criteria:**
- [ ] `probe_aws(session)` returns a dict with keys `captured_at`, `cloud`, `identity`, `simulated`, `instance_type`, `quotas`, `exit_code`
- [ ] `identity` matches `kinoforge-ci` ARN
- [ ] `simulated` returns `"allowed"` for every required action (full list in Step 3.3)
- [ ] `instance_type["g4dn.xlarge"]` lookup succeeds
- [ ] `quotas["L-DB2E81BA"]` captured with `value`, `code`, `name`
- [ ] Exit 0 on all green; 1 on auth fail; 2 on quota < 4
- [ ] Snapshot written via temp-file + rename for atomic update
- [ ] Tests inject a fake `boto3.Session` — no real AWS calls in CI
- [ ] Live probe run once by Claude; snapshot committed (gitignored; the file is local-only)

**Verify:** `pixi run pytest tests/tools/test_cloud_perms_probe.py -v` → all pass; then `pixi run cloud:perms-probe --cloud aws` (after T2 applied) → exit 0 or 2 with non-empty snapshot.

**Steps:**

- [ ] **Step 3.1: Write the first failing test — exit code on auth failure.**

Create `tests/tools/test_cloud_perms_probe.py`:

```python
"""Unit tests for cloud_perms_probe — Layer W+α T3 (AWS), T4 (GCP)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tools import cloud_perms_probe as probe


class _FakeBoto3Session:
    """boto3.Session test double — clients(name) returns fakes from a map."""

    def __init__(self, clients: dict[str, Any]) -> None:
        self._clients = clients

    def client(self, name: str, **_kwargs: Any) -> Any:
        if name not in self._clients:
            raise KeyError(f"no fake registered for boto3 client {name!r}")
        return self._clients[name]


class _FakeSTSClient:
    def __init__(self, *, identity: dict[str, str] | None = None,
                 raise_on_call: BaseException | None = None) -> None:
        self._identity = identity
        self._raise = raise_on_call

    def get_caller_identity(self) -> dict[str, str]:
        if self._raise is not None:
            raise self._raise
        assert self._identity is not None
        return self._identity


def test_probe_aws_exit_1_on_auth_failure(tmp_path: Path) -> None:
    """sts.get_caller_identity raising → exit 1, no snapshot written."""
    from botocore.exceptions import ClientError

    err = ClientError(
        {"Error": {"Code": "InvalidClientTokenId", "Message": "bad key"}},
        "GetCallerIdentity",
    )
    session = _FakeBoto3Session({
        "sts": _FakeSTSClient(raise_on_call=err),
    })

    snapshot_path = tmp_path / "aws_snapshot.json"
    result = probe.probe_aws(session, snapshot_path=snapshot_path)

    assert result["exit_code"] == 1
    assert "auth_error" in result
    assert not snapshot_path.exists()
```

- [ ] **Step 3.2: Run test, confirm FAIL with "module not found".**

```bash
pixi run pytest tests/tools/test_cloud_perms_probe.py::test_probe_aws_exit_1_on_auth_failure -v
```

Expected: FAIL (ModuleNotFoundError on `tools.cloud_perms_probe`).

- [ ] **Step 3.3: Write the minimal `tools/cloud_perms_probe.py` to pass the failing test.**

Create `tools/cloud_perms_probe.py`:

```python
"""Cloud perms + GPU quota probe for SkyPilot multi-cloud bootstrap.

Layer W+α. Verifies AWS + GCP perms and persists snapshots to
``.aws/perms-snapshot.json`` and ``.gcp/perms-snapshot.json``.

Usage::

    pixi run cloud:perms-probe                # both clouds
    pixi run cloud:perms-probe --cloud aws    # AWS only
    pixi run cloud:perms-probe --cloud gcp    # GCP only

Exit codes:
    0 — all probes green
    1 — auth failure or required action denied
    2 — quota gap (AWS request submitted; GCP needs operator console)

Seams (mirror tools/preflight.py) — every SDK call goes through a
factory callable so unit tests inject fakes; no real cloud in tests.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_log = logging.getLogger(__name__)

# Required IAM actions kinoforge-ci must hold after T2 attaches the policy.
_REQUIRED_AWS_ACTIONS: tuple[str, ...] = (
    "ec2:RunInstances",
    "ec2:TerminateInstances",
    "ec2:DescribeInstances",
    "ec2:CreateTags",
    "ec2:CreateSecurityGroup",
    "ec2:CreateVpc",
    "iam:CreateRole",
    "iam:CreateInstanceProfile",
    "iam:PassRole",
    "servicequotas:GetServiceQuota",
    "servicequotas:RequestServiceQuotaIncrease",
    "s3:PutObject",
    "s3:GetObject",
    "kms:Encrypt",
    "kms:Decrypt",
)

# Running On-Demand G and VT instances (vCPU quota).
_AWS_QUOTA_CODE = "L-DB2E81BA"
_AWS_QUOTA_SERVICE = "ec2"
_AWS_TARGET_QUOTA_VCPUS = 4.0
_AWS_REGION = "us-east-1"

_AWS_SNAPSHOT_PATH = Path(_REPO_ROOT) / ".aws" / "perms-snapshot.json"
_GCP_SNAPSHOT_PATH = Path(_REPO_ROOT) / ".gcp" / "perms-snapshot.json"


def _now_local_iso() -> str:
    """Local-timezone ISO timestamp — matches user CLAUDE.md preference."""
    return datetime.now().astimezone().isoformat()


def _write_snapshot_atomic(path: Path, data: dict[str, Any]) -> None:
    """tmp-file + rename so a crashed probe never leaves a half-written snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def probe_aws(
    session: Any,
    *,
    snapshot_path: Path | None = None,
    region: str = _AWS_REGION,
    required_actions: tuple[str, ...] = _REQUIRED_AWS_ACTIONS,
    quota_code: str = _AWS_QUOTA_CODE,
    quota_service: str = _AWS_QUOTA_SERVICE,
    target_vcpus: float = _AWS_TARGET_QUOTA_VCPUS,
) -> dict[str, Any]:
    """Run AWS probes against ``session``; write snapshot; return exit-shaped dict.

    Returns:
        dict with keys: captured_at, cloud, identity, simulated,
        instance_type, quotas, exit_code, plus optional auth_error /
        denied / quota_gap details.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    snapshot_path = snapshot_path or _AWS_SNAPSHOT_PATH
    out: dict[str, Any] = {
        "captured_at": _now_local_iso(),
        "cloud": "aws",
        "region": region,
    }
    try:
        sts = session.client("sts", region_name=region)
        identity = sts.get_caller_identity()
        out["identity"] = identity
    except (ClientError, BotoCoreError) as exc:
        out["exit_code"] = 1
        out["auth_error"] = str(exc)
        return out

    # Remaining sections (simulate / instance-type / quota) land in Step 3.4.
    out["exit_code"] = 0
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entry — placeholder until Step 3.10 wires arguments."""
    raise SystemExit("main() not yet implemented")
```

Also create `tests/tools/__init__.py` if it's not already present (it is — confirmed by the file listing).

- [ ] **Step 3.4: Run the test, confirm PASS.**

```bash
pixi run pytest tests/tools/test_cloud_perms_probe.py::test_probe_aws_exit_1_on_auth_failure -v
```

Expected: PASS.

- [ ] **Step 3.5: Add the green-path test — simulated actions allowed + quota captured + exit 0.**

Append to `tests/tools/test_cloud_perms_probe.py`:

```python
class _FakeIAMClient:
    def __init__(self, results: dict[str, str]) -> None:
        self._results = results

    def simulate_principal_policy(
        self, *, PolicySourceArn: str, ActionNames: list[str], **_kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "EvaluationResults": [
                {"EvalActionName": a, "EvalDecision": self._results.get(a, "implicitDeny")}
                for a in ActionNames
            ],
        }


class _FakeEC2Client:
    def __init__(self, types: list[dict[str, Any]] | None = None) -> None:
        self._types = types or [{"InstanceType": "g4dn.xlarge", "GpuInfo": {"Gpus": [{"Name": "T4"}]}}]

    def describe_instance_types(self, *, InstanceTypes: list[str], **_k: Any) -> dict[str, Any]:
        wanted = set(InstanceTypes)
        return {"InstanceTypes": [t for t in self._types if t["InstanceType"] in wanted]}


class _FakeServiceQuotasClient:
    def __init__(self, *, value: float, name: str = "Running On-Demand G and VT instances") -> None:
        self._value = value
        self._name = name

    def get_service_quota(self, *, ServiceCode: str, QuotaCode: str) -> dict[str, Any]:
        return {"Quota": {"QuotaCode": QuotaCode, "QuotaName": self._name, "Value": self._value}}


def _green_aws_session(*, quota_value: float = 8.0) -> _FakeBoto3Session:
    return _FakeBoto3Session({
        "sts": _FakeSTSClient(identity={
            "UserId": "AIDAEXAMPLE",
            "Account": "<AWS_ACCOUNT>",
            "Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci",
        }),
        "iam": _FakeIAMClient({a: "allowed" for a in probe._REQUIRED_AWS_ACTIONS}),
        "ec2": _FakeEC2Client(),
        "service-quotas": _FakeServiceQuotasClient(value=quota_value),
    })


def test_probe_aws_exit_0_on_all_green(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "aws_snapshot.json"
    result = probe.probe_aws(_green_aws_session(), snapshot_path=snapshot_path)

    assert result["exit_code"] == 0
    assert result["identity"]["Arn"].endswith(":user/kinoforge-ci")
    assert result["simulated"][probe._REQUIRED_AWS_ACTIONS[0]] == "allowed"
    assert "g4dn.xlarge" in result["instance_type"]
    assert result["quotas"]["L-DB2E81BA"]["value"] == 8.0
    on_disk = json.loads(snapshot_path.read_text())
    assert on_disk == result


def test_probe_aws_exit_2_on_quota_gap(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "aws_snapshot.json"
    result = probe.probe_aws(
        _green_aws_session(quota_value=0.0), snapshot_path=snapshot_path,
    )
    assert result["exit_code"] == 2
    assert result["quota_gap"] == {"code": "L-DB2E81BA", "have": 0.0, "want": 4.0}


def test_probe_aws_exit_1_on_action_denied(tmp_path: Path) -> None:
    session = _FakeBoto3Session({
        "sts": _FakeSTSClient(identity={"Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci"}),
        "iam": _FakeIAMClient({a: "implicitDeny" for a in probe._REQUIRED_AWS_ACTIONS}),
        "ec2": _FakeEC2Client(),
        "service-quotas": _FakeServiceQuotasClient(value=8.0),
    })
    snapshot_path = tmp_path / "aws_snapshot.json"
    result = probe.probe_aws(session, snapshot_path=snapshot_path)

    assert result["exit_code"] == 1
    assert "denied" in result
    assert result["denied"]
```

- [ ] **Step 3.6: Run new tests, confirm 2 FAIL (green + quota-gap; denied passes by accident if AC list is empty).**

```bash
pixi run pytest tests/tools/test_cloud_perms_probe.py -v
```

Expected: at least two FAIL (the green and quota-gap paths exit 0 but skip simulated / quota work).

- [ ] **Step 3.7: Expand `probe_aws` to cover simulated / instance-type / quota + snapshot persistence.**

Replace the `probe_aws` body (after the auth-error return) with the full implementation:

```python
    # Simulate every required action against the caller's principal.
    try:
        iam = session.client("iam", region_name=region)
        sim = iam.simulate_principal_policy(
            PolicySourceArn=identity["Arn"],
            ActionNames=list(required_actions),
        )
        simulated = {
            entry["EvalActionName"]: entry["EvalDecision"]
            for entry in sim["EvaluationResults"]
        }
        out["simulated"] = simulated
        denied = [a for a, d in simulated.items() if d != "allowed"]
        if denied:
            out["exit_code"] = 1
            out["denied"] = denied
            _write_snapshot_atomic(snapshot_path, out)
            return out
    except (ClientError, BotoCoreError) as exc:
        out["exit_code"] = 1
        out["auth_error"] = f"simulate_principal_policy: {exc}"
        return out

    # Sanity: target instance type exists in the region.
    try:
        ec2 = session.client("ec2", region_name=region)
        types_resp = ec2.describe_instance_types(InstanceTypes=["g4dn.xlarge"])
        out["instance_type"] = {
            t["InstanceType"]: t for t in types_resp["InstanceTypes"]
        }
    except (ClientError, BotoCoreError) as exc:
        out["exit_code"] = 1
        out["instance_type_error"] = str(exc)
        _write_snapshot_atomic(snapshot_path, out)
        return out

    # Quota readout — T5 acts on the gap, T3 only captures.
    try:
        sq = session.client("service-quotas", region_name=region)
        q = sq.get_service_quota(ServiceCode=quota_service, QuotaCode=quota_code)
        quota_entry = {
            "code": q["Quota"]["QuotaCode"],
            "name": q["Quota"]["QuotaName"],
            "value": q["Quota"]["Value"],
        }
        out["quotas"] = {quota_code: quota_entry}
        if quota_entry["value"] < target_vcpus:
            out["exit_code"] = 2
            out["quota_gap"] = {
                "code": quota_code,
                "have": quota_entry["value"],
                "want": target_vcpus,
            }
            _write_snapshot_atomic(snapshot_path, out)
            return out
    except (ClientError, BotoCoreError) as exc:
        out["exit_code"] = 1
        out["quota_error"] = str(exc)
        _write_snapshot_atomic(snapshot_path, out)
        return out

    out["exit_code"] = 0
    _write_snapshot_atomic(snapshot_path, out)
    return out
```

- [ ] **Step 3.8: Re-run all probe tests, confirm 4 PASS.**

```bash
pixi run pytest tests/tools/test_cloud_perms_probe.py -v
```

Expected: 4 PASS.

- [ ] **Step 3.9: Wire `main()` to parse `--cloud aws` and dispatch via real `boto3.Session`.**

Replace the placeholder `main()` with:

```python
def _real_aws_session_factory() -> Any:
    import boto3  # local import — kept out of test path
    return boto3.Session()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cloud perms probe (Layer W+α).")
    parser.add_argument(
        "--cloud",
        choices=("aws", "gcp", "both"),
        default="both",
        help="Which cloud to probe (default: both).",
    )
    args = parser.parse_args(argv)

    exit_codes: list[int] = []

    if args.cloud in ("aws", "both"):
        session = _real_aws_session_factory()
        result = probe_aws(session)
        print(f"[aws] exit={result['exit_code']}")
        if "auth_error" in result:
            print(f"[aws] auth_error: {result['auth_error']}")
        if "denied" in result:
            print(f"[aws] denied: {result['denied']}")
        if "quota_gap" in result:
            print(f"[aws] quota gap: {result['quota_gap']}")
        exit_codes.append(result["exit_code"])

    if args.cloud in ("gcp", "both"):
        # Wired in Task 4.
        print("[gcp] not yet implemented (Task 4)")

    return max(exit_codes) if exit_codes else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3.10: Add `cloud:perms-probe` pixi task.**

Edit `pixi.toml` `[tasks]` section, add:

```toml
"cloud:perms-probe" = "python tools/cloud_perms_probe.py"
```

(Use the exact key syntax the file already uses — quoted because of the colon.)

- [ ] **Step 3.11: Live probe + capture green fixture.**

Once the operator confirms T2 is done:

```bash
pixi run cloud:perms-probe --cloud aws
```

Expected: exit 0 (or 2 if quota=0; that's expected and handled by T5).

Read the snapshot:

```bash
python -c "import json; print(json.dumps(json.load(open('.aws/perms-snapshot.json')), indent=2))"
```

Copy a redacted version (drop `UserId`; keep `Arn` as `arn:aws:iam::REDACTED:user/kinoforge-ci`) into `tests/tools/fixtures/cloud_perms/aws_green.json` to serve as a lockdown fixture.

- [ ] **Step 3.12: Add a fixture-replay regression test.**

Append to `tests/tools/test_cloud_perms_probe.py`:

```python
def test_aws_green_fixture_matches_probe_shape() -> None:
    """Lockdown: live-captured fixture must match the dict shape probe_aws emits.

    If a future refactor changes the snapshot schema, the fixture diff fails fast.
    """
    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "cloud_perms" / "aws_green.json"
    )
    fixture = json.loads(fixture_path.read_text())
    required_top_keys = {
        "captured_at", "cloud", "region", "identity", "simulated",
        "instance_type", "quotas", "exit_code",
    }
    assert required_top_keys.issubset(fixture.keys()), \
        f"missing keys: {required_top_keys - fixture.keys()}"
    assert fixture["cloud"] == "aws"
    assert fixture["identity"]["Arn"].endswith(":user/kinoforge-ci")
    assert "L-DB2E81BA" in fixture["quotas"]
```

- [ ] **Step 3.13: Commit T3.**

```bash
git add tools/cloud_perms_probe.py tests/tools/test_cloud_perms_probe.py tests/tools/fixtures/cloud_perms/aws_green.json pixi.toml
git commit -m "$(cat <<'EOF'
feat(probe): AWS perms + quota probe with fixture lockdown

Layer W+α T3. `tools/cloud_perms_probe.py --cloud aws` runs
sts.get_caller_identity + iam.simulate_principal_policy +
ec2.describe_instance_types + service-quotas.get_service_quota,
writes .aws/perms-snapshot.json. Exit 0 green / 1 auth or denial /
2 quota gap. Lockdown fixture captured from live probe.
EOF
)"
```

---

## Task 4 — GCP probe

**Goal:** `tools/cloud_perms_probe.py --cloud gcp` runs equivalent probes against `google-cloud-compute` + `google-cloud-service-usage` + `google-cloud-resource-manager`, writes `.gcp/perms-snapshot.json`.

**Files:**
- Modify: `tools/cloud_perms_probe.py`
- Modify: `tests/tools/test_cloud_perms_probe.py`
- Create: `tests/tools/fixtures/cloud_perms/gcp_green.json` (after live capture)
- Modify: `pixi.toml` (verify `google-cloud-compute`, `google-cloud-service-usage`, `google-cloud-resource-manager` are pinned)

**Acceptance Criteria:**
- [ ] `probe_gcp(compute_client, regions_client, quotas_client, iam_client)` returns the same exit-shaped dict as `probe_aws`
- [ ] Region metadata for `us-central1` captured
- [ ] `NVIDIA_T4_GPUS` quota captured from the region
- [ ] `kinoforge-runner` SA roles audited; presence of `roles/compute.instanceAdmin.v1` + `roles/iam.serviceAccountUser` recorded
- [ ] Exit 0 if all green and quota ≥ 1; 1 on auth fail / role missing; 2 on quota < 1
- [ ] Tests inject fake google.cloud clients — no real GCP calls in CI

**Verify:** `pixi run pytest tests/tools/test_cloud_perms_probe.py -v` → all pass; then `pixi run cloud:perms-probe --cloud gcp` → exit 0 or 2.

**Steps:**

- [ ] **Step 4.1: Verify `pixi.toml` carries the GCP SDK deps.**

```bash
grep -E "google-cloud-(compute|service-usage|resource-manager)" /workspace/pixi.toml
```

If missing, add them under the appropriate `[dependencies]` (conda-forge first; fall back to `[pypi-dependencies]` only if not on conda-forge):

```bash
pixi add google-cloud-compute google-cloud-service-usage google-cloud-resource-manager
```

- [ ] **Step 4.2: Write the first failing GCP test — exit 0 on green.**

Append to `tests/tools/test_cloud_perms_probe.py`:

```python
class _FakeGCPRegionsClient:
    def __init__(self, *, quotas: list[dict[str, Any]]) -> None:
        self._quotas = quotas

    def get(self, *, project: str, region: str) -> Any:
        class _Region:
            def __init__(self, quotas: list[dict[str, Any]]) -> None:
                self.name = f"projects/{project}/regions/{region}"
                self.quotas = [type("Q", (), q)() for q in quotas]

        return _Region(self._quotas)


class _FakeGCPIAMClient:
    def __init__(self, *, sa_roles: dict[str, list[str]]) -> None:
        self._roles = sa_roles

    def get_iam_policy(self, *, resource: str, **_k: Any) -> Any:
        class _Binding:
            def __init__(self, role: str, members: list[str]) -> None:
                self.role = role
                self.members = members

        bindings = []
        for role, members in self._roles.items():
            bindings.append(_Binding(role, members))
        class _Policy:
            def __init__(self, b: list[_Binding]) -> None:
                self.bindings = b
        return _Policy(bindings)


def _green_gcp_clients(*, t4_quota: float = 8.0) -> dict[str, Any]:
    return {
        "regions": _FakeGCPRegionsClient(quotas=[
            {"metric": "NVIDIA_T4_GPUS", "limit": t4_quota, "usage": 0.0},
            {"metric": "CPUS", "limit": 24.0, "usage": 0.0},
        ]),
        "iam": _FakeGCPIAMClient(sa_roles={
            "roles/compute.instanceAdmin.v1": ["serviceAccount:kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com"],
            "roles/iam.serviceAccountUser": ["serviceAccount:kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com"],
            "roles/storage.admin": ["serviceAccount:kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com"],
        }),
    }


def test_probe_gcp_exit_0_on_all_green(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "gcp_snapshot.json"
    result = probe.probe_gcp(
        clients=_green_gcp_clients(),
        project="<GCP_PROJECT>",
        sa_email="kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com",
        snapshot_path=snapshot_path,
    )
    assert result["exit_code"] == 0
    assert result["quotas"]["NVIDIA_T4_GPUS"]["limit"] == 8.0
    assert "roles/compute.instanceAdmin.v1" in result["sa_roles"]
    assert json.loads(snapshot_path.read_text()) == result


def test_probe_gcp_exit_2_on_quota_zero(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "gcp_snapshot.json"
    result = probe.probe_gcp(
        clients=_green_gcp_clients(t4_quota=0.0),
        project="<GCP_PROJECT>",
        sa_email="kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com",
        snapshot_path=snapshot_path,
    )
    assert result["exit_code"] == 2
    assert result["quota_gap"] == {
        "metric": "NVIDIA_T4_GPUS", "have": 0.0, "want": 1.0, "region": "us-central1",
    }


def test_probe_gcp_exit_1_on_missing_role(tmp_path: Path) -> None:
    clients = _green_gcp_clients()
    clients["iam"] = _FakeGCPIAMClient(sa_roles={
        # No instanceAdmin → required role missing.
        "roles/storage.admin": ["serviceAccount:kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com"],
    })
    snapshot_path = tmp_path / "gcp_snapshot.json"
    result = probe.probe_gcp(
        clients=clients,
        project="<GCP_PROJECT>",
        sa_email="kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com",
        snapshot_path=snapshot_path,
    )
    assert result["exit_code"] == 1
    assert "roles/compute.instanceAdmin.v1" in result["missing_roles"]
```

- [ ] **Step 4.3: Run tests, confirm 3 FAIL (probe_gcp not defined).**

```bash
pixi run pytest tests/tools/test_cloud_perms_probe.py::test_probe_gcp_exit_0_on_all_green -v
```

Expected: FAIL (AttributeError on `probe.probe_gcp`).

- [ ] **Step 4.4: Implement `probe_gcp`.**

Add to `tools/cloud_perms_probe.py`:

```python
_GCP_REGION = "us-central1"
_GCP_TARGET_T4_QUOTA = 1.0
_GCP_REQUIRED_ROLES: tuple[str, ...] = (
    "roles/compute.instanceAdmin.v1",
    "roles/iam.serviceAccountUser",
)


def probe_gcp(
    *,
    clients: dict[str, Any],
    project: str,
    sa_email: str,
    snapshot_path: Path | None = None,
    region: str = _GCP_REGION,
    target_t4_quota: float = _GCP_TARGET_T4_QUOTA,
    required_roles: tuple[str, ...] = _GCP_REQUIRED_ROLES,
) -> dict[str, Any]:
    """Run GCP probes; write snapshot; return exit-shaped dict.

    ``clients`` is a dict keyed by service name (``regions``, ``iam``) so
    callers can inject fakes. The real factory wires the google.cloud
    SDK clients.
    """
    snapshot_path = snapshot_path or _GCP_SNAPSHOT_PATH
    out: dict[str, Any] = {
        "captured_at": _now_local_iso(),
        "cloud": "gcp",
        "project": project,
        "region": region,
        "sa_email": sa_email,
    }

    # Region + T4 quota lookup.
    try:
        regions_client = clients["regions"]
        region_obj = regions_client.get(project=project, region=region)
        quotas: dict[str, dict[str, Any]] = {}
        for q in region_obj.quotas:
            quotas[q.metric] = {
                "metric": q.metric,
                "limit": float(q.limit),
                "usage": float(q.usage),
            }
        out["quotas"] = quotas
    except Exception as exc:  # noqa: BLE001 — top-level boundary
        out["exit_code"] = 1
        out["region_error"] = str(exc)
        _write_snapshot_atomic(snapshot_path, out)
        return out

    # SA role audit.
    try:
        iam_client = clients["iam"]
        policy = iam_client.get_iam_policy(resource=f"projects/{project}")
        sa_member = f"serviceAccount:{sa_email}"
        sa_roles = []
        for binding in policy.bindings:
            if sa_member in binding.members:
                sa_roles.append(binding.role)
        out["sa_roles"] = sa_roles
        missing = [r for r in required_roles if r not in sa_roles]
        if missing:
            out["exit_code"] = 1
            out["missing_roles"] = missing
            _write_snapshot_atomic(snapshot_path, out)
            return out
    except Exception as exc:  # noqa: BLE001
        out["exit_code"] = 1
        out["iam_error"] = str(exc)
        _write_snapshot_atomic(snapshot_path, out)
        return out

    # T4 quota gap?
    t4 = out["quotas"].get("NVIDIA_T4_GPUS")
    if t4 is None or t4["limit"] < target_t4_quota:
        out["exit_code"] = 2
        out["quota_gap"] = {
            "metric": "NVIDIA_T4_GPUS",
            "have": t4["limit"] if t4 else 0.0,
            "want": target_t4_quota,
            "region": region,
        }
        _write_snapshot_atomic(snapshot_path, out)
        return out

    out["exit_code"] = 0
    _write_snapshot_atomic(snapshot_path, out)
    return out
```

- [ ] **Step 4.5: Run tests, confirm 3 PASS.**

```bash
pixi run pytest tests/tools/test_cloud_perms_probe.py -v
```

Expected: all GCP tests PASS; AWS tests still PASS.

- [ ] **Step 4.6: Wire real-GCP factory + `main()` GCP branch.**

Add to `tools/cloud_perms_probe.py`:

```python
def _real_gcp_clients() -> dict[str, Any]:
    from google.cloud import compute_v1
    from google.cloud import resourcemanager_v3

    return {
        "regions": compute_v1.RegionsClient(),
        "iam": resourcemanager_v3.ProjectsClient(),
    }


def _resolve_gcp_project_and_sa() -> tuple[str, str]:
    """Read project + SA email from .gcp/kinoforge-sa.json (default chain)."""
    sa_path = Path(_REPO_ROOT) / ".gcp" / "kinoforge-sa.json"
    sa_doc = json.loads(sa_path.read_text())
    return sa_doc["project_id"], sa_doc["client_email"]
```

Update `main()` GCP branch:

```python
    if args.cloud in ("gcp", "both"):
        try:
            project, sa_email = _resolve_gcp_project_and_sa()
            clients = _real_gcp_clients()
            result = probe_gcp(clients=clients, project=project, sa_email=sa_email)
        except Exception as exc:  # noqa: BLE001 — boundary
            print(f"[gcp] bootstrap error: {exc}")
            exit_codes.append(1)
        else:
            print(f"[gcp] exit={result['exit_code']}")
            if "missing_roles" in result:
                print(f"[gcp] missing roles: {result['missing_roles']}")
            if "quota_gap" in result:
                print(f"[gcp] quota gap: {result['quota_gap']}")
            exit_codes.append(result["exit_code"])
```

Note: the `resourcemanager_v3.ProjectsClient.get_iam_policy` real API takes a `resource` positional argument formatted slightly differently than the fake — verify with a smoke probe in Step 4.7 and patch the fake or the real call to match.

- [ ] **Step 4.7: Live probe + capture green fixture.**

```bash
pixi run cloud:perms-probe --cloud gcp
```

If the live shape differs from the fake (e.g. real client wants `request=GetIamPolicyRequest(resource=...)` instead of a kwarg), tighten the fake to match the real shape and re-run unit tests until both green.

Capture redacted snapshot to `tests/tools/fixtures/cloud_perms/gcp_green.json`.

- [ ] **Step 4.8: Add the GCP fixture lockdown test.**

Append to `tests/tools/test_cloud_perms_probe.py`:

```python
def test_gcp_green_fixture_matches_probe_shape() -> None:
    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "cloud_perms" / "gcp_green.json"
    )
    fixture = json.loads(fixture_path.read_text())
    required_top_keys = {
        "captured_at", "cloud", "project", "region", "sa_email",
        "quotas", "sa_roles", "exit_code",
    }
    assert required_top_keys.issubset(fixture.keys()), \
        f"missing keys: {required_top_keys - fixture.keys()}"
    assert fixture["cloud"] == "gcp"
    assert "NVIDIA_T4_GPUS" in fixture["quotas"]
```

- [ ] **Step 4.9: Commit T4.**

```bash
git add tools/cloud_perms_probe.py tests/tools/test_cloud_perms_probe.py tests/tools/fixtures/cloud_perms/gcp_green.json pixi.toml pixi.lock
git commit -m "$(cat <<'EOF'
feat(probe): GCP perms + T4 quota probe

Layer W+α T4. probe_gcp() audits kinoforge-runner SA roles,
captures NVIDIA_T4_GPUS quota in us-central1. Exit 0 green / 1
missing role or auth / 2 quota gap. Lockdown fixture captured.
EOF
)"
```

---

## Task 5 — Quota gap handler

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. The GCP half ends with operator action — the probe MUST NOT be declared green until the operator confirms the GCP quota request was filed AND a follow-up GCP probe shows quota landed.

**Goal:** When the AWS quota is below target, auto-submit a `RequestServiceQuotaIncrease` via SDK. When GCP T4 quota is below target, print a console URL + instructions and exit 2.

**Files:**
- Modify: `tools/cloud_perms_probe.py`
- Modify: `tests/tools/test_cloud_perms_probe.py`

**Acceptance Criteria:**
- [ ] `request_aws_quota_increase(session, code, target)` calls `service-quotas.request_service_quota_increase`; returns CaseId
- [ ] Re-running against an already-open case returns the same CaseId (no duplicate)
- [ ] `probe_aws` writes CaseId into snapshot at `quota_request.case_id`
- [ ] `probe_gcp` emits a console URL on quota gap; URL contains project ID + filter `NVIDIA_T4_GPUS`
- [ ] Test: AWS quota gap → request submitted exactly once even with repeated probe calls
- [ ] Test: AWS quota gap → exit 2 + snapshot carries `quota_request.case_id`
- [ ] Test: GCP quota gap → exit 2 + snapshot carries `quota_request.console_url`

**Verify:** `pixi run pytest tests/tools/test_cloud_perms_probe.py -v` → all pass.

**Steps:**

- [ ] **Step 5.1: Add the AWS auto-request test (RED).**

Append to `tests/tools/test_cloud_perms_probe.py`:

```python
class _FakeServiceQuotasWithRequest(_FakeServiceQuotasClient):
    def __init__(self, *, value: float, existing_case: str | None = None) -> None:
        super().__init__(value=value)
        self._existing_case = existing_case
        self.requests_made: list[dict[str, Any]] = []

    def list_requested_service_quota_change_history(
        self, *, ServiceCode: str, **_k: Any,
    ) -> dict[str, Any]:
        items = []
        if self._existing_case is not None:
            items.append({
                "Id": self._existing_case,
                "QuotaCode": "L-DB2E81BA",
                "Status": "PENDING",
            })
        return {"RequestedQuotas": items}

    def request_service_quota_increase(
        self, *, ServiceCode: str, QuotaCode: str, DesiredValue: float,
    ) -> dict[str, Any]:
        case_id = self._existing_case or f"case-{len(self.requests_made):04d}"
        self.requests_made.append({
            "ServiceCode": ServiceCode,
            "QuotaCode": QuotaCode,
            "DesiredValue": DesiredValue,
        })
        return {"RequestedQuota": {"Id": case_id, "Status": "PENDING"}}


def test_probe_aws_submits_quota_request_on_gap(tmp_path: Path) -> None:
    fake_sq = _FakeServiceQuotasWithRequest(value=0.0)
    session = _FakeBoto3Session({
        "sts": _FakeSTSClient(identity={"Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci"}),
        "iam": _FakeIAMClient({a: "allowed" for a in probe._REQUIRED_AWS_ACTIONS}),
        "ec2": _FakeEC2Client(),
        "service-quotas": fake_sq,
    })
    snapshot_path = tmp_path / "aws.json"
    result = probe.probe_aws(session, snapshot_path=snapshot_path)

    assert result["exit_code"] == 2
    assert result["quota_request"]["case_id"].startswith("case-")
    assert len(fake_sq.requests_made) == 1
    assert fake_sq.requests_made[0]["DesiredValue"] == 4.0


def test_probe_aws_idempotent_quota_request(tmp_path: Path) -> None:
    """Re-run with an existing open case → no duplicate submission."""
    fake_sq = _FakeServiceQuotasWithRequest(value=0.0, existing_case="case-EXISTING")
    session = _FakeBoto3Session({
        "sts": _FakeSTSClient(identity={"Arn": "arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci"}),
        "iam": _FakeIAMClient({a: "allowed" for a in probe._REQUIRED_AWS_ACTIONS}),
        "ec2": _FakeEC2Client(),
        "service-quotas": fake_sq,
    })
    snapshot_path = tmp_path / "aws.json"
    result = probe.probe_aws(session, snapshot_path=snapshot_path)

    assert result["exit_code"] == 2
    assert result["quota_request"]["case_id"] == "case-EXISTING"
    assert len(fake_sq.requests_made) == 0, "must not duplicate existing open case"


def test_probe_gcp_quota_gap_emits_console_url(tmp_path: Path) -> None:
    result = probe.probe_gcp(
        clients=_green_gcp_clients(t4_quota=0.0),
        project="<GCP_PROJECT>",
        sa_email="kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com",
        snapshot_path=tmp_path / "gcp.json",
    )
    assert result["exit_code"] == 2
    url = result["quota_request"]["console_url"]
    assert "<GCP_PROJECT>" in url
    assert "NVIDIA_T4_GPUS" in url
```

- [ ] **Step 5.2: Run tests, confirm 3 FAIL.**

```bash
pixi run pytest tests/tools/test_cloud_perms_probe.py -v
```

Expected: 3 new tests FAIL.

- [ ] **Step 5.3: Implement AWS quota auto-request — idempotency via history lookup.**

Add to `tools/cloud_perms_probe.py`:

```python
def _aws_existing_quota_case(sq_client: Any, service: str, code: str) -> str | None:
    """Return the case id of an open quota-increase request for ``code``, if any."""
    try:
        resp = sq_client.list_requested_service_quota_change_history(
            ServiceCode=service,
        )
    except Exception:  # noqa: BLE001
        return None
    for entry in resp.get("RequestedQuotas", []):
        if entry.get("QuotaCode") == code and entry.get("Status") in (
            "PENDING", "CASE_OPENED", "APPROVED",
        ):
            return str(entry["Id"])
    return None


def _aws_submit_quota_request(
    sq_client: Any, service: str, code: str, target: float,
) -> str:
    """Submit a quota increase; return case id. Idempotent — returns existing case."""
    existing = _aws_existing_quota_case(sq_client, service, code)
    if existing is not None:
        return existing
    resp = sq_client.request_service_quota_increase(
        ServiceCode=service, QuotaCode=code, DesiredValue=target,
    )
    return str(resp["RequestedQuota"]["Id"])
```

Then, in the quota-gap branch of `probe_aws`, replace the existing `quota_gap` block with:

```python
        if quota_entry["value"] < target_vcpus:
            case_id = _aws_submit_quota_request(
                sq, quota_service, quota_code, target_vcpus,
            )
            out["exit_code"] = 2
            out["quota_gap"] = {
                "code": quota_code,
                "have": quota_entry["value"],
                "want": target_vcpus,
            }
            out["quota_request"] = {
                "case_id": case_id,
                "submitted_at": _now_local_iso(),
            }
            _write_snapshot_atomic(snapshot_path, out)
            return out
```

- [ ] **Step 5.4: Implement the GCP console URL handler.**

Replace the GCP quota-gap block in `probe_gcp` with:

```python
    if t4 is None or t4["limit"] < target_t4_quota:
        console_url = (
            f"https://console.cloud.google.com/iam-admin/quotas"
            f"?project={project}&filter=Quota%3A%20NVIDIA_T4_GPUS"
        )
        out["exit_code"] = 2
        out["quota_gap"] = {
            "metric": "NVIDIA_T4_GPUS",
            "have": t4["limit"] if t4 else 0.0,
            "want": target_t4_quota,
            "region": region,
        }
        out["quota_request"] = {
            "console_url": console_url,
            "instructions": (
                "GCP has no SDK surface for compute-quota increases. "
                "Open the URL above, click 'EDIT QUOTAS' for NVIDIA T4 GPUs "
                f"in {region}, request limit ≥ {target_t4_quota:.0f}, submit. "
                "Re-run `pixi run cloud:perms-probe --cloud gcp` after approval lands."
            ),
        }
        _write_snapshot_atomic(snapshot_path, out)
        return out
```

- [ ] **Step 5.5: Run all tests, confirm green.**

```bash
pixi run pytest tests/tools/test_cloud_perms_probe.py -v
```

Expected: all PASS.

- [ ] **Step 5.6: Live run — submit quota requests if real quotas are 0.**

```bash
pixi run cloud:perms-probe
```

Expected outcomes:

- If AWS quota < 4 vCPUs: probe submits a case automatically; snapshot now carries `quota_request.case_id`. Exit 2.
- If GCP quota < 1: probe prints the console URL; exit 2. Operator opens the URL, files the request, confirms in chat.

Operator gate point: wait for the operator to ack the GCP request filing before considering T5 closed.

- [ ] **Step 5.7: Commit T5.**

```bash
git add tools/cloud_perms_probe.py tests/tools/test_cloud_perms_probe.py
git commit -m "$(cat <<'EOF'
feat(probe): quota gap handler — AWS auto-request + GCP console URL

Layer W+α T5. AWS RequestServiceQuotaIncrease is naturally
idempotent (history-lookup before submit). GCP has no SDK surface —
probe emits console URL + operator instructions and exits 2.
EOF
)"
```

```json:metadata
{"userGate": true, "tags": ["user-gate"], "files": ["tools/cloud_perms_probe.py", "tests/tools/test_cloud_perms_probe.py"], "verifyCommand": "pixi run pytest tests/tools/test_cloud_perms_probe.py -v && pixi run cloud:perms-probe", "acceptanceCriteria": ["AWS RequestServiceQuotaIncrease submitted once on gap", "GCP console URL emitted", "operator confirms GCP quota request filed"], "requiresUserSpecification": false}
```

---

## Task 6 — `sky check` smoke

**Goal:** Run `sky check aws gcp` from the `live-skypilot` pixi env, capture stdout/stderr, append to `docs/CLOUD-CREDS.md`. Zero spend.

**Files:**
- Modify: `docs/CLOUD-CREDS.md`

**Acceptance Criteria:**
- [ ] `sky check aws gcp` returns clean (both clouds reported as `Enabled` / `OK`)
- [ ] Full output (or a representative trimmed excerpt) appended to a new `## SkyPilot check — captured output` appendix
- [ ] No EC2 / GCE instances launched (verify with `aws ec2 describe-instances` returning empty + `gcloud compute instances list` empty)

**Verify:** `pixi run -e live-skypilot sky check aws gcp` → clean output; `aws ec2 describe-instances --region us-east-1` returns 0 reservations.

**Steps:**

- [ ] **Step 6.1: Activate the `live-skypilot` env and run `sky check`.**

```bash
pixi run -e live-skypilot sky check aws gcp 2>&1 | tee /tmp/sky_check_output.txt
```

If sky check reports a missing credential (e.g. `~/.config/gcloud/application_default_credentials.json` not found despite `GOOGLE_APPLICATION_CREDENTIALS` env), investigate — likely a SkyPilot quirk that wants ADC over SA-key explicitly. Common fix:

```bash
gcloud auth application-default login --no-launch-browser   # one-time
```

…but with our SA-key auth chain this should not be needed. If sky check still fails, file the gap in PROGRESS Phase 39 follow-ups and adjust the plan.

- [ ] **Step 6.2: Verify zero instances exist (sanity).**

```bash
pixi run -e live-skypilot aws ec2 describe-instances --region us-east-1 --query 'Reservations[].Instances[].InstanceId' --output text
pixi run -e live-skypilot gcloud compute instances list --format='value(name)'
```

Expected: both empty.

- [ ] **Step 6.3: Append the captured output to `docs/CLOUD-CREDS.md`.**

Add a new appendix at the bottom:

```markdown
## SkyPilot check — captured output (Layer W+α T6)

Captured `pixi run -e live-skypilot sky check aws gcp` at <ISO timestamp>:

```text
<paste contents of /tmp/sky_check_output.txt — trim ANSI codes>
```

Both AWS and GCP report enabled. Zero instances launched during this check.
```

- [ ] **Step 6.4: Commit T6.**

```bash
git add docs/CLOUD-CREDS.md
git commit -m "$(cat <<'EOF'
docs(cloud-creds): sky check appendix — AWS + GCP enabled

Layer W+α T6. `sky check aws gcp` reports both clouds enabled.
Zero EC2/GCE instances launched during the metadata check.
EOF
)"
```

---

## Task 7 — Docs + Phase 39 + commit

**Goal:** Finalize `docs/CLOUD-CREDS.md` (table refresh + SkyPilot perms section), add `PROGRESS.md` Phase 39 entry with per-task SHAs, add a one-paragraph `README.md` pointer, ensure pre-commit clean, prepare for merge.

**Files:**
- Modify: `docs/CLOUD-CREDS.md`
- Modify: `PROGRESS.md`
- Modify: `README.md`

**Acceptance Criteria:**
- [ ] `docs/CLOUD-CREDS.md` bootstrap-status table has new rows: `SkyPilot perms (AWS)` and `SkyPilot perms (GCP)` both ✅
- [ ] New `## SkyPilot permissions (Layer W+α)` section summarizes scope + snapshot file locations
- [ ] `PROGRESS.md` Phase 39 entry committed: per-task SHAs, design decisions, snapshot excerpts, done-criteria checklist
- [ ] `README.md` carries a one-paragraph pointer in the appropriate section (likely near the Phase 31 SkyPilot note)
- [ ] `pre-commit run --all-files` exits 0
- [ ] All tests still pass: `pixi run test` reports same count + new probe tests green
- [ ] `pixi run cloud:perms-probe` exits 0 against both clouds
- [ ] Single-next-action block in `PROGRESS.md` updated to point at Layer W+β (or whatever comes next)

**Verify:** `pixi run pre-commit run --all-files` + `pixi run test` + `pixi run cloud:perms-probe`.

**Steps:**

- [ ] **Step 7.1: Refresh `docs/CLOUD-CREDS.md` bootstrap-status table.**

Add rows after the existing AWS/GCP rows:

```markdown
| SkyPilot perms (AWS)  | ✅ Bootstrapped               | `.aws/policies/skypilot-minimal.json`, `.aws/perms-snapshot.json`   | `kinoforge-ci` + `KinoforgeSkypilotMinimal`                          |
| SkyPilot perms (GCP)  | ✅ Bootstrapped               | `.gcp/perms-snapshot.json`                                          | `kinoforge-runner` + `compute.instanceAdmin.v1`                      |
```

- [ ] **Step 7.2: Add `## SkyPilot permissions (Layer W+α)` section.**

After the existing AWS provisioning history section, add:

```markdown
## SkyPilot permissions (Layer W+α)

Front-load layer that landed every AWS + GCP permission and GPU quota
the SkyPilot multi-cloud T4 smoke (Layer W+β) needs. Spec:
`docs/superpowers/specs/2026-06-06-layer-w-alpha-cloud-bootstrap-design.md`.

- AWS scoped policy: `.aws/policies/skypilot-minimal.json` (tracked).
  EC2 lifecycle + IAM PassRole for `skypilot-*` + ServiceQuotas + S3
  scoped to `kinoforge-realcloud-tests-*` and `skypilot-*` prefixes +
  KMS scoped to `alias/kinoforge-realcloud-tests`.
- AWS quota: `L-DB2E81BA` (Running On-Demand G/VT instances) ≥ 4 vCPUs
  in `us-east-1`. Auto-requested via SDK if low; CaseId captured in
  `.aws/perms-snapshot.json`.
- GCP roles: `kinoforge-runner` SA holds
  `roles/compute.instanceAdmin.v1` + `roles/iam.serviceAccountUser`.
- GCP quota: `NVIDIA_T4_GPUS` ≥ 1 in `us-central1`. Operator-driven
  (no SDK surface). Console URL emitted by probe on gap.

Re-run with `pixi run cloud:perms-probe`. Snapshots overwritten in
place via atomic temp-file + rename. Exit codes: 0 green; 1 auth
failure or required action denied; 2 quota gap pending.
```

- [ ] **Step 7.3: Add `PROGRESS.md` Phase 39 entry.**

After the Phase 38 (Layer W) entry, add:

```markdown
### Phase 39 — Layer W+α (cloud bootstrap — SkyPilot perms front-load)

Zero-spend verification layer. Lands all AWS + GCP permissions + GPU
quota needed for the SkyPilot multi-cloud T4 smoke (Layer W+β). Spec:
`docs/superpowers/specs/2026-06-06-layer-w-alpha-cloud-bootstrap-design.md`.

- [x] Task 1: AWS scoped IAM policy doc — commit `<T1 SHA>`
- [x] Task 2: Operator gate — `.aws/README.md` apply instructions — commit `<T2 SHA>`
- [x] Task 3: AWS probe (sts + iam.simulate + ec2.describe + servicequotas.get) — commit `<T3 SHA>`
- [x] Task 4: GCP probe (regions + SA-role audit + T4 quota) — commit `<T4 SHA>`
- [x] Task 5: Quota gap handler — AWS auto-request, GCP console URL — commit `<T5 SHA>`
- [x] Task 6: `sky check aws gcp` clean — commit `<T6 SHA>`
- [x] Task 7: Docs + Phase 39 + final gate — commit `<T7 SHA>`

**Key design decisions:**
- Scoped IAM policy doc tracked (not secret); applied once via console.
- Probe mirrors `tools/preflight.py` seam pattern — every SDK call goes
  through a factory callable; tests inject fakes; no real cloud in CI.
- Snapshot writes are atomic (tmp-file + rename) — a crashed probe never
  leaves a half-written snapshot.
- AWS quota requests are idempotent via history lookup before submit.
- GCP quota requests have no SDK surface; probe prints a console URL
  + operator-actionable instructions and exits 2.

**Spend:** $0. Operator console actions: 2 (IAM policy attach + GCP
quota request) plus 2 chat acks.

**Out of scope (Layer W+β candidates):**
- Actual `sky launch` GPU smoke on AWS + GCP T4 instances.
- Azure / B2 / R2 SkyPilot enablement.
- AWS bucket scope-down on `AmazonS3FullAccess` — separate work.
```

- [ ] **Step 7.4: Update the "Single next action" block at the top of `PROGRESS.md`.**

Replace the existing block's contents with a pointer to Layer W+β + the candidate set.

- [ ] **Step 7.5: Add `README.md` pointer.**

Find the existing SkyPilot or "cloud" mention; add a one-paragraph note such as:

```markdown
### Cloud bootstrap (Layer W+α)

SkyPilot multi-cloud GPU work is gated by AWS + GCP permission and
quota readiness. Run `pixi run cloud:perms-probe` to verify; the
probe writes snapshots to `.aws/perms-snapshot.json` and
`.gcp/perms-snapshot.json` (gitignored). See
`docs/CLOUD-CREDS.md` for the bootstrap inventory.
```

- [ ] **Step 7.6: Final pre-commit + full test gate.**

```bash
pixi run pre-commit run --all-files
pixi run test
pixi run cloud:perms-probe
```

All three must exit 0.

- [ ] **Step 7.7: Commit T7 with PROGRESS SHA backfill.**

First commit the docs without the SHA:

```bash
git add docs/CLOUD-CREDS.md PROGRESS.md README.md
git commit -m "$(cat <<'EOF'
docs(layer-w-alpha): CLOUD-CREDS + PROGRESS Phase 39 + README pointer

Layer W+α T7. SkyPilot perms rows green on both clouds. Done
criteria all checked. Next action points at Layer W+β.
EOF
)"
```

Then backfill the SHA into PROGRESS.md Task 7 line if the workflow requires it (see prior layers' SHA-backfill pattern). Optional second commit:

```bash
git add PROGRESS.md
git commit -m "chore(progress): backfill Layer W+α T7 SHA"
```

- [ ] **Step 7.8: Merge into main via `--no-ff`.**

If working on a feature branch, return to main and merge with the layer-summary message pattern from prior phases (see commit `4672735` / `9e02e15` as templates).

```bash
git checkout main
git merge --no-ff layer-w-alpha -m "$(cat <<'EOF'
Layer W+α — cloud bootstrap (SkyPilot perms front-load)

Zero-spend verification layer. AWS scoped IAM policy applied;
quota auto-requested; GCP roles verified; T4 quota request filed
via console (no SDK surface). `pixi run cloud:perms-probe` exits 0.

Tasks: T1 <SHA> T2 <SHA> T3 <SHA> T4 <SHA> T5 <SHA> T6 <SHA> T7 <SHA>.
EOF
)"
```

---

## Self-Review (run before handoff)

**1. Spec coverage.**

| Spec section | Plan task |
|---|---|
| §3 Files — `.aws/policies/skypilot-minimal.json` | T1 |
| §3 Files — `.aws/README.md` apply instructions | T2 |
| §3 Files — `.aws/perms-snapshot.json` | T3 |
| §3 Files — `.gcp/perms-snapshot.json` | T4 |
| §3 Files — `tools/cloud_perms_probe.py` | T3 + T4 + T5 |
| §3 Files — `docs/CLOUD-CREDS.md` | T6 + T7 |
| §3 Files — `pixi.toml` `cloud:perms-probe` | T3 |
| §5 T1–T7 | T1–T7 |
| §6 Error handling — atomic snapshot writes | T3 (`_write_snapshot_atomic`) |
| §6 Idempotent quota request | T5 (`_aws_existing_quota_case` short-circuit) |
| §7 Unit tests with fake SDK clients | T3 + T4 + T5 |
| §8 Done criteria | T7 |

All spec sections covered.

**2. Placeholder scan.** No `TBD`, no `TODO`, no "fill in details". Every code block contains complete code. Verify commands carry expected output where applicable.

**3. Type consistency.** Function signatures match across tasks:
- `probe_aws(session, *, snapshot_path=None, region=..., ...)` consistent T3 → T5.
- `probe_gcp(*, clients, project, sa_email, ...)` consistent T4 → T5.
- `_write_snapshot_atomic(path, data)` referenced from both probes with the same signature.

**4. Operator-action minimization.** Two console actions:
- T2: paste IAM policy JSON (one-time, unavoidable — `kinoforge-ci` can't elevate itself).
- T5 GCP: file quota request (one-time, unavoidable — no SDK surface).

Two chat acks (T2 + T5 GCP). Everything else runs without operator involvement.

---

**Plan saved to:** `docs/superpowers/plans/2026-06-06-layer-w-alpha-cloud-bootstrap.md`
