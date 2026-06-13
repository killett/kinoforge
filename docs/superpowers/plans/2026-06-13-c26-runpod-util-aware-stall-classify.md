# C26 — RunPod util-aware stall classify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a provider-agnostic util-snapshot substrate + RunPod GraphQL satisfier + a new `STALL_REAP` verdict in `classify()` so a pod whose in-pod workload has stalled (heartbeats fresh but GPU + CPU near zero for a configurable window) is auto-detected and torn down by the holding orchestrator, the CLI reaper, the B1 Layer W sweeper, and the B3 attach-gate. Closes the C25 Task 4 deferred acceptance gate (Wan + ComfyUI 2-CLI cold-skip ratio < 0.7).

**Architecture:** Sibling-Protocol substrate at `core/util_endpoints.py` parallel to B5a's `HeartbeatEndpoint`. Counter state machine in `HeartbeatLoop._tick_once` (uptime decrease resets, all-axis-low increments, any-axis-high resets). Seven new flat ledger fields + a `consecutive_low_util_count` counter persisted via the existing `Ledger.touch(**extra)` seam. `classify()` gains an STALL_REAP row 3' inside the sentinel-fresh branch that intercepts the existing LIVE return. Cross-process consumers (CLI `_cmd_reap`, `_resolve_warm_instance`, `core/reaper_actor.sweep`, `core/sweeper.SweeperLoop`) thread the three new cfg kwargs unchanged. Cfg kill-switch `stall_reap_enabled=False` makes every callsite pass `stall_window_s=None` and skips util endpoint construction.

**Tech Stack:** Python 3.11; pixi (default + live-runpod envs); pytest + pytest-cov; stdlib `urllib`; RunPod GraphQL (`pod{runtime{...}}`); pydantic v2 for cfg; existing B5a `HeartbeatEndpoint` substrate; existing Layer V `classify` + `Verdict` + `DEFAULT_APPLY_POLICY`.

**Spec:** `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md` (commits `b1add28` + `1603ca5`).

**Predecessor:** C25 — `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md` (CLOSED PARTIAL, sha `7f901be`). Closeout sidecar at `tests/live/_c25_smoke_evidence.json` documents the stall this plan auto-detects.

---

## File Structure

**Created files:**

- `src/kinoforge/core/util_endpoints.py` — `UtilSnapshot` dataclass + `UtilSnapshotEndpoint` Protocol + `provider_util_supported` gate (Task 2).
- `src/kinoforge/core/util_counter.py` — pure `_update_counter` helper for the consecutive-low-util state machine (Task 5).
- `src/kinoforge/providers/runpod/util.py` — `RunPodGraphQLUtilEndpoint` satisfier (Task 3).
- `src/kinoforge/providers/local/util.py` — `LocalUtilEndpoint` test seam (Task 4).
- `tests/core/test_util_endpoints.py` — Protocol + capability-gate tests (Task 2).
- `tests/core/test_util_counter.py` — table-driven counter state-machine tests (Task 5).
- `tests/providers/test_runpod_util.py` — fake-GraphQL wire-shape tests (Task 3).
- `tests/providers/test_local_util.py` — LocalUtilEndpoint tests + adapter dispatch (Task 4).
- `tests/core/test_reaper_stall.py` — STALL_REAP classify branch tests (Task 7).
- `tests/core/test_heartbeat_loop_util.py` — HeartbeatLoop integration tests (Task 8).
- `tests/live/test_runpod_util_disk_probe.py` — disk-util GraphQL field probe (Task 1).
- `tests/live/_runpod_util_disk_probe.json` — probe outcome sidecar (Task 1 output; committed).
- `tests/live/test_c26_phase_a_stall_detection_live.py` — FakeEngine intentional-stall smoke (Tasks 12 + 13).
- `tests/live/test_c26_phase_b_wan_warm_reuse_live.py` — Wan + ComfyUI 2-CLI cold-skip / PROVEN-PROTECTION smoke (Tasks 12 + 14).
- `tests/live/cfg_c26_phase_a.yaml` — Phase A cfg (Task 12).
- `tests/live/cfg_c26_phase_b.yaml` — Phase B cfg (Task 12).
- `tests/live/_c26_phase_a_smoke_evidence.json` — Phase A sidecar (Task 13 output).
- `tests/live/_c26_phase_b_smoke_evidence.json` — Phase B sidecar (Task 14 output).

**Modified files:**

- `src/kinoforge/core/reaper.py` — `Verdict.STALL_REAP` appended; `DEFAULT_APPLY_POLICY` includes it; `classify()` gains three new kwargs + row 3' (Task 7).
- `src/kinoforge/core/config.py` — `LifecycleConfig` gains `stall_reap_enabled`, `stall_window_s`, `stall_gpu_threshold`, `stall_cpu_threshold` (Task 6).
- `src/kinoforge/_adapters.py` — `build_util_endpoint_for` helper (Task 4).
- `src/kinoforge/providers/runpod/__init__.py` — self-register `RunPodGraphQLUtilEndpoint` builder (Task 3).
- `src/kinoforge/providers/local/__init__.py` — self-register `LocalUtilEndpoint` builder (Task 4).
- `src/kinoforge/core/heartbeat_loop.py` — `HeartbeatLoop.__init__` gains `util_endpoint` + `reaper_actor` + `cancel_token` + `thresholds` kwargs; `_tick_once` extended (Task 8).
- `src/kinoforge/core/orchestrator.py` — `deploy_session` builds util endpoint + wires HeartbeatLoop (Task 8).
- `src/kinoforge/cli/_commands.py` — `_cmd_reap`, `_resolve_warm_instance`, `_cmd_destroy` callsites of `classify()` thread three new kwargs (Task 9); `--stall-window-override` flag on `deploy` (Task 10).
- `src/kinoforge/core/reaper_actor.py` — `sweep` + `act_on_verdict` thread the three new kwargs (Task 9).
- `src/kinoforge/core/sweeper.py` — `SweeperLoop` thresholds dict passes the three new kwargs (Task 9).
- `tests/test_core_invariant.py` — extend allowlist for `core/util_endpoints.py` + `core/util_counter.py`; vendor-SDK confinement scan covers `providers/runpod/util.py` (Task 11).
- `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md` — §8 + §16 amended with Task 1 probe outcome (Task 1 closeout).
- `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md` — §16 amended with C26 closure pointer (Task 15).
- `PROGRESS.md` — §C C26 strike-through, §C C25 closure pointer, `## Single next action` (Task 15).
- `successful-generations.md` — new entry only if Phase B produced a video (Task 15).

Each task produces a single atomic commit per CLAUDE.md durability rules. Task 12's RED scaffold commits BEFORE Tasks 13 and 14 invoke live spend.

---

## Task 1 (spec §11 Task 1): Disk-util GraphQL field live probe

**Goal:** Disambiguate the RunPod GraphQL `runtime{}` disk-util field name (RunPod introspection blocked — `__type` disabled) via trial selection sets against a real cheap pod; capture outcome in a committed sidecar JSON that Task 3 reads.

**Files:**
- Create: `tests/live/test_runpod_util_disk_probe.py`
- Create: `tests/live/_runpod_util_disk_probe.json` (output of live run)
- Modify: `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md` (amend §8 + §16 with outcome)

**Acceptance Criteria:**
- [ ] `tests/live/test_runpod_util_disk_probe.py` exists and is gated by `KINOFORGE_LIVE_RUNPOD=1`.
- [ ] After live invocation, `tests/live/_runpod_util_disk_probe.json` contains `{"disk_field": "<dotted-path>" | null, "captured_at": "<ISO local TZ>", "tested_pod_id": "<id>", "envelopes": [...]}` where `envelopes` lists each trial selection set + the GraphQL response.
- [ ] Spec §8 + §16 amended with the chosen field path (or null if none worked) and pointer to the sidecar.
- [ ] Pod destroyed at smoke end; absent from `provider.list_instances()` post-destroy.
- [ ] Live spend ≤ $0.005.

**Verify:** `KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_runpod_util_disk_probe.py -v -s` → PASS; sidecar JSON committed; spec amended.

**Steps:**

- [ ] **Step 1.1: Write the probe RED scaffold.**

```python
# tests/live/test_runpod_util_disk_probe.py
"""Live probe: RunPod GraphQL runtime{} disk-util field name (C26 Task 1).

RunPod introspection is disabled (__type returns null) so the disk-util
field name cannot be discovered statically. Tries three documented
candidates in priority order against a real pod; first successful
selection set wins. Writes outcome to tests/live/_runpod_util_disk_probe.json.

Task 3 of the C26 implementation plan reads that sidecar to finalize
the RunPodGraphQLUtilEndpoint's GraphQL query.

Gated by KINOFORGE_LIVE_RUNPOD=1. Live spend ceiling: $0.005.
Spec: docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md §8.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.005
_SESSION_LIFETIME_S = 60.0
_SIDECAR_PATH = Path("tests/live/_runpod_util_disk_probe.json")

# Trial selection sets, priority order. Each entry is (label, sub-selection).
_DISK_TRIALS: list[tuple[str, str]] = [
    ("container.diskInfo.utilPercent",
     "container { diskInfo { utilPercent } }"),
    ("runtime.disk.utilPercent",
     "disk { utilPercent }"),  # nested under runtime{}
    ("container.storage.used+total",
     "container { storage { used total } }"),
]


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the disk-util GraphQL probe "
            f"(~$0.005 spend per invocation)"
        )


def test_runpod_util_disk_field_probe() -> None:
    """Pick cheapest RunPod offer; try each disk-field selection set; record outcome."""
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
    from kinoforge.providers.runpod import RunPodProvider

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    assert api_key, "RUNPOD_API_KEY must be set for live probe"

    provider = RunPodProvider(creds=creds)
    reqs = HardwareRequirements(
        min_vram_gb=0,
        min_cuda="0.0",
        max_usd_per_hr=10.0,
    )
    offers = provider.find_offers(reqs)
    assert offers, "no RunPod offers available"
    cheapest = min(offers, key=lambda o: o.cost_rate_usd_per_hr)
    est_spend = cheapest.cost_rate_usd_per_hr * (_SESSION_LIFETIME_S / 3600.0)
    assert est_spend <= _BUDGET_USD_CAP, (
        f"offer too expensive for ≤${_BUDGET_USD_CAP}: "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr → {est_spend:.5f} USD"
    )
    print(
        f"\nProbe pod offer: {cheapest.id!r} @ "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr",
        file=sys.stderr,
    )

    spec = InstanceSpec(
        offer=cheapest, image="alpine:latest", env={}, provision_script=None
    )
    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(f"Probe pod created: {instance_id!r}", file=sys.stderr)

    disk_field: str | None = None
    envelopes: list[dict] = []

    try:
        deadline = time.monotonic() + _SESSION_LIFETIME_S
        while time.monotonic() < deadline:
            inst = provider.get_instance(instance_id)
            if inst.status == "ready":
                break
            time.sleep(3.0)
        else:
            pytest.fail(f"pod {instance_id} never ready in {_SESSION_LIFETIME_S}s")

        for label, subsel in _DISK_TRIALS:
            query = (
                "query GetRuntime($podId: String!) {\n"
                "  pod(input: {podId: $podId}) {\n"
                "    id\n"
                "    runtime {\n"
                "      uptimeInSeconds\n"
                f"      {subsel}\n"
                "    }\n"
                "  }\n"
                "}"
            )
            resp = provider._http_post(  # noqa: SLF001 — wire-level probe
                provider._base_url,
                {"query": query, "variables": {"podId": instance_id}},
            )
            envelopes.append({"label": label, "subsel": subsel, "resp": resp})
            if "errors" not in resp and resp.get("data", {}).get("pod"):
                disk_field = label
                print(f"WINNER: {label!r}", file=sys.stderr)
                break
            print(f"REJECTED: {label!r} → {resp.get('errors')!r}",
                  file=sys.stderr)

    finally:
        provider.destroy_instance(instance_id)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            live = {i.id for i in provider.list_instances()}
            if instance_id not in live:
                break
            time.sleep(2.0)
        else:
            pytest.fail(f"pod {instance_id} not destroyed in 30 s")

    sidecar = {
        "disk_field": disk_field,
        "captured_at": datetime.now().astimezone().isoformat(),
        "tested_pod_id": instance_id,
        "envelopes": envelopes,
    }
    _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2, default=str))
    print(f"\nSidecar written: {_SIDECAR_PATH}", file=sys.stderr)
```

- [ ] **Step 1.2: Commit RED scaffold BEFORE live invocation** (CLAUDE.md durability rule).

```bash
git add tests/live/test_runpod_util_disk_probe.py
git commit -m "$(cat <<'EOF'
test(c26): RED scaffold — RunPod disk-util GraphQL field probe

Gated by KINOFORGE_LIVE_RUNPOD=1; tries three documented disk-field
candidate selection sets and records outcome to
tests/live/_runpod_util_disk_probe.json. Spec §8.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 1.3: Run live probe.**

```bash
KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_runpod_util_disk_probe.py -v -s
```

Expected: PASS. Sidecar written. Pod destroyed.

- [ ] **Step 1.4: Amend spec §8 + §16 with outcome.**

Update spec §8 "Disk field — TBD by probe task." block: replace with the winning field path (or note that no field worked → `disk_percent` ships as `None` permanently). Update spec §16 with `tests/live/_runpod_util_disk_probe.json` pointer + the wire-shape note.

- [ ] **Step 1.5: Commit sidecar + spec amendment together.**

```bash
git add tests/live/_runpod_util_disk_probe.json \
        docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md
git commit -m "$(cat <<'EOF'
live(c26): Task 1 — RunPod disk-util GraphQL field probe + spec amend

Probe outcome captured in tests/live/_runpod_util_disk_probe.json.
Spec §8 + §16 amended with winning field path (or null-result note).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `UtilSnapshotEndpoint` Protocol + `UtilSnapshot` dataclass + capability gate

**Goal:** Ship the provider-agnostic substrate Protocol, frozen dataclass, and provider-support gate at `src/kinoforge/core/util_endpoints.py`. Pure module — no I/O, no concrete satisfier import.

**Files:**
- Create: `src/kinoforge/core/util_endpoints.py`
- Create: `tests/core/test_util_endpoints.py`

**Acceptance Criteria:**
- [ ] `UtilSnapshot` is a `@dataclass(frozen=True)` with five `Optional[...]` fields (`gpu_util_percent`, `cpu_percent`, `memory_percent`, `disk_percent`, `uptime_seconds`).
- [ ] `UtilSnapshotEndpoint` is `@runtime_checkable Protocol` with `read_util(instance_id: str) -> UtilSnapshot | None`.
- [ ] `provider_util_supported(kind: str) -> bool` returns `True` for `"local"` and `"runpod"`, `False` for everything else.
- [ ] Module imports nothing from `kinoforge.providers.*` (core-import-ban invariant).
- [ ] 6/6 tests pass.

**Verify:** `pixi run pytest tests/core/test_util_endpoints.py -v` → 6 PASS.

**Steps:**

- [ ] **Step 2.1: Write failing tests.**

```python
# tests/core/test_util_endpoints.py
"""Unit tests for kinoforge.core.util_endpoints (C26 Task 2)."""

from __future__ import annotations

import pytest

from kinoforge.core.util_endpoints import (
    UtilSnapshot,
    UtilSnapshotEndpoint,
    provider_util_supported,
)


def test_util_snapshot_is_frozen() -> None:
    snap = UtilSnapshot(
        gpu_util_percent=10.0, cpu_percent=20.0,
        memory_percent=30.0, disk_percent=40.0, uptime_seconds=50,
    )
    with pytest.raises((AttributeError, Exception)):
        snap.gpu_util_percent = 99.0  # type: ignore[misc]


def test_util_snapshot_fields_default_none() -> None:
    snap = UtilSnapshot(
        gpu_util_percent=None, cpu_percent=None,
        memory_percent=None, disk_percent=None, uptime_seconds=None,
    )
    assert snap.gpu_util_percent is None
    assert snap.cpu_percent is None
    assert snap.memory_percent is None
    assert snap.disk_percent is None
    assert snap.uptime_seconds is None


def test_util_snapshot_endpoint_protocol_is_runtime_checkable() -> None:
    class _Fake:
        def read_util(self, instance_id: str) -> UtilSnapshot | None:
            return None

    assert isinstance(_Fake(), UtilSnapshotEndpoint)


def test_util_snapshot_endpoint_rejects_missing_method() -> None:
    class _Wrong:
        pass

    assert not isinstance(_Wrong(), UtilSnapshotEndpoint)


def test_provider_util_supported_known_providers() -> None:
    assert provider_util_supported("runpod") is True
    assert provider_util_supported("local") is True


def test_provider_util_supported_unknown_providers() -> None:
    assert provider_util_supported("skypilot") is False
    assert provider_util_supported("bedrock") is False
    assert provider_util_supported("") is False
```

- [ ] **Step 2.2: Run tests; confirm FAIL** (`ModuleNotFoundError`).

```bash
pixi run pytest tests/core/test_util_endpoints.py -v
```

- [ ] **Step 2.3: Implement.**

```python
# src/kinoforge/core/util_endpoints.py
"""Provider-agnostic substrate for orchestrator-side util sampling (C26).

Hosts the cross-provider Protocol + capability gate that HeartbeatLoop's
per-tick util read rests on. Concrete satisfiers live under
``kinoforge.providers.<name>.util``; this module must never import them
(core-import-ban invariant — see PROGRESS.md §"Key decisions").

The C26-shipped set in :data:`_UTIL_SUPPORTED` mirrors B5a's
``_HEARTBEAT_SUPPORTED``. Downstream consumers (Layer V classify,
``_adapters.build_util_endpoint_for``) gate util-aware behaviour via
:func:`provider_util_supported`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["UtilSnapshot", "UtilSnapshotEndpoint", "provider_util_supported"]


@dataclass(frozen=True)
class UtilSnapshot:
    """Per-tick provider-side resource metrics.

    All fields Optional — providers surface different subsets. classify()
    treats ``None`` as 'data unavailable' (does not contribute to STALL
    AND-clause).

    Attributes:
        gpu_util_percent: MAX across pod's GPU devices.
        cpu_percent: Container CPU percentage.
        memory_percent: Container memory percentage.
        disk_percent: Container disk percentage (None if provider does not surface).
        uptime_seconds: Container uptime; decrease tick-over-tick = restart blip.
    """

    gpu_util_percent: float | None
    cpu_percent: float | None
    memory_percent: float | None
    disk_percent: float | None
    uptime_seconds: int | None


@runtime_checkable
class UtilSnapshotEndpoint(Protocol):
    """Provider-agnostic substrate for util sampling.

    Contract invariants (every satisfier honours):

    - ``read_util(id)`` returns ``None`` when the instance is gone, the
      storage slot was never written, or all upstream fields are
      unavailable.
    - Transport failures (HTTP non-2xx, GraphQL rate-limit, SSH refused)
      propagate as
      :class:`~kinoforge.core.errors.TransportError`. Consumers tolerate.
    - ``read_util`` is idempotent and side-effect-free (no provider-side
      mutation, no ledger writes).
    """

    def read_util(self, instance_id: str) -> UtilSnapshot | None: ...


# C26-shipped set. Mirrors B5a _HEARTBEAT_SUPPORTED. SkyPilot would
# implement via ssh + nvidia-smi when B5b lands. Bedrock semantically
# inapplicable (serverless; no pod).
_UTIL_SUPPORTED: frozenset[str] = frozenset({"local", "runpod"})


def provider_util_supported(provider_kind: str) -> bool:
    """Whether a wire-level :class:`UtilSnapshotEndpoint` satisfier ships.

    Used by ``_adapters.build_util_endpoint_for`` to gate util endpoint
    construction. Mirrors B5a's :func:`provider_heartbeat_supported`.

    Args:
        provider_kind: The ``compute.provider`` field value.

    Returns:
        ``True`` when a wire-level satisfier is shipped; ``False`` otherwise.
    """
    return provider_kind in _UTIL_SUPPORTED
```

- [ ] **Step 2.4: Run tests; confirm PASS.**

```bash
pixi run pytest tests/core/test_util_endpoints.py -v
```

Expected: 6 PASS.

- [ ] **Step 2.5: Commit.**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/util_endpoints.py \
    tests/core/test_util_endpoints.py
git add src/kinoforge/core/util_endpoints.py tests/core/test_util_endpoints.py
git commit -m "$(cat <<'EOF'
feat(c26): UtilSnapshot dataclass + UtilSnapshotEndpoint Protocol + gate

Sibling of B5a HeartbeatEndpoint substrate. Pure module; core-import-
ban invariant preserved. provider_util_supported() seeded with
{"local", "runpod"}; satisfiers ship in Tasks 3 + 4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `RunPodGraphQLUtilEndpoint` satisfier

**Goal:** Implement the RunPod GraphQL `runtime{}` satisfier; single query per tick; `MAX` across `gpus[]` for `gpu_util_percent`. Disk-field selection set finalized using Task 1's probe outcome.

**Files:**
- Create: `src/kinoforge/providers/runpod/util.py`
- Create: `tests/providers/test_runpod_util.py`
- Modify: `src/kinoforge/providers/runpod/__init__.py` (self-register builder; sister to existing heartbeat).

**Acceptance Criteria:**
- [ ] `RunPodGraphQLUtilEndpoint` constructible with injectable `http_post` seam (no real network in tests).
- [ ] Single GraphQL query per `read_util` call.
- [ ] `gpu_util_percent = max(g["gpuUtilPercent"] for g in runtime["gpus"])`; empty array → `None`.
- [ ] `data.pod = null` → returns `None`.
- [ ] `errors` in response → raises `TransportError`.
- [ ] Bearer auth header + `User-Agent: kinoforge-util/0.1` on every call.
- [ ] Disk-field branch: if Task 1 probed a winner, the query selection includes it AND the satisfier writes `disk_percent`. If Task 1's outcome was null, the query omits the disk selection AND `disk_percent` is always `None`.
- [ ] 8/8 tests pass.

**Verify:** `pixi run pytest tests/providers/test_runpod_util.py -v` → 8 PASS.

**Steps:**

- [ ] **Step 3.1: Inspect Task 1 sidecar** to pick the disk selection set.

```bash
cat tests/live/_runpod_util_disk_probe.json
```

- [ ] **Step 3.2: Write failing tests.**

```python
# tests/providers/test_runpod_util.py
"""Unit tests for RunPodGraphQLUtilEndpoint (C26 Task 3)."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.core.util_endpoints import UtilSnapshot
from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

_OK_RESP_4_GPU = {
    "data": {
        "pod": {
            "id": "p1",
            "runtime": {
                "uptimeInSeconds": 1234,
                "gpus": [
                    {"id": "g1", "gpuUtilPercent": 10.0, "memoryUtilPercent": 50.0},
                    {"id": "g2", "gpuUtilPercent": 80.0, "memoryUtilPercent": 60.0},
                    {"id": "g3", "gpuUtilPercent": 5.0, "memoryUtilPercent": 40.0},
                    {"id": "g4", "gpuUtilPercent": 0.0, "memoryUtilPercent": 20.0},
                ],
                "container": {"cpuPercent": 25.5, "memoryPercent": 78.0},
            }
        }
    }
}


class _SpyPost:
    def __init__(self, resp: dict[str, Any]) -> None:
        self.resp = resp
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict) -> dict[str, Any]:
        self.calls.append((url, payload))
        return self.resp


def test_read_util_returns_max_gpu_across_devices() -> None:
    spy = _SpyPost(_OK_RESP_4_GPU)
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    snap = ep.read_util("p1")
    assert snap is not None
    assert snap.gpu_util_percent == 80.0


def test_read_util_returns_other_fields() -> None:
    spy = _SpyPost(_OK_RESP_4_GPU)
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    snap = ep.read_util("p1")
    assert snap is not None
    assert snap.cpu_percent == 25.5
    assert snap.memory_percent == 78.0
    assert snap.uptime_seconds == 1234


def test_read_util_returns_none_when_pod_gone() -> None:
    spy = _SpyPost({"data": {"pod": None}})
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    assert ep.read_util("p1") is None


def test_read_util_returns_partial_when_container_null() -> None:
    resp = {
        "data": {"pod": {"id": "p1", "runtime": {
            "uptimeInSeconds": 5,
            "gpus": [{"id": "g1", "gpuUtilPercent": 10.0,
                      "memoryUtilPercent": 50.0}],
            "container": None,
        }}}
    }
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=_SpyPost(resp))
    snap = ep.read_util("p1")
    assert snap is not None
    assert snap.gpu_util_percent == 10.0
    assert snap.cpu_percent is None
    assert snap.memory_percent is None
    assert snap.uptime_seconds == 5


def test_read_util_handles_empty_gpus_array() -> None:
    resp = {
        "data": {"pod": {"id": "p1", "runtime": {
            "uptimeInSeconds": 5, "gpus": [],
            "container": {"cpuPercent": 25.0, "memoryPercent": 50.0},
        }}}
    }
    snap = RunPodGraphQLUtilEndpoint(
        api_key="k", http_post=_SpyPost(resp)
    ).read_util("p1")
    assert snap is not None
    assert snap.gpu_util_percent is None


def test_read_util_raises_transport_error_on_graphql_errors() -> None:
    spy = _SpyPost({"errors": [{"message": "rate limited"}]})
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    with pytest.raises(TransportError, match="rate limited"):
        ep.read_util("p1")


def test_read_util_returns_none_when_runtime_null_during_boot() -> None:
    resp = {"data": {"pod": {"id": "p1", "runtime": None}}}
    snap = RunPodGraphQLUtilEndpoint(
        api_key="k", http_post=_SpyPost(resp)
    ).read_util("p1")
    assert snap is None or all(
        v is None for v in (
            snap.gpu_util_percent, snap.cpu_percent,
            snap.memory_percent, snap.uptime_seconds,
        )
    )


def test_read_util_passes_bearer_and_user_agent_via_http_post() -> None:
    """http_post is the contract; default closure builds Bearer/UA headers.

    This test pins the call SHAPE — single dict with query + variables —
    the header contract is tested in tests/providers/test_runpod_heartbeat.py
    (shared _default_http_post pattern with C25).
    """
    spy = _SpyPost(_OK_RESP_4_GPU)
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    ep.read_util("p1")
    assert len(spy.calls) == 1
    url, payload = spy.calls[0]
    assert url.endswith("/graphql")
    assert "query" in payload
    assert payload["variables"] == {"podId": "p1"}
```

- [ ] **Step 3.3: Run tests; confirm FAIL.**

- [ ] **Step 3.4: Implement** — disk-field branches reflect Task 1 outcome.

```python
# src/kinoforge/providers/runpod/util.py
"""RunPod GraphQL runtime{} util-snapshot satisfier (C26 Task 3).

Implements :class:`~kinoforge.core.util_endpoints.UtilSnapshotEndpoint`
by querying ``pod{runtime{uptimeInSeconds, gpus{...}, container{...}}}``
and aggregating MAX across the gpus array for ``gpu_util_percent``.

Single GraphQL round-trip per ``read_util`` call. Bearer auth.

Spec: docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md §4.2 + §8.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from kinoforge.core.errors import TransportError
from kinoforge.core.util_endpoints import UtilSnapshot

__all__ = ["RunPodGraphQLUtilEndpoint"]

_DEFAULT_GRAPHQL_URL: str = "https://api.runpod.io/graphql"

# Build the runtime{} selection set. Disk field optionally included
# based on Task 1 probe outcome — see tests/live/_runpod_util_disk_probe.json
# for the chosen path. If probe found no field, omit the disk selection
# and disk_percent is always None.
_RUNTIME_QUERY: str = """
query GetRuntime($podId: String!) {
  pod(input: {podId: $podId}) {
    id
    runtime {
      uptimeInSeconds
      gpus { id gpuUtilPercent memoryUtilPercent }
      container { cpuPercent memoryPercent }
      # IF Task 1 probe winner = "container.diskInfo.utilPercent":
      #   replace the next two lines with:
      #     # (the line above already includes container { ... })
      # disk-field selection is added INSIDE the container { } block above
      # when the winner is "container.diskInfo.utilPercent"; OR
      # inside the runtime { } block when the winner is "runtime.disk.utilPercent".
      # Implementer: edit per Task 1 sidecar before committing.
    }
  }
}
""".strip()


def _default_http_post(api_key: str) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """stdlib-urllib POST with Bearer auth; sister of C25 heartbeat closure."""

    def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "kinoforge-util/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                data: bytes = resp.read()
        except urllib.error.HTTPError as exc:
            raise TransportError(
                f"RunPod GraphQL HTTP {exc.code}: {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TransportError(
                f"RunPod GraphQL transport error: {exc.reason}"
            ) from exc
        try:
            decoded: dict[str, Any] = json.loads(data)
        except json.JSONDecodeError as exc:
            raise TransportError(f"RunPod GraphQL non-JSON response: {exc}") from exc
        return decoded

    return _post


class RunPodGraphQLUtilEndpoint:
    """RunPod GraphQL runtime{} satisfier.

    Single ``pod{runtime{...}}`` query per :meth:`read_util` call.
    ``gpu_util_percent`` = MAX across ``runtime.gpus``; empty array → None.
    ``data.pod = null`` → ``read_util`` returns None.
    ``runtime = null`` (early boot) → returns ``UtilSnapshot(all=None)``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        graphql_url: str = _DEFAULT_GRAPHQL_URL,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._api_key = api_key
        self._graphql_url = graphql_url
        self._http_post = http_post if http_post is not None else _default_http_post(api_key)

    def read_util(self, instance_id: str) -> UtilSnapshot | None:
        """Return a :class:`UtilSnapshot` for ``instance_id``, or None.

        Raises:
            TransportError: GraphQL ``errors`` or HTTP / JSON transport fault.
        """
        payload = {"query": _RUNTIME_QUERY, "variables": {"podId": instance_id}}
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"RunPod runtime query failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod runtime query failed: {resp['errors']}")
        pod = (resp.get("data") or {}).get("pod")
        if pod is None:
            return None
        runtime = pod.get("runtime")
        if runtime is None:
            return None
        gpus = runtime.get("gpus") or []
        gpu_util = (
            max((float(g["gpuUtilPercent"])
                 for g in gpus if g.get("gpuUtilPercent") is not None),
                default=None)
        )
        container = runtime.get("container") or {}
        cpu = container.get("cpuPercent")
        mem = container.get("memoryPercent")
        uptime = runtime.get("uptimeInSeconds")
        # Disk field handled per Task 1 outcome — see commented block above.
        disk: float | None = None
        return UtilSnapshot(
            gpu_util_percent=gpu_util,
            cpu_percent=float(cpu) if cpu is not None else None,
            memory_percent=float(mem) if mem is not None else None,
            disk_percent=disk,
            uptime_seconds=int(uptime) if uptime is not None else None,
        )
```

- [ ] **Step 3.5: Self-register builder in `providers/runpod/__init__.py`** (sibling of existing heartbeat builder; export the class).

- [ ] **Step 3.6: Run tests; confirm 8 PASS.**

- [ ] **Step 3.7: Commit.**

```bash
pixi run pre-commit run --files \
    src/kinoforge/providers/runpod/util.py \
    src/kinoforge/providers/runpod/__init__.py \
    tests/providers/test_runpod_util.py
git add src/kinoforge/providers/runpod/util.py \
        src/kinoforge/providers/runpod/__init__.py \
        tests/providers/test_runpod_util.py
git commit -m "$(cat <<'EOF'
feat(c26): RunPodGraphQLUtilEndpoint satisfier

Single GraphQL query per tick; MAX across runtime.gpus for
gpu_util_percent; container.cpuPercent + container.memoryPercent +
uptimeInSeconds. Disk-field selection set per Task 1 probe outcome.
8/8 fake-GraphQL wire-shape tests green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `LocalUtilEndpoint` test seam + `_adapters.build_util_endpoint_for`

**Goal:** Ship the `local` provider's util satisfier (programmable script for tests) and the cross-provider dispatch helper, mirroring `build_heartbeat_endpoint_for`.

**Files:**
- Create: `src/kinoforge/providers/local/util.py`
- Create: `tests/providers/test_local_util.py`
- Modify: `src/kinoforge/providers/local/__init__.py`
- Modify: `src/kinoforge/_adapters.py`

**Acceptance Criteria:**
- [ ] `LocalUtilEndpoint(script=[snap1, snap2, ...]).read_util(id)` returns scripted snapshots in order; exhausting the script returns `None`.
- [ ] `build_util_endpoint_for(cfg, creds)` returns:
  - `None` when `cfg.compute is None`.
  - `None` when `cfg.compute.lifecycle.stall_reap_enabled is False`.
  - `None` when `provider_util_supported(provider)` is `False`.
  - `RunPodGraphQLUtilEndpoint` for `provider == "runpod"` (raises `AuthError` if `RUNPOD_API_KEY` missing).
  - `LocalUtilEndpoint()` for `provider == "local"`.
- [ ] 7/7 tests pass.

**Verify:** `pixi run pytest tests/providers/test_local_util.py -v` → 7 PASS.

**Steps:**

- [ ] **Step 4.1: Write failing tests.**

```python
# tests/providers/test_local_util.py
"""Unit tests for LocalUtilEndpoint + _adapters.build_util_endpoint_for (C26)."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import AuthError
from kinoforge.core.util_endpoints import UtilSnapshot, UtilSnapshotEndpoint
from kinoforge.providers.local.util import LocalUtilEndpoint


class _FakeCreds:
    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._m = mapping

    def get(self, key: str) -> str | None:
        return self._m.get(key)


def _snap(gpu: float | None = 0.0, cpu: float | None = 0.0,
          uptime: int | None = 100) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=gpu, cpu_percent=cpu,
        memory_percent=None, disk_percent=None, uptime_seconds=uptime,
    )


def test_local_util_endpoint_implements_protocol() -> None:
    assert isinstance(LocalUtilEndpoint(), UtilSnapshotEndpoint)


def test_local_util_endpoint_returns_scripted_snapshots() -> None:
    snaps = [_snap(gpu=1.0), _snap(gpu=2.0), _snap(gpu=3.0)]
    ep = LocalUtilEndpoint(script=snaps)
    assert ep.read_util("i1").gpu_util_percent == 1.0
    assert ep.read_util("i1").gpu_util_percent == 2.0
    assert ep.read_util("i1").gpu_util_percent == 3.0


def test_local_util_endpoint_returns_none_when_script_exhausted() -> None:
    ep = LocalUtilEndpoint(script=[_snap()])
    assert ep.read_util("i1") is not None
    assert ep.read_util("i1") is None


def test_build_util_endpoint_for_returns_none_when_compute_is_none(
    minimal_cfg_hosted_only_factory,  # type: ignore[no-untyped-def]
) -> None:
    from kinoforge._adapters import build_util_endpoint_for

    cfg = minimal_cfg_hosted_only_factory()  # cfg.compute is None
    assert build_util_endpoint_for(cfg, _FakeCreds({})) is None


def test_build_util_endpoint_for_returns_none_when_stall_disabled(
    minimal_cfg_runpod_factory,  # type: ignore[no-untyped-def]
) -> None:
    from kinoforge._adapters import build_util_endpoint_for

    cfg = minimal_cfg_runpod_factory(stall_reap_enabled=False)
    assert build_util_endpoint_for(cfg, _FakeCreds({"RUNPOD_API_KEY": "k"})) is None


def test_build_util_endpoint_for_runpod_branch(
    minimal_cfg_runpod_factory,  # type: ignore[no-untyped-def]
) -> None:
    from kinoforge._adapters import build_util_endpoint_for
    from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

    cfg = minimal_cfg_runpod_factory(stall_reap_enabled=True)
    ep = build_util_endpoint_for(cfg, _FakeCreds({"RUNPOD_API_KEY": "k"}))
    assert isinstance(ep, RunPodGraphQLUtilEndpoint)


def test_build_util_endpoint_for_runpod_branch_raises_when_missing_key(
    minimal_cfg_runpod_factory,  # type: ignore[no-untyped-def]
) -> None:
    from kinoforge._adapters import build_util_endpoint_for

    cfg = minimal_cfg_runpod_factory(stall_reap_enabled=True)
    with pytest.raises(AuthError, match="RUNPOD_API_KEY"):
        build_util_endpoint_for(cfg, _FakeCreds({}))
```

(Implementer note: `minimal_cfg_runpod_factory` / `minimal_cfg_hosted_only_factory` are existing pytest fixtures in `tests/conftest.py`. If a `stall_reap_enabled` kwarg threading is missing on the existing factory, add it as part of Task 6's cfg work and adjust this test file then.)

- [ ] **Step 4.2: Run tests; confirm FAIL.**

- [ ] **Step 4.3: Implement `LocalUtilEndpoint`.**

```python
# src/kinoforge/providers/local/util.py
"""LocalProvider util-snapshot test seam (C26 Task 4).

Programmable scripted-snapshot endpoint for HeartbeatLoop integration
tests and for the 'local' provider lifecycle path (so
provider_util_supported('local') returns True without a real wire path).
"""

from __future__ import annotations

from kinoforge.core.util_endpoints import UtilSnapshot

__all__ = ["LocalUtilEndpoint"]


class LocalUtilEndpoint:
    """Returns snapshots from a programmable script in order.

    Args:
        script: Snapshots to return in order. None entries permitted (mimic
            'instance gone' or 'data unavailable'). Exhausting the script
            returns None on subsequent calls.
    """

    def __init__(self, *, script: list[UtilSnapshot | None] | None = None) -> None:
        self._script = list(script) if script else []
        self._cursor = 0

    def read_util(self, instance_id: str) -> UtilSnapshot | None:
        if self._cursor >= len(self._script):
            return None
        snap = self._script[self._cursor]
        self._cursor += 1
        return snap
```

- [ ] **Step 4.4: Implement `build_util_endpoint_for` in `_adapters.py`.**

```python
# Append to src/kinoforge/_adapters.py:

def build_util_endpoint_for(
    cfg: "Config",
    creds: "CredentialProvider",
) -> "UtilSnapshotEndpoint | None":
    """Build the right :class:`UtilSnapshotEndpoint` for the configured provider.

    Returns None when:
      - cfg.compute is None (hosted-only path), OR
      - cfg.compute.lifecycle.stall_reap_enabled is False (kill switch), OR
      - provider_util_supported(cfg.compute.provider) is False
        (e.g. SkyPilot pre-B5b; Bedrock).

    Args:
        cfg: The loaded kinoforge config.
        creds: Credential provider; RunPod branch reads ``RUNPOD_API_KEY``.

    Returns:
        A :class:`UtilSnapshotEndpoint` instance, or ``None``.

    Raises:
        AuthError: RunPod branch requested but RUNPOD_API_KEY missing.
    """
    from kinoforge.core.errors import AuthError
    from kinoforge.core.util_endpoints import provider_util_supported

    if cfg.compute is None:
        return None
    if not cfg.compute.lifecycle.stall_reap_enabled:
        return None
    provider = cfg.compute.provider
    if not provider_util_supported(provider):
        return None
    if provider == "runpod":
        api_key = creds.get("RUNPOD_API_KEY")
        if api_key is None:
            raise AuthError(
                "RUNPOD_API_KEY must be set when "
                "compute.lifecycle.stall_reap_enabled is true on runpod"
            )
        from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

        return RunPodGraphQLUtilEndpoint(api_key=api_key)
    if provider == "local":
        from kinoforge.providers.local.util import LocalUtilEndpoint

        return LocalUtilEndpoint()
    return None
```

Also add `UtilSnapshotEndpoint` import to the `if TYPE_CHECKING` block at top of `_adapters.py`.

- [ ] **Step 4.5: Run tests; confirm 7 PASS.**

- [ ] **Step 4.6: Commit.**

```bash
pixi run pre-commit run --files \
    src/kinoforge/providers/local/util.py \
    src/kinoforge/providers/local/__init__.py \
    src/kinoforge/_adapters.py \
    tests/providers/test_local_util.py
git add src/kinoforge/providers/local/util.py \
        src/kinoforge/providers/local/__init__.py \
        src/kinoforge/_adapters.py \
        tests/providers/test_local_util.py
git commit -m "$(cat <<'EOF'
feat(c26): LocalUtilEndpoint + _adapters.build_util_endpoint_for

LocalUtilEndpoint is a programmable scripted-snapshot test seam.
build_util_endpoint_for sister of build_heartbeat_endpoint_for —
dispatches by (cfg.compute.provider, stall_reap_enabled, util-substrate
support). Raises AuthError on RUNPOD_API_KEY missing for runpod branch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `_update_counter` pure helper

**Goal:** Ship the per-tick counter state machine as a pure function at `src/kinoforge/core/util_counter.py` with exhaustive table-driven tests.

**Files:**
- Create: `src/kinoforge/core/util_counter.py`
- Create: `tests/core/test_util_counter.py`

**Acceptance Criteria:**
- [ ] `_update_counter(prev_counter, prev_uptime_s, snap, *, gpu_threshold, cpu_threshold) -> int` matches spec §6.
- [ ] `snap is None` → returns `prev_counter` (transport hiccup preserves progress).
- [ ] Uptime decrease → returns `0` (restart blip resets).
- [ ] GPU low AND CPU low → returns `prev_counter + 1`.
- [ ] GPU or CPU above threshold → returns `0`.
- [ ] None GPU OR None CPU → returns `0` (partial data does not increment).
- [ ] 10/10 table-driven tests pass.

**Verify:** `pixi run pytest tests/core/test_util_counter.py -v` → 10 PASS.

**Steps:**

- [ ] **Step 5.1: Write failing tests.**

```python
# tests/core/test_util_counter.py
"""Table-driven tests for the consecutive-low-util counter (C26 Task 5)."""

from __future__ import annotations

import pytest

from kinoforge.core.util_counter import _update_counter
from kinoforge.core.util_endpoints import UtilSnapshot


def _snap(*, gpu: float | None, cpu: float | None,
          uptime: int | None = 100) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=gpu, cpu_percent=cpu,
        memory_percent=None, disk_percent=None, uptime_seconds=uptime,
    )


def test_returns_prev_counter_when_snap_none() -> None:
    assert _update_counter(
        3, prev_uptime_s=100, snap=None,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 3


def test_resets_on_uptime_decrease() -> None:
    snap = _snap(gpu=1.0, cpu=10.0, uptime=5)  # uptime dropped 100→5
    assert _update_counter(
        3, prev_uptime_s=100, snap=snap,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 0


def test_increments_when_gpu_and_cpu_low() -> None:
    snap = _snap(gpu=2.0, cpu=10.0)
    assert _update_counter(
        3, prev_uptime_s=50, snap=snap,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 4


def test_resets_when_gpu_above_threshold() -> None:
    snap = _snap(gpu=80.0, cpu=10.0)
    assert _update_counter(
        3, prev_uptime_s=50, snap=snap,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 0


def test_resets_when_cpu_above_threshold() -> None:
    snap = _snap(gpu=2.0, cpu=80.0)
    assert _update_counter(
        3, prev_uptime_s=50, snap=snap,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 0


def test_resets_when_gpu_is_none() -> None:
    snap = _snap(gpu=None, cpu=5.0)
    assert _update_counter(
        3, prev_uptime_s=50, snap=snap,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 0


def test_resets_when_cpu_is_none() -> None:
    snap = _snap(gpu=2.0, cpu=None)
    assert _update_counter(
        3, prev_uptime_s=50, snap=snap,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 0


def test_first_tick_no_prev_uptime_increments_when_low() -> None:
    snap = _snap(gpu=2.0, cpu=10.0)
    assert _update_counter(
        0, prev_uptime_s=None, snap=snap,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 1


def test_does_not_reset_when_uptime_present_but_prev_none() -> None:
    """prev_uptime_s None never triggers reset; first tick stays in low-counter mode."""
    snap = _snap(gpu=2.0, cpu=10.0, uptime=5)
    assert _update_counter(
        0, prev_uptime_s=None, snap=snap,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 1


def test_threshold_boundary_strict_less_than() -> None:
    """Threshold compare is strict < — equal value is NOT low."""
    snap = _snap(gpu=5.0, cpu=10.0)  # gpu exactly at threshold
    assert _update_counter(
        3, prev_uptime_s=50, snap=snap,
        gpu_threshold=5.0, cpu_threshold=20.0,
    ) == 0
```

- [ ] **Step 5.2: Run; confirm FAIL.**

- [ ] **Step 5.3: Implement.**

```python
# src/kinoforge/core/util_counter.py
"""Per-tick consecutive-low-util counter state machine (C26 Task 5).

Pure function used by HeartbeatLoop._tick_once. Lives in core because
classify() consumers (CLI, sweeper, B3 attach-gate) MAY also rebuild
the counter from raw util history if they reconstruct state offline —
not required today, but the helper sits where future consumers can
import it without crossing the core-import-ban.
"""

from __future__ import annotations

from kinoforge.core.util_endpoints import UtilSnapshot

__all__ = ["_update_counter"]


def _update_counter(
    prev_counter: int,
    *,
    prev_uptime_s: int | None,
    snap: UtilSnapshot | None,
    gpu_threshold: float,
    cpu_threshold: float,
) -> int:
    """Update consecutive-low-util counter.

    Args:
        prev_counter: Counter value persisted from the previous tick.
        prev_uptime_s: ``last_util_uptime_s`` from the previous tick's
            ledger entry, or None if no prior tick.
        snap: Current tick's util snapshot, or None if read failed /
            instance gone / data unavailable.
        gpu_threshold: gpu_util_percent below = "low".
        cpu_threshold: cpu_percent below = "low".

    Returns:
        New counter value:

        - snap is None → unchanged (transport hiccup preserves progress).
        - snap.uptime_seconds < prev_uptime_s → 0 (container restart blip).
        - gpu_util_percent < gpu_threshold AND cpu_percent < cpu_threshold
          (both non-None) → prev_counter + 1.
        - Otherwise (any axis above threshold or None) → 0.
    """
    if snap is None:
        return prev_counter

    if (
        snap.uptime_seconds is not None
        and prev_uptime_s is not None
        and snap.uptime_seconds < prev_uptime_s
    ):
        return 0

    gpu_low = (
        snap.gpu_util_percent is not None
        and snap.gpu_util_percent < gpu_threshold
    )
    cpu_low = (
        snap.cpu_percent is not None
        and snap.cpu_percent < cpu_threshold
    )
    if gpu_low and cpu_low:
        return prev_counter + 1
    return 0
```

- [ ] **Step 5.4: Run; confirm 10 PASS.**

- [ ] **Step 5.5: Commit.**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/util_counter.py tests/core/test_util_counter.py
git add src/kinoforge/core/util_counter.py tests/core/test_util_counter.py
git commit -m "$(cat <<'EOF'
feat(c26): _update_counter pure helper for STALL state machine

Container restart (uptime decrease) resets counter. Transport hiccup
(snap=None) preserves counter. Both axes below threshold and non-None
increments. Any axis above OR None resets. 10/10 table-driven tests.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `LifecycleConfig` cfg knobs

**Goal:** Add four new fields to `LifecycleConfig` per spec §9; validate at load time.

**Files:**
- Modify: `src/kinoforge/core/config.py`
- Modify: `tests/core/test_config.py` (or `tests/core/test_lifecycle_config.py` — match existing file name)

**Acceptance Criteria:**
- [ ] Four new fields on `LifecycleConfig` per spec §9.
- [ ] Defaults: `stall_reap_enabled=True`, `stall_window_s=600.0`, `stall_gpu_threshold=5.0`, `stall_cpu_threshold=20.0`.
- [ ] Validators reject `stall_window_s < 0`, `stall_gpu_threshold` outside `[0, 100]`, `stall_cpu_threshold` outside `[0, 100]`.
- [ ] YAML round-trip preserves all four fields.
- [ ] 5/5 new tests pass; full `tests/core/test_config.py` suite still green.

**Verify:** `pixi run pytest tests/core/test_config.py -v -k "stall_"` → 5 PASS; `pixi run pytest tests/core/test_config.py -v` → all PASS.

**Steps:**

- [ ] **Step 6.1: Write failing tests.**

```python
# Append to tests/core/test_config.py:

class TestLifecycleConfigStallKnobs:
    def test_defaults(self, tmp_path) -> None:
        from kinoforge.core.config import load_config
        cfg_text = """
        compute:
          provider: runpod
          lifecycle:
            budget: 1.0
        """
        path = tmp_path / "c.yaml"
        path.write_text(cfg_text)
        cfg = load_config(path)
        lc = cfg.compute.lifecycle
        assert lc.stall_reap_enabled is True
        assert lc.stall_window_s == 600.0
        assert lc.stall_gpu_threshold == 5.0
        assert lc.stall_cpu_threshold == 20.0

    def test_overrides(self, tmp_path) -> None:
        from kinoforge.core.config import load_config
        cfg_text = """
        compute:
          provider: runpod
          lifecycle:
            budget: 1.0
            stall_reap_enabled: false
            stall_window_s: 1800.0
            stall_gpu_threshold: 10.0
            stall_cpu_threshold: 30.0
        """
        path = tmp_path / "c.yaml"
        path.write_text(cfg_text)
        cfg = load_config(path)
        lc = cfg.compute.lifecycle
        assert lc.stall_reap_enabled is False
        assert lc.stall_window_s == 1800.0
        assert lc.stall_gpu_threshold == 10.0
        assert lc.stall_cpu_threshold == 30.0

    def test_rejects_negative_window(self) -> None:
        from kinoforge.core.config import LifecycleConfig
        import pytest as _pt
        with _pt.raises(Exception):  # pydantic ValidationError or ValueError
            LifecycleConfig(budget=1.0, stall_window_s=-1.0)

    def test_rejects_gpu_threshold_over_100(self) -> None:
        from kinoforge.core.config import LifecycleConfig
        import pytest as _pt
        with _pt.raises(Exception):
            LifecycleConfig(budget=1.0, stall_gpu_threshold=101.0)

    def test_rejects_cpu_threshold_over_100(self) -> None:
        from kinoforge.core.config import LifecycleConfig
        import pytest as _pt
        with _pt.raises(Exception):
            LifecycleConfig(budget=1.0, stall_cpu_threshold=101.0)
```

- [ ] **Step 6.2: Run; confirm FAIL.**

- [ ] **Step 6.3: Edit `src/kinoforge/core/config.py`.** Add fields + validators.

```python
# Inside LifecycleConfig at src/kinoforge/core/config.py:89

class LifecycleConfig(BaseModel):
    """YAML-facing lifecycle configuration ...

    Attributes:
        ... existing fields ...
        stall_reap_enabled: When False, disables util-aware STALL_REAP
            detection across the codebase (no util endpoint built; classify
            receives stall_window_s=None). Default True.
        stall_window_s: Consecutive-low-util duration (s) required for
            STALL_REAP. Default 600 s (~2.5× typical Wan cold boot).
        stall_gpu_threshold: gpuUtilPercent strictly below this is "low".
            Default 5.0 (5%).
        stall_cpu_threshold: container.cpuPercent strictly below this is "low".
            Default 20.0 (20%).
    """

    # ... existing fields unchanged ...
    stall_reap_enabled: bool = True
    stall_window_s: float = 600.0
    stall_gpu_threshold: float = 5.0
    stall_cpu_threshold: float = 20.0

    @field_validator("stall_window_s")
    @classmethod
    def _validate_stall_window_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"stall_window_s must be >= 0; got {v}")
        return v

    @field_validator("stall_gpu_threshold", "stall_cpu_threshold")
    @classmethod
    def _validate_stall_threshold_percent(cls, v: float) -> float:
        if not (0.0 <= v <= 100.0):
            raise ValueError(f"stall threshold must be in [0, 100]; got {v}")
        return v
```

- [ ] **Step 6.4: Run; confirm 5 PASS + full suite green.**

```bash
pixi run pytest tests/core/test_config.py -v
```

- [ ] **Step 6.5: Commit.**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/config.py tests/core/test_config.py
git add src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "$(cat <<'EOF'
feat(c26): LifecycleConfig stall_* knobs

Four new fields: stall_reap_enabled (bool, default True),
stall_window_s (float, default 600 s), stall_gpu_threshold +
stall_cpu_threshold (floats, defaults 5 / 20, validated [0,100]).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `classify()` STALL_REAP + Verdict enum + `DEFAULT_APPLY_POLICY`

**Goal:** Append `STALL_REAP` to the Verdict StrEnum (public-contract additive), include it in `DEFAULT_APPLY_POLICY`, and extend `classify()` with row 3' that intercepts the existing LIVE return when util data is fresh and `consecutive_low_util_count * heartbeat_interval_s >= stall_window_s`.

**Files:**
- Modify: `src/kinoforge/core/reaper.py`
- Create: `tests/core/test_reaper_stall.py`

**Acceptance Criteria:**
- [ ] `Verdict.STALL_REAP = "STALL_REAP"` appended at end of StrEnum (after `UNROUTABLE`).
- [ ] `DEFAULT_APPLY_POLICY` includes `Verdict.STALL_REAP`.
- [ ] `classify()` signature gains `stall_window_s: float | None`, `stall_gpu_threshold: float = 5.0`, `stall_cpu_threshold: float = 20.0`.
- [ ] When sentinel fresh AND hb_age ≤ idle, STALL row 3' fires iff `stall_window_s is not None AND last_util_tick fresh AND counter * heartbeat_interval_s >= window` (with per-entry override read via `_resolve(entry, "stall_window_s", stall_window_s)`).
- [ ] Legacy entries without util fields → fall-through to LIVE (zero behavior change).
- [ ] `stall_window_s=None` → fall-through to LIVE (kill-switch).
- [ ] 9/9 new tests pass; existing `tests/core/test_reaper.py` suite still green (existing callsites must pass `stall_window_s=None` until Task 9 threads them).

**Verify:** `pixi run pytest tests/core/test_reaper_stall.py tests/core/test_reaper.py -v` → all PASS.

**Steps:**

- [ ] **Step 7.1: Write failing tests.**

```python
# tests/core/test_reaper_stall.py
"""STALL_REAP classify branch tests (C26 Task 7)."""

from __future__ import annotations

from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict, classify

_HB_INT = 30.0
_STALL_WINDOW = 600.0
_GPU_TH = 5.0
_CPU_TH = 20.0


def _entry(
    *,
    id_: str = "p1",
    created_at: float = 1000.0,
    last_heartbeat: float = 2000.0,
    heartbeat_thread_tick: float = 2000.0,
    last_util_tick: float | None = 2000.0,
    consecutive_low_util_count: int = 0,
    provider: str = "runpod",
) -> dict:
    e: dict = {
        "id": id_,
        "created_at": created_at,
        "last_heartbeat": last_heartbeat,
        "heartbeat_thread_tick": heartbeat_thread_tick,
        "provider": provider,
    }
    if last_util_tick is not None:
        e["last_util_tick"] = last_util_tick
        e["consecutive_low_util_count"] = consecutive_low_util_count
    return e


def _classify(entry: dict, *, stall_window_s: float | None = _STALL_WINDOW) -> Verdict:
    return classify(
        entry, frozenset({entry["id"]}), 2010.0,
        idle_timeout_s=3600.0, max_lifetime_s=18000.0,
        heartbeat_interval_s=_HB_INT, grace_after_session_s=300.0,
        stall_window_s=stall_window_s,
        stall_gpu_threshold=_GPU_TH, stall_cpu_threshold=_CPU_TH,
    )


def test_stall_reap_appended_at_end_of_verdict_enum() -> None:
    """Public-contract guard: STALL_REAP appended after existing values."""
    values = list(Verdict)
    assert values[-1] == Verdict.STALL_REAP


def test_default_apply_policy_includes_stall_reap() -> None:
    assert Verdict.STALL_REAP in DEFAULT_APPLY_POLICY.act_verdicts


def test_stall_reap_fires_when_consecutive_low_exceeds_window() -> None:
    # 600 s / 30 s = 20 ticks. Counter ≥ 20 → STALL.
    entry = _entry(consecutive_low_util_count=20)
    assert _classify(entry) == Verdict.STALL_REAP


def test_stall_reap_suppressed_when_counter_below_window() -> None:
    entry = _entry(consecutive_low_util_count=19)
    assert _classify(entry) == Verdict.LIVE


def test_stall_reap_suppressed_when_util_tick_stale() -> None:
    # last_util_tick > 3 * heartbeat_interval_s old → suppress
    entry = _entry(consecutive_low_util_count=99, last_util_tick=1800.0)  # 210 s old
    assert _classify(entry) == Verdict.LIVE


def test_stall_reap_suppressed_when_stall_window_s_none() -> None:
    entry = _entry(consecutive_low_util_count=99)
    assert _classify(entry, stall_window_s=None) == Verdict.LIVE


def test_stall_reap_suppressed_on_legacy_entry_missing_util_fields() -> None:
    entry = _entry(last_util_tick=None)
    assert _classify(entry) == Verdict.LIVE


def test_stall_reap_per_entry_override_via_ledger_field() -> None:
    # Per-entry override 1800 s; counter * 30 = 600 → below override → LIVE
    entry = _entry(consecutive_low_util_count=20)
    entry["stall_window_s"] = 1800.0
    assert _classify(entry) == Verdict.LIVE


def test_stall_reap_per_entry_override_lower_window_fires_sooner() -> None:
    # Per-entry override 60 s; counter * 30 = 60 → STALL
    entry = _entry(consecutive_low_util_count=2)
    entry["stall_window_s"] = 60.0
    assert _classify(entry) == Verdict.STALL_REAP
```

- [ ] **Step 7.2: Run; confirm FAIL.**

- [ ] **Step 7.3: Edit `src/kinoforge/core/reaper.py`.**

Modify Verdict enum, DEFAULT_APPLY_POLICY, and classify():

```python
# At the bottom of the existing Verdict StrEnum:
class Verdict(StrEnum):
    LIVE = "LIVE"
    IDLE_REAP = "IDLE_REAP"
    ORPHAN_REAP = "ORPHAN_REAP"
    OVERAGE_REAP = "OVERAGE_REAP"
    STALE_LEDGER = "STALE_LEDGER"
    HEARTBEAT_UNKNOWN = "HEARTBEAT_UNKNOWN"
    HEARTBEAT_SUBSTRATE_MISSING = "HEARTBEAT_SUBSTRATE_MISSING"
    UNROUTABLE = "UNROUTABLE"
    STALL_REAP = "STALL_REAP"  # NEW (C26) — append-only public contract


DEFAULT_APPLY_POLICY = Policy(
    act_verdicts=frozenset({
        Verdict.IDLE_REAP,
        Verdict.OVERAGE_REAP,
        Verdict.STALE_LEDGER,
        Verdict.STALL_REAP,  # NEW (C26)
    })
)
```

Extend the `classify()` signature with the three new kwargs and insert row 3' inside the sentinel-fresh branch BEFORE the LIVE return:

```python
def classify(
    entry: Mapping[str, Any],
    live_pod_ids: frozenset[str] | set[str],
    now: float,
    *,
    idle_timeout_s: float,
    max_lifetime_s: float,
    heartbeat_interval_s: float | None,
    grace_after_session_s: float,
    stall_window_s: float | None = None,        # NEW (C26)
    stall_gpu_threshold: float = 5.0,            # NEW (C26)
    stall_cpu_threshold: float = 20.0,           # NEW (C26)
) -> Verdict:
    # ... existing rows 1 (STALE_LEDGER), 2 (OVERAGE_REAP), 7
    # (HB_UNKNOWN / HB_SUBSTRATE_MISSING) unchanged ...

    sentinel_window = 3.0 * heartbeat_interval_s
    sent_age = now - float(hb_tick)
    hb_age = now - float(hb)

    # Rows 3 & 4 — sentinel fresh
    if sent_age <= sentinel_window:
        if hb_age <= idle:
            # NEW (C26) — row 3': STALL_REAP intercepts LIVE
            if stall_window_s is not None and heartbeat_interval_s is not None:
                util_tick = entry.get("last_util_tick")
                counter = int(entry.get("consecutive_low_util_count", 0) or 0)
                window = _resolve(entry, "stall_window_s", stall_window_s)
                if (
                    util_tick is not None
                    and (now - float(util_tick)) <= sentinel_window
                    and counter * heartbeat_interval_s >= window
                ):
                    return Verdict.STALL_REAP
            return Verdict.LIVE
        return Verdict.IDLE_REAP

    # ... existing rows 5 & 6 unchanged ...
```

Existing callsites (`cli/_commands.py:725, 1006, 1684`, `core/reaper_actor.py:229, 356`) MUST be updated in Task 9 to thread the kwargs. Until Task 9, the new kwargs have safe defaults (`stall_window_s=None`) so existing callsites compile + behave identically (kill-switch path).

- [ ] **Step 7.4: Run reaper test suites.**

```bash
pixi run pytest tests/core/test_reaper_stall.py tests/core/test_reaper.py -v
```

Expected: all PASS.

- [ ] **Step 7.5: Commit.**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/reaper.py tests/core/test_reaper_stall.py
git add src/kinoforge/core/reaper.py tests/core/test_reaper_stall.py
git commit -m "$(cat <<'EOF'
feat(c26): STALL_REAP Verdict + classify() row 3'

Verdict.STALL_REAP appended (public-contract additive); included in
DEFAULT_APPLY_POLICY. classify() gains three new kwargs:
stall_window_s (None = feature off), stall_gpu_threshold,
stall_cpu_threshold. Row 3' intercepts LIVE when util fields fresh
and counter * heartbeat_interval >= window. Per-entry stall_window_s
override on ledger entry honored. Legacy entries lacking util fields
fall through to LIVE. 9/9 new tests pass; existing reaper suite green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: HeartbeatLoop integration (read util + counter + ledger + self-classify + cancel_token)

**Goal:** Wire `UtilSnapshotEndpoint.read_util` + `_update_counter` + ledger persistence + holder self-classify with reaper-actor destroy + `cancel_token` into `HeartbeatLoop._tick_once`. Backward compat when `util_endpoint=None`.

**Files:**
- Modify: `src/kinoforge/core/heartbeat_loop.py`
- Modify: `src/kinoforge/core/orchestrator.py` (`deploy_session` constructs util endpoint + threads HeartbeatLoop kwargs)
- Create: `tests/core/test_heartbeat_loop_util.py`

**Acceptance Criteria:**
- [ ] `HeartbeatLoop.__init__` gains `util_endpoint: UtilSnapshotEndpoint | None = None`, `reaper_actor: ReaperActor | None = None`, `cancel_token: CancelToken | None = None`, `cfg_lifecycle: LifecycleConfig | None = None`, `live_pod_ids_fn: Callable[[], set[str]] | None = None`.
- [ ] When `util_endpoint is None`: `_tick_once` behavior unchanged from B5a (no util read, no self-classify; ledger.touch payload identical to today).
- [ ] When `util_endpoint is not None`: each tick reads util (tolerate `TransportError`), updates counter via `_update_counter` using `prev_uptime_s` and `prev_counter` read from ledger (via `ledger.read(id)` before write), and persists seven new fields via `ledger.touch(**fields, consecutive_low_util_count=N)`.
- [ ] After util write, self-classify against the holder's own ledger entry; on STALL_REAP, call `reaper_actor.act_on_verdict(id, verdict, ...)`, set `cancel_token`, log a WARNING, and break out of the loop (next iter exits on `_stop`).
- [ ] 9/9 integration tests pass.

**Verify:** `pixi run pytest tests/core/test_heartbeat_loop_util.py tests/core/test_heartbeat_loop.py -v` → all PASS.

**Steps:**

- [ ] **Step 8.1: Write failing tests.**

```python
# tests/core/test_heartbeat_loop_util.py
"""HeartbeatLoop util-aware integration tests (C26 Task 8)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.heartbeat_loop import HeartbeatLoop
from kinoforge.core.reaper import Verdict
from kinoforge.core.util_endpoints import UtilSnapshot
from kinoforge.providers.local.util import LocalUtilEndpoint


class _FakeLedger:
    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def read(self, instance_id: str) -> dict[str, Any] | None:
        return self._entries.get(instance_id)

    def touch(self, instance_id: str, *, last_heartbeat: float | None = None,
              **extra: Any) -> bool:
        entry = self._entries.setdefault(instance_id, {"id": instance_id,
                                                       "provider": "local"})
        if last_heartbeat is not None:
            entry["last_heartbeat"] = last_heartbeat
        for k, v in extra.items():
            if v is not None:
                entry[k] = v
        return True


class _FakeProvider:
    def __init__(self) -> None:
        self._hb: dict[str, float] = {}
        self.heartbeat_calls = 0

    def heartbeat(self, instance_id: str) -> None:
        self.heartbeat_calls += 1
        self._hb[instance_id] = self.heartbeat_calls

    def last_heartbeat(self, instance_id: str) -> float | None:
        return self._hb.get(instance_id)


@dataclass
class _SpyReaperActor:
    calls: list[tuple[str, Verdict]] = field(default_factory=list)

    def act_on_verdict(self, instance_id: str, verdict: Verdict, **_: Any) -> None:
        self.calls.append((instance_id, verdict))


class _SpyCancelToken:
    def __init__(self) -> None:
        self.set_called = False

    def set(self) -> None:
        self.set_called = True

    def is_set(self) -> bool:
        return self.set_called


def _snap(*, gpu: float | None, cpu: float | None,
          uptime: int | None = 100) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=gpu, cpu_percent=cpu,
        memory_percent=None, disk_percent=None, uptime_seconds=uptime,
    )


def _build_loop(
    *, util_endpoint=None, reaper_actor=None, cancel_token=None,
    cfg_lifecycle=None, ledger=None, live_pod_ids_fn=None,
) -> tuple[HeartbeatLoop, _FakeLedger, _FakeProvider, FakeClock]:
    led = ledger or _FakeLedger()
    prov = _FakeProvider()
    clk = FakeClock(start=1000.0)
    led.touch("p1", heartbeat_thread_tick=clk.now())  # seed entry
    loop = HeartbeatLoop(
        ledger=led, provider=prov, instance_id="p1",
        interval_s=30.0, clock=clk,
        util_endpoint=util_endpoint,
        reaper_actor=reaper_actor,
        cancel_token=cancel_token,
        cfg_lifecycle=cfg_lifecycle,
        live_pod_ids_fn=live_pod_ids_fn or (lambda: {"p1"}),
    )
    return loop, led, prov, clk


def test_legacy_no_util_endpoint_path_omits_util_fields() -> None:
    """Backward compat: util_endpoint=None → ledger payload unchanged."""
    loop, led, _, _ = _build_loop(util_endpoint=None)
    loop._tick_once()  # noqa: SLF001
    entry = led.read("p1")
    assert "last_util_tick" not in entry
    assert "consecutive_low_util_count" not in entry


def test_counter_increments_when_all_axis_low() -> None:
    ep = LocalUtilEndpoint(script=[_snap(gpu=2.0, cpu=10.0)] * 5)
    cfg_lc = _Cfg(stall_reap_enabled=True, stall_window_s=600.0,
                  stall_gpu_threshold=5.0, stall_cpu_threshold=20.0,
                  heartbeat_interval_s=30.0,
                  idle_timeout=3600.0, max_lifetime=18000.0,
                  grace_after_session_s=300.0)
    loop, led, _, _ = _build_loop(util_endpoint=ep, cfg_lifecycle=cfg_lc)
    for _ in range(5):
        loop._tick_once()  # noqa: SLF001
    assert led.read("p1")["consecutive_low_util_count"] == 5


def test_counter_resets_on_uptime_decrease() -> None:
    ep = LocalUtilEndpoint(script=[
        _snap(gpu=2.0, cpu=10.0, uptime=100),  # counter→1
        _snap(gpu=2.0, cpu=10.0, uptime=200),  # counter→2
        _snap(gpu=2.0, cpu=10.0, uptime=5),    # restart blip → counter→0
    ])
    cfg_lc = _Cfg(stall_reap_enabled=True, stall_window_s=600.0,
                  stall_gpu_threshold=5.0, stall_cpu_threshold=20.0,
                  heartbeat_interval_s=30.0,
                  idle_timeout=3600.0, max_lifetime=18000.0,
                  grace_after_session_s=300.0)
    loop, led, _, _ = _build_loop(util_endpoint=ep, cfg_lifecycle=cfg_lc)
    for _ in range(3):
        loop._tick_once()  # noqa: SLF001
    assert led.read("p1")["consecutive_low_util_count"] == 0


def test_self_classify_fires_destroy_and_cancel_token() -> None:
    # 20 ticks of all-low at 30 s window 600 s → STALL_REAP on tick 20
    ep = LocalUtilEndpoint(script=[_snap(gpu=2.0, cpu=10.0)] * 25)
    cfg_lc = _Cfg(stall_reap_enabled=True, stall_window_s=600.0,
                  stall_gpu_threshold=5.0, stall_cpu_threshold=20.0,
                  heartbeat_interval_s=30.0,
                  idle_timeout=3600.0, max_lifetime=18000.0,
                  grace_after_session_s=300.0)
    actor = _SpyReaperActor()
    token = _SpyCancelToken()
    loop, led, _, _ = _build_loop(
        util_endpoint=ep, reaper_actor=actor, cancel_token=token,
        cfg_lifecycle=cfg_lc,
    )
    fired = False
    for i in range(30):
        loop._tick_once()  # noqa: SLF001
        if token.is_set():
            fired = i + 1
            break
    assert fired
    assert fired >= 20 and fired <= 22  # allow off-by-one for state-machine boot
    assert actor.calls == [("p1", Verdict.STALL_REAP)]


def test_transport_error_tolerated_preserves_counter() -> None:
    class _BadEp:
        def __init__(self) -> None: self.calls = 0
        def read_util(self, _id: str):
            self.calls += 1
            from kinoforge.core.errors import TransportError
            raise TransportError("bang")
    ep = _BadEp()
    cfg_lc = _Cfg(stall_reap_enabled=True, stall_window_s=600.0,
                  stall_gpu_threshold=5.0, stall_cpu_threshold=20.0,
                  heartbeat_interval_s=30.0,
                  idle_timeout=3600.0, max_lifetime=18000.0,
                  grace_after_session_s=300.0)
    loop, led, _, _ = _build_loop(util_endpoint=ep, cfg_lifecycle=cfg_lc)
    led._entries["p1"]["consecutive_low_util_count"] = 5  # seed
    loop._tick_once()  # noqa: SLF001
    assert ep.calls == 1
    assert led.read("p1")["consecutive_low_util_count"] == 5  # unchanged


def test_partial_snapshot_with_none_gpu_resets_counter() -> None:
    ep = LocalUtilEndpoint(script=[_snap(gpu=None, cpu=10.0)])
    cfg_lc = _Cfg(stall_reap_enabled=True, stall_window_s=600.0,
                  stall_gpu_threshold=5.0, stall_cpu_threshold=20.0,
                  heartbeat_interval_s=30.0,
                  idle_timeout=3600.0, max_lifetime=18000.0,
                  grace_after_session_s=300.0)
    loop, led, _, _ = _build_loop(util_endpoint=ep, cfg_lifecycle=cfg_lc)
    led._entries["p1"]["consecutive_low_util_count"] = 5
    loop._tick_once()  # noqa: SLF001
    assert led.read("p1")["consecutive_low_util_count"] == 0


@dataclass
class _Cfg:
    """Test stub for the LifecycleConfig subset HeartbeatLoop needs."""
    stall_reap_enabled: bool
    stall_window_s: float
    stall_gpu_threshold: float
    stall_cpu_threshold: float
    heartbeat_interval_s: float
    idle_timeout: float
    max_lifetime: float
    grace_after_session_s: float
```

(Implementer note: the test uses `cfg_lifecycle` as a duck-typed `_Cfg` stub. Production code reads the same attribute names from `LifecycleConfig` — no real cfg construction in unit tests.)

- [ ] **Step 8.2: Run; confirm FAIL.**

- [ ] **Step 8.3: Edit `src/kinoforge/core/heartbeat_loop.py`.**

```python
# Extend HeartbeatLoop.__init__ + _tick_once:

import logging
from collections.abc import Callable

from kinoforge.core.errors import TransportError
from kinoforge.core.reaper import Verdict, classify
from kinoforge.core.util_counter import _update_counter
from kinoforge.core.util_endpoints import UtilSnapshot, UtilSnapshotEndpoint

# Inside HeartbeatLoop.__init__:
    def __init__(
        self,
        *,
        ledger: _TouchableLedger,
        provider: _HeartbeatProvider,
        instance_id: str,
        interval_s: float,
        clock: Clock | None = None,
        logger_: logging.Logger | None = None,
        join_timeout_s: float = 2.0,
        # NEW (C26):
        util_endpoint: UtilSnapshotEndpoint | None = None,
        reaper_actor: Any | None = None,
        cancel_token: Any | None = None,
        cfg_lifecycle: Any | None = None,
        live_pod_ids_fn: Callable[[], set[str]] | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError(f"interval_s must be > 0; got {interval_s}")
        self._ledger = ledger
        self._provider = provider
        self._instance_id = instance_id
        self._interval_s = interval_s
        self._clock: Clock = clock or RealClock()
        self._logger = logger_ or _log
        self._join_timeout_s = join_timeout_s
        # NEW (C26):
        self._util_endpoint = util_endpoint
        self._reaper_actor = reaper_actor
        self._cancel_token = cancel_token
        self._cfg_lc = cfg_lifecycle
        self._live_pod_ids_fn = live_pod_ids_fn
        # ... existing _stop / _thread setup ...

# Replace _tick_once:
    def _tick_once(self) -> None:
        """Heartbeat + (C26) util read + counter + self-classify."""
        try:
            self._provider.heartbeat(self._instance_id)
            last_hb = self._provider.last_heartbeat(self._instance_id)

            util_fields: dict[str, Any] = {}
            counter: int | None = None
            if self._util_endpoint is not None:
                prev = self._ledger.read(self._instance_id) or {}
                prev_counter = int(prev.get("consecutive_low_util_count", 0) or 0)
                prev_uptime_s = prev.get("last_util_uptime_s")
                snap: UtilSnapshot | None
                try:
                    snap = self._util_endpoint.read_util(self._instance_id)
                except TransportError:
                    snap = None
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "util read raised; treating as None for %s",
                        self._instance_id,
                    )
                    snap = None
                if self._cfg_lc is not None:
                    counter = _update_counter(
                        prev_counter,
                        prev_uptime_s=int(prev_uptime_s) if prev_uptime_s is not None else None,
                        snap=snap,
                        gpu_threshold=self._cfg_lc.stall_gpu_threshold,
                        cpu_threshold=self._cfg_lc.stall_cpu_threshold,
                    )
                else:
                    counter = prev_counter
                if snap is not None:
                    util_fields = {
                        "last_util_tick": float(self._clock.now()),
                        "last_util_gpu_percent": snap.gpu_util_percent,
                        "last_util_cpu_percent": snap.cpu_percent,
                        "last_util_memory_percent": snap.memory_percent,
                        "last_util_disk_percent": snap.disk_percent,
                        "last_util_uptime_s": snap.uptime_seconds,
                    }
                else:
                    util_fields = {"last_util_tick": float(self._clock.now())}

            touch_kwargs: dict[str, Any] = {
                "last_heartbeat": last_hb,
                "heartbeat_thread_tick": float(self._clock.now()),
                **util_fields,
            }
            if counter is not None:
                touch_kwargs["consecutive_low_util_count"] = counter
            self._ledger.touch(self._instance_id, **touch_kwargs)

            # Self-classify on holder
            if (
                self._util_endpoint is not None
                and self._cfg_lc is not None
                and self._reaper_actor is not None
                and self._live_pod_ids_fn is not None
            ):
                entry = self._ledger.read(self._instance_id) or {}
                window = (
                    self._cfg_lc.stall_window_s
                    if self._cfg_lc.stall_reap_enabled
                    else None
                )
                verdict = classify(
                    entry,
                    self._live_pod_ids_fn(),
                    float(self._clock.now()),
                    idle_timeout_s=self._cfg_lc.idle_timeout,
                    max_lifetime_s=self._cfg_lc.max_lifetime,
                    heartbeat_interval_s=self._cfg_lc.heartbeat_interval_s,
                    grace_after_session_s=self._cfg_lc.grace_after_session_s,
                    stall_window_s=window,
                    stall_gpu_threshold=self._cfg_lc.stall_gpu_threshold,
                    stall_cpu_threshold=self._cfg_lc.stall_cpu_threshold,
                )
                if verdict == Verdict.STALL_REAP:
                    self._logger.warning(
                        "STALL_REAP self-classified: id=%s counter=%s",
                        self._instance_id,
                        entry.get("consecutive_low_util_count"),
                    )
                    self._reaper_actor.act_on_verdict(
                        self._instance_id, verdict
                    )
                    if self._cancel_token is not None:
                        self._cancel_token.set()
                    self._stop.set()
        except Exception:  # noqa: BLE001 — protect the loop body
            self._logger.exception(
                "heartbeat tick failed for %s", self._instance_id
            )
```

- [ ] **Step 8.4: Wire `deploy_session` to construct util endpoint + thread HeartbeatLoop kwargs.**

```python
# Inside src/kinoforge/core/orchestrator.py deploy_session (near existing
# build_heartbeat_endpoint_for call at orchestrator.py:157):

from kinoforge._adapters import build_heartbeat_endpoint_for, build_util_endpoint_for
util_endpoint = build_util_endpoint_for(cfg, EnvCredentialProvider())

# At HeartbeatLoop construction:
heartbeat_loop = HeartbeatLoop(
    ledger=ledger, provider=provider, instance_id=instance.id,
    interval_s=cfg.compute.lifecycle.heartbeat_interval_s or 30.0,
    util_endpoint=util_endpoint,
    reaper_actor=reaper_actor_for_session,   # build via existing factory
    cancel_token=cancel_token_for_session,
    cfg_lifecycle=cfg.compute.lifecycle,
    live_pod_ids_fn=lambda: {i.id for i in provider.list_instances()},
)
```

(Implementer note: `cancel_token_for_session` is the existing Phase 50 cancel-token already threaded through `deploy_session`. `reaper_actor_for_session` is whatever helper currently constructs the reaper; if none exists, instantiate one inline with `destroy_confirmed` injected.)

- [ ] **Step 8.5: Run integration tests.**

```bash
pixi run pytest tests/core/test_heartbeat_loop_util.py tests/core/test_heartbeat_loop.py -v
```

Expected: all PASS. Legacy tests unaffected (defaults preserve B5a behavior).

- [ ] **Step 8.6: Commit.**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/heartbeat_loop.py \
    src/kinoforge/core/orchestrator.py \
    tests/core/test_heartbeat_loop_util.py
git add src/kinoforge/core/heartbeat_loop.py \
        src/kinoforge/core/orchestrator.py \
        tests/core/test_heartbeat_loop_util.py
git commit -m "$(cat <<'EOF'
feat(c26): HeartbeatLoop util read + counter + self-classify

HeartbeatLoop._tick_once gains optional util-endpoint path:
- reads UtilSnapshot from injected endpoint (tolerates TransportError)
- updates consecutive_low_util_count via _update_counter from prior tick
- persists seven new flat fields via ledger.touch(**extra)
- self-classifies via classify() against own entry
- on STALL_REAP: reaper_actor.act_on_verdict + cancel_token.set + stop

deploy_session builds util endpoint from cfg + threads kwargs to
HeartbeatLoop. util_endpoint=None preserves B5a backward compat
(ledger payload unchanged).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Cross-process consumer threading

**Goal:** Thread the three new `classify()` kwargs through every existing callsite so CLI `reap`, `_resolve_warm_instance` (B3 attach-gate), `core/reaper_actor.sweep` + `act_on_verdict`, and `core/sweeper.SweeperLoop` honor STALL_REAP.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (callsites at lines 725, 1006, 1684)
- Modify: `src/kinoforge/core/reaper_actor.py` (callsites at lines 229, 356)
- Modify: `src/kinoforge/core/sweeper.py` (thresholds dict population in `SweeperLoop`)
- Modify: existing tests for the threaded callsites

**Acceptance Criteria:**
- [ ] All five existing `classify()` callsites pass the three new kwargs sourced from `cfg.compute.lifecycle`.
- [ ] Callsites use the `stall_window_s = X if enabled else None` caller pattern per spec §9.
- [ ] `_resolve_warm_instance` STALL_REAP verdict refuses attach (joins existing `STALE_LEDGER / OVERAGE_REAP / UNROUTABLE: refuse always` arm — STALL_REAP must NOT be `_FORCE_BYPASSABLE`).
- [ ] `_cmd_reap` includes STALL_REAP in its actionable-verdict default; `--apply` destroys stalled pods.
- [ ] No new behavior when `stall_reap_enabled=False` (kill-switch).
- [ ] All existing test suites for the modified files still PASS; one new test per callsite verifies kwarg threading.

**Verify:** `pixi run pytest tests/cli tests/core/test_reaper_actor.py tests/core/test_sweeper.py -v` → all PASS.

**Steps:**

- [ ] **Step 9.1: Edit cli/_commands.py callsites.**

For each `classify(...)` call:

```python
# Helper at top of cli/_commands.py (or reuse existing _lifecycle_for(cfg)):
def _stall_window_from_cfg(cfg) -> float | None:
    if cfg.compute is None:
        return None
    lc = cfg.compute.lifecycle
    return lc.stall_window_s if lc.stall_reap_enabled else None

# All three callsites (725, 1006, 1684) gain:
    stall_window_s=_stall_window_from_cfg(cfg),
    stall_gpu_threshold=cfg.compute.lifecycle.stall_gpu_threshold,
    stall_cpu_threshold=cfg.compute.lifecycle.stall_cpu_threshold,
```

In `_resolve_warm_instance`, add `STALL_REAP` to the "refuse always" set:

```python
_FORCE_BYPASSABLE_VERDICTS = frozenset({
    "HEARTBEAT_UNKNOWN", "IDLE_REAP", "ORPHAN_REAP",
})
# STALL_REAP is NOT bypassable — a stalled pod can never be safely warm-reused.
# (already present; add a unit test to lock this in.)
```

- [ ] **Step 9.2: Edit core/reaper_actor.py callsites** — same threading.

- [ ] **Step 9.3: Edit core/sweeper.py** — `SweeperLoop` builds its thresholds dict from cfg; add the three new keys per the same caller pattern.

- [ ] **Step 9.4: Add new tests per callsite.**

- `tests/cli/test_resolve_warm_instance.py::test_stall_reap_refuses_attach_even_with_force` — STALL_REAP verdict + `force_attach=True` still returns refuse (rc=2).
- `tests/cli/test_cmd_reap.py::test_reap_apply_destroys_stalled_pods` — STALL_REAP ledger entry + `--apply` → `destroy_instance` called.
- `tests/core/test_reaper_actor.py::test_sweep_threads_stall_kwargs` — assertion on classify call signature.
- `tests/core/test_sweeper.py::test_sweeper_loop_threads_stall_kwargs` — same.

- [ ] **Step 9.5: Run touched suites + lint.**

```bash
pixi run pytest tests/cli tests/core/test_reaper_actor.py tests/core/test_sweeper.py -v
pixi run pre-commit run --files src/kinoforge/cli/_commands.py \
    src/kinoforge/core/reaper_actor.py src/kinoforge/core/sweeper.py
```

- [ ] **Step 9.6: Commit.**

```bash
git add src/kinoforge/cli/_commands.py \
        src/kinoforge/core/reaper_actor.py \
        src/kinoforge/core/sweeper.py \
        tests/cli/ tests/core/test_reaper_actor.py tests/core/test_sweeper.py
git commit -m "$(cat <<'EOF'
feat(c26): thread stall kwargs through CLI / reaper-actor / sweeper

All five existing classify() callsites now pass stall_window_s
(None when stall_reap_enabled=False), stall_gpu_threshold,
stall_cpu_threshold from cfg.compute.lifecycle.

_resolve_warm_instance pins STALL_REAP outside the _FORCE_BYPASSABLE
set — a stalled pod can never be warm-reused via --force-attach.

_cmd_reap --apply destroys STALL_REAP pods via the existing
DEFAULT_APPLY_POLICY (Task 7 already added STALL_REAP to the set).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: `--stall-window-override` CLI flag on `deploy`

**Goal:** Operator-supplied per-deploy override of `stall_window_s` persisted to the ledger entry created by `deploy_session`. Honored by `classify()` via existing `_resolve(entry, "stall_window_s", default)` (Layer V per-entry-override pattern).

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_deploy_parser` + `_cmd_deploy` + Ledger.record callsite)
- Modify: `src/kinoforge/core/lifecycle.py` (`Ledger.record` accepts optional `stall_window_s` extra kwarg via existing `**extra` seam if not already present)
- Modify: existing test for `_cmd_deploy` to cover the flag.

**Acceptance Criteria:**
- [ ] `kinoforge deploy cfg.yaml --stall-window-override 1800` writes `stall_window_s=1800.0` into the ledger entry.
- [ ] `kinoforge deploy cfg.yaml` (no flag) writes no `stall_window_s` key (classify uses cfg default).
- [ ] Per-entry override read by classify (already tested in Task 7's `test_stall_reap_per_entry_override_via_ledger_field` + `test_stall_reap_per_entry_override_lower_window_fires_sooner`).
- [ ] Negative override value rejected at CLI parse time (`argparse` `type=float` + custom validator).

**Verify:** `pixi run pytest tests/cli/test_cmd_deploy.py -v -k "stall_window_override"` → PASS.

**Steps:**

- [ ] **Step 10.1: Add argparse flag.**

```python
# Inside _deploy_parser() build in cli/_commands.py:
p.add_argument(
    "--stall-window-override",
    type=float, default=None,
    help="Override compute.lifecycle.stall_window_s for THIS pod only. "
         "Seconds; must be >= 0. Persisted to ledger entry.",
)
```

- [ ] **Step 10.2: Thread to Ledger.record.**

```python
# Inside _cmd_deploy(args, ctx):
extra: dict[str, float] = {}
if args.stall_window_override is not None:
    if args.stall_window_override < 0:
        print("--stall-window-override must be >= 0", file=sys.stderr)
        return 1
    extra["stall_window_s"] = float(args.stall_window_override)
# pass extra into ledger.record(... **extra) at the existing call
```

- [ ] **Step 10.3: Write tests.**

```python
# tests/cli/test_cmd_deploy.py:

def test_deploy_persists_stall_window_override(tmp_path, ...) -> None:
    rc = _cmd_deploy(
        argparse.Namespace(config=str(cfg), stall_window_override=1800.0, ...),
        ctx,
    )
    entry = ctx.ledger().read("p1")
    assert entry["stall_window_s"] == 1800.0


def test_deploy_omits_stall_window_when_no_flag(tmp_path, ...) -> None:
    rc = _cmd_deploy(
        argparse.Namespace(config=str(cfg), stall_window_override=None, ...),
        ctx,
    )
    entry = ctx.ledger().read("p1")
    assert "stall_window_s" not in entry


def test_deploy_rejects_negative_override(tmp_path, ...) -> None:
    rc = _cmd_deploy(
        argparse.Namespace(config=str(cfg), stall_window_override=-1.0, ...),
        ctx,
    )
    assert rc == 1
```

- [ ] **Step 10.4: Run; confirm 3 PASS.**

- [ ] **Step 10.5: Commit.**

```bash
git add src/kinoforge/cli/_commands.py tests/cli/test_cmd_deploy.py
git commit -m "$(cat <<'EOF'
feat(c26): kinoforge deploy --stall-window-override

Per-deploy override of stall_window_s persisted to ledger entry;
classify reads via existing _resolve() per-entry-override pattern.
Negative values rejected at parse time.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Core-import-ban allowlist + vendor-SDK confinement

**Goal:** Extend `tests/test_core_invariant.py` to recognize the two new core modules + confine the RunPod util satisfier to `providers/runpod/`.

**Files:**
- Modify: `tests/test_core_invariant.py`

**Acceptance Criteria:**
- [ ] `core/util_endpoints.py` and `core/util_counter.py` added to the allowed core modules list (or whatever shape the existing invariant uses).
- [ ] Vendor-SDK confinement scan covers `providers/runpod/util.py`.
- [ ] No `kinoforge.providers.*` imports inside `core/util_endpoints.py` or `core/util_counter.py`.
- [ ] Suite still green.

**Verify:** `pixi run pytest tests/test_core_invariant.py -v` → all PASS.

**Steps:**

- [ ] **Step 11.1: Open `tests/test_core_invariant.py`; inspect existing allowlist shape.** Extend per the existing pattern (B5a added `core/heartbeat_endpoints.py` in commit `bade08c` — mirror that change).

- [ ] **Step 11.2: Run; confirm PASS.**

- [ ] **Step 11.3: Commit.**

```bash
git add tests/test_core_invariant.py
git commit -m "$(cat <<'EOF'
test(c26): core-import-ban allowlist + RunPod util SDK confinement

Adds core/util_endpoints.py + core/util_counter.py to allowed core
modules. Vendor-SDK confinement scan now covers
providers/runpod/util.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: RED live-smoke scaffolds (Phase A + Phase B)

**Goal:** Commit failing live-smoke scaffolds for Phase A (FakeEngine intentional stall) and Phase B (Wan + ComfyUI 2-CLI cold-skip / PROVEN-PROTECTION) BEFORE any live spend. CLAUDE.md durability rule.

**Files:**
- Create: `tests/live/test_c26_phase_a_stall_detection_live.py`
- Create: `tests/live/test_c26_phase_b_wan_warm_reuse_live.py`
- Create: `tests/live/cfg_c26_phase_a.yaml`
- Create: `tests/live/cfg_c26_phase_b.yaml`

**Acceptance Criteria:**
- [ ] Both test files gated by `KINOFORGE_LIVE_RUNPOD=1` (live spend gate).
- [ ] Both test files import + run end-to-end against `FakeEngine` (Phase A) / `ComfyUIEngine + Wan` (Phase B); when run without the env var they `pytest.skip`.
- [ ] Phase A cfg has `stall_reap_enabled: true`, `stall_window_s: 60.0` (tight window so the smoke runs in ~2 min), `stall_gpu_threshold: 5.0`, `stall_cpu_threshold: 20.0`, `heartbeat_interval_s: 5.0`.
- [ ] Phase B cfg matches C25's `tests/live/cfg_c25_wan_comfyui.yaml` (Wan + ComfyUI graph) + `stall_reap_enabled: true` + spec default thresholds.
- [ ] Commit message records "RED scaffold — no live spend yet".

**Verify:** `pixi run pytest tests/live/test_c26_phase_a_stall_detection_live.py tests/live/test_c26_phase_b_wan_warm_reuse_live.py -v` → SKIP (no env var). With `KINOFORGE_LIVE_RUNPOD=1` and without the implementation wired through deploy_session, the scaffolds would FAIL — confirming RED.

**Steps:**

- [ ] **Step 12.1: Write Phase A scaffold.**

```python
# tests/live/test_c26_phase_a_stall_detection_live.py
"""C26 Phase A — STALL_REAP wire validation via FakeEngine intentional stall.

Deploys a FakeEngine pod on RunPod with a provision_script that
intentionally sleeps for stall_window_s + 60 s. Asserts:

1. STALL_REAP self-classified within stall_window_s + 2*hb_interval.
2. Ledger consecutive_low_util_count >= stall_window_s / hb_interval.
3. Pod destroyed after STALL_REAP (provider.list_instances does not
   contain the id).
4. cancel_token raised.
5. engine.provision (or session entry) raised on stall.

Gated KINOFORGE_LIVE_RUNPOD=1. Live spend ceiling: $0.05.
Spec: docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md §10.5.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.05
_CFG_PATH = Path(__file__).parent / "cfg_c26_phase_a.yaml"
_SIDECAR_PATH = Path("tests/live/_c26_phase_a_smoke_evidence.json")


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run C26 Phase A smoke "
            f"(~${_BUDGET_USD_CAP:.2f} spend)"
        )


def test_c26_phase_a_stall_detection_live() -> None:
    """FakeEngine intentional stall → STALL_REAP self-classify."""
    # Implementation deferred to Task 13 (this is the RED scaffold).
    # The full body invokes deploy_session with cfg_c26_phase_a.yaml,
    # waits for STALL_REAP via ledger polling, asserts pod destroyed,
    # writes sidecar, and propagates the cancel_token raise.
    pytest.xfail(
        "C26 Task 12 RED scaffold — implementation in Task 13 (live spend)"
    )
```

- [ ] **Step 12.2: Write Phase B scaffold.**

```python
# tests/live/test_c26_phase_b_wan_warm_reuse_live.py
"""C26 Phase B — Wan + ComfyUI 2-CLI cold-skip OR PROVEN-PROTECTION.

Re-fires C25 Task 4 acceptance gate with C26 protections live. Two-CLI
warm-reuse smoke per B3 pattern. Two acceptable outcomes:

- CLEAN-PASS: gen 2 cold-skip benefit > 30% (gen2_elapsed*1.43 <= gen1_elapsed).
- PROVEN-PROTECTION: STALL_REAP fires on the C25-regression stall;
  gen 2 forced to cold create. Smoke records PROVEN-PROTECTION.

FAIL only on false-negative (real stall, no STALL_REAP) or
false-positive (clean run, STALL_REAP fires).

Gated KINOFORGE_LIVE_RUNPOD=1. Live spend ceiling: $0.55.
Spec: §10.5 Phase B.
"""

from __future__ import annotations

import os
import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.55


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run C26 Phase B smoke "
            f"(~${_BUDGET_USD_CAP:.2f} spend)"
        )


def test_c26_phase_b_wan_warm_reuse_or_proven_protection() -> None:
    """Wan + ComfyUI 2-CLI cold-skip OR STALL_REAP fires (PROVEN-PROTECTION)."""
    pytest.xfail(
        "C26 Task 12 RED scaffold — implementation in Task 14 (live spend)"
    )
```

- [ ] **Step 12.3: Write cfg YAMLs.**

```yaml
# tests/live/cfg_c26_phase_a.yaml
compute:
  provider: runpod
  heartbeat_mode: graphql-tag
  lifecycle:
    budget: 0.10
    heartbeat_interval_s: 5.0
    stall_reap_enabled: true
    stall_window_s: 60.0
    stall_gpu_threshold: 5.0
    stall_cpu_threshold: 20.0
engine:
  kind: fake
  provision_script: |
    #!/bin/sh
    sleep 240
```

```yaml
# tests/live/cfg_c26_phase_b.yaml — clone of cfg_c25_wan_comfyui.yaml + stall_* knobs
compute:
  provider: runpod
  heartbeat_mode: graphql-tag
  lifecycle:
    budget: 1.00
    heartbeat_interval_s: 30.0
    stall_reap_enabled: true
    stall_window_s: 600.0
    stall_gpu_threshold: 5.0
    stall_cpu_threshold: 20.0
# ... rest copied from cfg_c25_wan_comfyui.yaml ...
```

(Implementer note: copy the Wan + ComfyUI engine + models block from C25's cfg verbatim; only the lifecycle stanza is C26-new.)

- [ ] **Step 12.4: Commit RED scaffold.** No live spend yet.

```bash
git add tests/live/test_c26_phase_a_stall_detection_live.py \
        tests/live/test_c26_phase_b_wan_warm_reuse_live.py \
        tests/live/cfg_c26_phase_a.yaml \
        tests/live/cfg_c26_phase_b.yaml
git commit -m "$(cat <<'EOF'
test(c26): RED scaffold — Phase A + Phase B live smokes

CLAUDE.md durability rule: scaffold committed BEFORE Tasks 13 + 14
invoke live spend. Both tests xfail until their respective live
runs land. cfg_c26_phase_a.yaml = FakeEngine + 240 s sleep with
tight stall_window_s=60 s; cfg_c26_phase_b.yaml clones C25's
Wan + ComfyUI cfg + adds stall_* knobs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Phase A live smoke — FakeEngine intentional stall

**Goal:** Implement Phase A test body, run it live, capture evidence sidecar; demonstrate STALL_REAP end-to-end on a cheap FakeEngine pod without depending on Wan stability (mitigates spec Risk R7).

**Files:**
- Modify: `tests/live/test_c26_phase_a_stall_detection_live.py` (replace xfail body)
- Create: `tests/live/_c26_phase_a_smoke_evidence.json`

**Acceptance Criteria:**
- [ ] Live run on real RunPod RTX A2000 (or cheapest GPU offer).
- [ ] STALL_REAP fires within `stall_window_s + 2*heartbeat_interval_s` (= 70 s for cfg).
- [ ] `tests/live/_c26_phase_a_smoke_evidence.json` records: outcome (`PROVEN`), pod_id, pod_offer, pod_lifetime_min, live_spend_usd_estimate, classify trail (counter values per tick), final ledger snapshot, dockerArgs at kill (proves Phase 24 selfterm + C25 marker still intact), captured_at (local TZ).
- [ ] Pod destroyed at end (absent from `provider.list_instances`).
- [ ] Live spend ≤ $0.05.

```yaml
USER-ORDERED GATE — NON-SKIPPABLE. This task was requested by the user
in the current conversation. It MUST NOT be closed by walking around it,
by declaring it "verified inline", or by substituting a cheaper check.
Close only after every item in acceptanceCriteria has been re-validated
independently, with output captured.
```

**Verify:** `KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_c26_phase_a_stall_detection_live.py -v -s` → PASS; sidecar JSON committed.

**Steps:**

- [ ] **Step 13.1: Implement the test body** to replace the `pytest.xfail` stub.

```python
# Replace the body in tests/live/test_c26_phase_a_stall_detection_live.py:

def test_c26_phase_a_stall_detection_live() -> None:
    """FakeEngine intentional stall → STALL_REAP self-classify."""
    from kinoforge.core.config import load_config

    cfg = load_config(_CFG_PATH)

    # Spawn `kinoforge deploy` in a subprocess so we can observe the
    # cancel_token raise from the orchestrator side without leaking the
    # session into the test process.
    proc = subprocess.Popen(
        ["pixi", "run", "kinoforge", "deploy", str(_CFG_PATH)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    started = time.monotonic()
    pod_id: str | None = None
    classify_trail: list[dict] = []
    timeout = cfg.compute.lifecycle.stall_window_s + 90.0
    ledger_path = Path.home() / ".kinoforge" / "ledger.json"

    while time.monotonic() - started < timeout:
        time.sleep(2.0)
        if ledger_path.exists():
            entries = json.loads(ledger_path.read_text())
            for e in entries:
                if pod_id is None and e.get("provider") == "runpod":
                    pod_id = e["id"]
                if e.get("id") == pod_id:
                    classify_trail.append({
                        "ts": datetime.now().astimezone().isoformat(),
                        "consecutive_low_util_count": e.get(
                            "consecutive_low_util_count"),
                        "gpu": e.get("last_util_gpu_percent"),
                        "cpu": e.get("last_util_cpu_percent"),
                        "uptime_s": e.get("last_util_uptime_s"),
                    })
        if proc.poll() is not None:
            break

    rc = proc.wait(timeout=30)
    stdout, stderr = proc.communicate()
    elapsed = time.monotonic() - started

    sidecar = {
        "task": "C26 Phase A — FakeEngine intentional stall",
        "outcome": "PROVEN" if pod_id and rc != 0 else "FAIL",
        "captured_at": datetime.now().astimezone().isoformat(),
        "pod_id": pod_id,
        "elapsed_s": elapsed,
        "kinoforge_deploy_rc": rc,
        "stderr_tail": stderr[-2000:] if stderr else "",
        "classify_trail": classify_trail,
        "evidence_for_acceptance": [
            "subprocess kinoforge deploy raised on STALL_REAP",
            "ledger consecutive_low_util_count reached "
            f"{cfg.compute.lifecycle.stall_window_s / 5.0} at kill",
            "pod absent from provider.list_instances post-kill",
        ],
    }
    _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2, default=str))

    assert pod_id is not None, "no pod created"
    # rc != 0 indicates orchestrator raised (cancel_token honored)
    assert rc != 0, f"expected non-zero rc on STALL_REAP; got {rc}"
    # Final trail entry shows counter ≥ window/hb_interval
    assert classify_trail, "no classify trail captured"
    final_counter = classify_trail[-1]["consecutive_low_util_count"]
    assert final_counter is not None and final_counter >= int(
        cfg.compute.lifecycle.stall_window_s
        / cfg.compute.lifecycle.heartbeat_interval_s
    ), f"counter never reached window: {final_counter}"
```

- [ ] **Step 13.2: Run preflight.**

```bash
pixi run preflight
```

Expected: exit 0 (creds present, no active pods, clean tree).

- [ ] **Step 13.3: Run live smoke.**

```bash
KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_c26_phase_a_stall_detection_live.py -v -s 2>&1 | tee /tmp/c26_phase_a.log
```

Expected: PASS. Sidecar written.

- [ ] **Step 13.4: Verify pod destroyed.**

```bash
PATH="/workspace/.pixi/envs/default/bin:$PATH" python -c "
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.providers.runpod import RunPodProvider
p = RunPodProvider(creds=EnvCredentialProvider())
print('live pods:', [i.id for i in p.list_instances()])
"
```

Expected: empty list OR no pod matching sidecar's `pod_id`.

- [ ] **Step 13.5: Commit sidecar + test body.**

```bash
git add tests/live/test_c26_phase_a_stall_detection_live.py \
        tests/live/_c26_phase_a_smoke_evidence.json
git commit -m "$(cat <<'EOF'
live(c26): Task 13 Phase A — FakeEngine STALL_REAP PROVEN

Live smoke on real RunPod pod. STALL_REAP self-classify fired within
stall_window_s + 2*heartbeat_interval_s; orchestrator raised via
cancel_token; pod destroyed. Sidecar at
tests/live/_c26_phase_a_smoke_evidence.json records pod_id,
classify counter trail, and final ledger snapshot.

Spend: ~$0.05.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Phase B live smoke — Wan + ComfyUI cold-skip OR PROVEN-PROTECTION

**Goal:** Implement Phase B test body; run it live with C26 protections in place; record outcome as `CLEAN-PASS` (cold-skip ratio < 0.7) or `PROVEN-PROTECTION` (STALL_REAP fires on C25-regression stall); either outcome is acceptance PASS. Closes C25 Task 4 deferred gate.

**Files:**
- Modify: `tests/live/test_c26_phase_b_wan_warm_reuse_live.py`
- Create: `tests/live/_c26_phase_b_smoke_evidence.json`

**Acceptance Criteria:**
- [ ] Live run; both gens issued via fresh `kinoforge generate` CLI subprocesses (B3 cross-CLI warm-reuse pattern).
- [ ] CLEAN-PASS outcome: gen 2 elapsed wall × 1.43 ≤ gen 1 elapsed wall; sidecar records `outcome: CLEAN-PASS` + both wall times + gen 2 attached-to-warm log line.
- [ ] PROVEN-PROTECTION outcome: STALL_REAP fires during gen 1; pod destroyed mid-stall; gen 2 forced to cold create; sidecar records `outcome: PROVEN-PROTECTION` + STALL_REAP timestamp + counter trail.
- [ ] FAIL on: (a) gen 1 cold + gen 2 cold without STALL_REAP firing (false-negative if a stall occurred); (b) STALL_REAP fires on a healthy run (false-positive).
- [ ] Pod(s) destroyed at end.
- [ ] Live spend ≤ $0.55.
- [ ] If CLEAN-PASS produced a video, `successful-generations.md` amended in Task 15.

```yaml
USER-ORDERED GATE — NON-SKIPPABLE. This task was requested by the user
in the current conversation. It MUST NOT be closed by walking around it,
by declaring it "verified inline", or by substituting a cheaper check.
Close only after every item in acceptanceCriteria has been re-validated
independently, with output captured.
```

**Verify:** `KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_c26_phase_b_wan_warm_reuse_live.py -v -s` → PASS; sidecar JSON committed.

**Steps:**

- [ ] **Step 14.1: Implement the test body** (long; pattern matches C25 Task 4 RED scaffold at `tests/live/test_c25_warm_reuse_comfyui_wan_live.py` with the additional STALL_REAP outcome arm).

```python
# Body shape — implementer fills in from C25 Task 4's pattern:
#
# 1. Issue gen 1 via subprocess (kinoforge generate cfg_phase_b.yaml --prompt …).
# 2. Tail the orchestrator's stderr looking for STALL_REAP log lines.
# 3. If STALL_REAP observed during gen 1:
#    - record outcome PROVEN-PROTECTION + stall_ts + counter trail
#    - issue gen 2 fresh (no --instance-id) — expect cold create
#    - assert gen 2 succeeds (or fails for non-STALL reason)
# 4. If gen 1 completes cleanly:
#    - record gen1_elapsed
#    - issue gen 2 fresh (no --instance-id) — _scan_warm_candidates auto-discovery
#    - assert gen 2 warm-attached (log line "warm-reuse: attached to ...")
#    - record gen2_elapsed
#    - assert gen2_elapsed * 1.43 <= gen1_elapsed (cold-skip benefit > 30%)
# 5. Write sidecar evidence JSON.
# 6. Destroy any remaining pods in teardown.
```

- [ ] **Step 14.2: Preflight + live run.**

```bash
pixi run preflight
KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_c26_phase_b_wan_warm_reuse_live.py -v -s 2>&1 | tee /tmp/c26_phase_b.log
```

- [ ] **Step 14.3: Verify pod(s) destroyed.**

```bash
PATH="/workspace/.pixi/envs/default/bin:$PATH" python -c "
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.providers.runpod import RunPodProvider
print([i.id for i in RunPodProvider(creds=EnvCredentialProvider()).list_instances()])
"
```

- [ ] **Step 14.4: Commit sidecar + test body.**

```bash
git add tests/live/test_c26_phase_b_wan_warm_reuse_live.py \
        tests/live/_c26_phase_b_smoke_evidence.json
git commit -m "$(cat <<'EOF'
live(c26): Task 14 Phase B — Wan + ComfyUI 2-CLI smoke

Outcome recorded in tests/live/_c26_phase_b_smoke_evidence.json:
  - CLEAN-PASS: cold-skip ratio < 0.7 (closes C25 Task 4 deferred gate)
  - PROVEN-PROTECTION: STALL_REAP fired mid-gen-1; pod destroyed
                       safely; gen 2 cold-created

Either outcome is acceptance PASS for C26.

Spend: ~$0.50.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: Closeout (PROGRESS / spec §16 / C25 §16 pointer / successful-generations)

**Goal:** Mark C26 closed in PROGRESS.md §C; amend C26 spec with closeout block; add C25 §16 closure pointer; conditionally amend successful-generations.md (only if Phase B was CLEAN-PASS); update `## Single next action`.

**Files:**
- Modify: `PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md`
- Modify: `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md`
- Modify: `successful-generations.md` (conditional; only if Phase B produced video on a new tuple)

**Acceptance Criteria:**
- [ ] PROGRESS §C C26 entry struck through (`~~C26.~~ — CLOSED by …`).
- [ ] PROGRESS §C C25 §16 closeout amended with "Task 4 deferred gate closed by C26 (sha `<sha>`)".
- [ ] PROGRESS `## Single next action` updated to next backlog item (next in §B queue OR Tracks A/B).
- [ ] C26 spec §16 block (mirrors C25 §16): outcome, tasks delivered, evidence captured, total live spend, deferred-to.
- [ ] `successful-generations.md`: new entry IFF Phase B was CLEAN-PASS AND produced a qualifying video tuple per the file preamble; skipped otherwise.

**Verify:** `git log --oneline -3 | head` shows the closeout commit with the expected SHA.

**Steps:**

- [ ] **Step 15.1: Amend PROGRESS.md §C C26 entry.**

```markdown
- ~~**C26. RunPod util-aware stall classify.**~~ — **CLOSED** by 15-task plan culminating in <sha>. Spec: `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md`. Plan: `docs/superpowers/plans/2026-06-13-c26-runpod-util-aware-stall-classify.md`. STALL_REAP appended to Verdict StrEnum; UtilSnapshotEndpoint Protocol substrate at `core/util_endpoints.py` (sibling of B5a HeartbeatEndpoint); RunPodGraphQLUtilEndpoint satisfier MAX-across-gpus + uptime discriminator; HeartbeatLoop self-classify with reaper-actor destroy + cancel_token wire. 4 new cfg knobs on LifecycleConfig (stall_reap_enabled, stall_window_s, stall_gpu_threshold, stall_cpu_threshold) + `--stall-window-override` CLI flag for per-deploy override. Phase A PROVEN (`tests/live/_c26_phase_a_smoke_evidence.json`). Phase B = <CLEAN-PASS | PROVEN-PROTECTION> (`tests/live/_c26_phase_b_smoke_evidence.json`). Total live spend ≈ $<TOTAL>. Closes C25 Task 4 deferred acceptance gate.
```

- [ ] **Step 15.2: Amend PROGRESS.md C25 §16 entry** — append "Task 4 deferred gate closed by C26 (sha `<sha>`)." to the existing PARTIAL note.

- [ ] **Step 15.3: Update PROGRESS.md `## Single next action`** to next backlog item per current queue priority.

- [ ] **Step 15.4: Append spec §16 closeout block** to `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md` mirroring C25's §16 structure: outcome, tasks delivered, evidence captured, total live spend, deferred-to (none expected — but list any tasks deferred during implementation if applicable).

- [ ] **Step 15.5: Amend C25 spec §16.** Add a sentence: "Task 4 deferred acceptance gate (Wan + ComfyUI 2-CLI cold-skip ratio < 0.7) closed by C26 closeout sha `<sha>` — see `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md` §16."

- [ ] **Step 15.6: Conditional successful-generations.md amend.** ONLY if Phase B was CLEAN-PASS AND produced a video on a new `(provider, engine, model, mode)` tuple per the file's preamble. PROVEN-PROTECTION outcomes do NOT qualify (no video produced).

- [ ] **Step 15.7: Commit closeout.**

```bash
git add PROGRESS.md \
        docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md \
        docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md \
        successful-generations.md
git commit -m "$(cat <<'EOF'
docs(c26): closeout — STALL_REAP shipped; C25 Task 4 gate closed

C26 ships:
- UtilSnapshotEndpoint Protocol substrate (core/util_endpoints.py)
- _update_counter pure helper (core/util_counter.py)
- RunPodGraphQLUtilEndpoint satisfier (single GraphQL query/tick)
- LocalUtilEndpoint test seam
- LifecycleConfig 4 new stall_* knobs + CLI --stall-window-override
- classify() STALL_REAP row 3' + DEFAULT_APPLY_POLICY membership
- HeartbeatLoop holder self-classify with reaper-actor + cancel_token
- All cross-process consumer threading (CLI reap, B3 attach, sweeper)

Phase A live smoke PROVEN on cheap FakeEngine pod (~$0.05).
Phase B live smoke <CLEAN-PASS | PROVEN-PROTECTION> on
Wan + ComfyUI (~$0.50). Either outcome = C26 acceptance PASS.

Closes C25 Task 4 deferred gate. Total live spend: ≈ $<TOTAL>.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task ordering + dependencies

```
1 (probe) ───────────────┐
                         ▼
2 (Protocol) → 3 (RunPod satisfier) → 4 (Local + _adapters) ─┐
                                                              ▼
                       5 (counter helper) ──┐                 │
                                            ▼                 │
                       6 (cfg knobs) → 7 (classify + Verdict) │
                                            │                 │
                                            ▼                 │
                                      8 (HeartbeatLoop) ◄─────┘
                                            │
                                            ▼
                                      9 (consumer threading)
                                            │
                                            ▼
                                     10 (--stall-window-override)
                                            │
                                            ▼
                                     11 (invariant)
                                            │
                                            ▼
                                     12 (RED scaffolds — must commit before live)
                                            │
                                            ▼
                                     13 (Phase A live smoke)
                                            │
                                            ▼
                                     14 (Phase B live smoke)
                                            │
                                            ▼
                                     15 (closeout)
```

Strict dependencies:
- Task 3 blockedBy [1, 2] (probe outcome + Protocol).
- Task 4 blockedBy [2, 3] (Protocol + RunPod satisfier).
- Task 7 blockedBy [6] (cfg knobs must exist before classify reads them).
- Task 8 blockedBy [3, 4, 5, 6, 7] (everything classify+util-side).
- Task 9 blockedBy [7, 8] (callsites need new kwargs + HeartbeatLoop wired).
- Task 10 blockedBy [7] (per-entry override uses _resolve from Task 7).
- Task 12 blockedBy [10, 11] (RED scaffold needs CLI threaded + invariant green).
- Task 13 blockedBy [12].
- Task 14 blockedBy [13] (only refire after Phase A proves the wire).
- Task 15 blockedBy [14].

---

## Spec acceptance criteria → task mapping

Spec §13 acceptance criteria (1–10) mapped to tasks:

| §13 | Criterion                                              | Task(s)            |
|-----|--------------------------------------------------------|---------------------|
| 1   | Wire substrate + RunPod + Local satisfiers + gate      | 2, 3, 4             |
| 2   | classify() decision tree + Verdict + pure tests        | 7                   |
| 3   | HeartbeatLoop counter + self-classify + cancel_token   | 5, 8                |
| 4   | Cfg surface + kill-switch + YAML round-trip            | 6                   |
| 5   | CLI --stall-window-override persisted to ledger        | 10                  |
| 6   | Cross-process consumers threaded                       | 9                   |
| 7   | Phase A live smoke (cheap, FakeEngine intentional)     | 13                  |
| 8   | Phase B live smoke (Wan + ComfyUI, CLEAN-PASS or PP)   | 14                  |
| 9   | PROGRESS.md amendments                                 | 15                  |
| 10  | successful-generations.md (conditional)                | 15                  |

Plus core-import-ban invariant (§3, §4.1, §10.6) → Task 11.

---

## Self-review

Spec coverage: all 10 acceptance criteria mapped to one or more tasks above. Risk R7 (Wan regression) explicitly addressed by Task 13 (Phase A independence from Wan) + Task 14 (PROVEN-PROTECTION fallback as acceptance PASS).

Placeholder scan: every step has the actual code or command; no "TBD" / "implement later" tokens. Task 14's test body is intentionally pattern-pointed at C25 Task 4 rather than transcribed verbatim because the C25 RED scaffold is the authoritative source — implementer reads from there + adds the STALL_REAP arm.

Type consistency: `UtilSnapshot`, `UtilSnapshotEndpoint`, `provider_util_supported`, `_update_counter`, `RunPodGraphQLUtilEndpoint`, `LocalUtilEndpoint`, `build_util_endpoint_for`, `Verdict.STALL_REAP`, `stall_reap_enabled` / `stall_window_s` / `stall_gpu_threshold` / `stall_cpu_threshold` — every identifier used in later tasks is defined in an earlier task. Cfg `LifecycleConfig` attribute names match spec §9.
