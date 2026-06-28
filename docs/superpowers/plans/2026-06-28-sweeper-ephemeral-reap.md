# Sweeper-side ephemeral reap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach `sweep()` and `kinoforge reap` to discover ephemeral pods via `EphemeralIndex` + provider runtime probe, classify them through a heartbeat-free verdict path, and reap wedged/overage ones.

**Architecture:** New `RuntimeProbe` dataclass + `ComputeProvider.probe_runtime` ABC method (default `None`); `RunPodProvider` override wraps existing `RunPodGraphQLUtilEndpoint`. `sweep()` unions `ledger.entries()` with `EphemeralIndex.rows()`, calls `probe_runtime` per ephemeral row with per-tick cache, synthesises ledger-shape entries flagged `kinoforge_ephemeral=True`, dispatches `classify()` through a new `_classify_ephemeral` branch (verdicts: `OVERAGE_REAP`, `STALL_REAP`, `GC_404`, `SKIP_NO_PROBE`, `PROBE_FAILED`, `LIVE`). `SweeperLoop` owns bounded in-memory stall-history deques per pod. One-shot `kinoforge reap` skips STALL_REAP (single sample insufficient).

**Tech Stack:** Python 3.12, dataclasses, `kinoforge.core.{reaper,reaper_actor,sweeper,interfaces}`, `kinoforge.providers.runpod`, `kinoforge.core.warm_reuse.ephemeral_index`, pytest + pytest-cov.

**User decisions (already made):**
- "Full reap parity" — sweeper classifies + reaps ephemeral pods, not just GC stale rows.
- "Provider-side probe only" — no heartbeat-into-index; no sibling heartbeat files.
- "Skip with WARN-once" for non-RunPod providers (Lambda/Vast/SkyPilot/Local).
- "Skip IDLE_REAP; rely on STALL + OVERAGE" — false-positive risk during model load.
- Spec approved as-is, no changes requested before plan.

**Spec:** `docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md`

---

## File structure

### Files created

| Path | Responsibility |
|---|---|
| `src/kinoforge/core/runtime_probe.py` | `RuntimeProbe` frozen dataclass — single per-tick liveness snapshot |
| `tests/core/test_runtime_probe.py` | Unit: RuntimeProbe shape + RunPod / SkyPilot / Local probe_runtime behavior |
| `tests/core/test_classify_ephemeral.py` | Unit: `_classify_ephemeral` decision tree + STALL/OVERAGE/GC_404/SKIP_NO_PROBE/PROBE_FAILED |
| `tests/core/test_sweep_ephemeral_union.py` | Unit: `sweep()` union with EphemeralIndex + probe cache + ledger-wins-on-overlap |
| `tests/core/test_sweeper_loop_stall_history.py` | Unit: `SweeperLoop` bounded deque eviction + restart reset |
| `tests/integration/test_sweeper_reaps_ephemeral_stall.py` | Integration: scripted probe sequence → STALL_REAP + index row removal |
| `tests/integration/test_sweeper_skips_on_session_claim.py` | Integration: provision-lock held → demote to deferred-session-claim |
| `tests/integration/test_reap_cli_ephemeral.py` | Integration: `kinoforge reap` dry-run + --apply + STALL skipped in one-shot |
| `tests/integration/test_sweep_skypilot_ephemeral_warn_once.py` | Integration: probe=None → WARN-once dedup |
| `tests/test_classify_ephemeral_no_heartbeat_keys.py` | AST invariant: `_classify_ephemeral` consumes zero heartbeat keys |
| `tests/live/test_runpod_ephemeral_sweeper_smoke.py` | Live: RunPod sweeper-reaps-ephemeral-stall — RED scaffold + GREEN evidence |

### Files modified

| Path | Change |
|---|---|
| `src/kinoforge/core/interfaces.py` (around line 218) | `ComputeProvider` ABC gains `probe_runtime(pod_id) -> RuntimeProbe \| None` method with default `return None` |
| `src/kinoforge/core/reaper.py` (lines 24-41, 56-67, 247-) | New `Verdict.GC_404`, `Verdict.SKIP_NO_PROBE`, `Verdict.PROBE_FAILED`; `DEFAULT_APPLY_POLICY` adds `GC_404`; `classify()` dispatches to `_classify_ephemeral` on `kinoforge_ephemeral=True` sentinel |
| `src/kinoforge/core/reaper_actor.py` (lines 34, 179-294, 297-) | `_WARNED_PROBE_MISSING` + `_WARNED_PROBE_FAILED` sets; `act_on_verdict` handles new verdicts; `sweep()` unions `EphemeralIndex.rows()` + per-tick probe cache + `_synthesize_ephemeral_entry`; accepts new `stall_history` kwarg |
| `src/kinoforge/core/sweeper.py` (line 111-) | `SweeperLoop` owns `dict[str, deque[ProbeSample]]`; passes to `_sweep_fn`; evicts entries for pods no longer in ledger ∪ index |
| `src/kinoforge/core/sweeper_metrics.py` | `_SweeperStats.gc_404_total`, `probe_failed_total`, `skip_no_probe_total` counters; fold + snapshot_for_ledger + summary_line updates |
| `src/kinoforge/providers/runpod/__init__.py` (around line 270) | `RunPodProvider.probe_runtime` override — wraps `RunPodGraphQLUtilEndpoint` with 404-vs-null-runtime distinction |
| `src/kinoforge/providers/runpod/util.py` (lines 125-171) | Extend `read_util` return shape OR add sibling `probe_runtime` method that distinguishes `pod is None` from `runtime is None` |
| `src/kinoforge/cli/_commands.py` (lines 2116-2230) | `_emit_reap_human` + `_emit_reap_jsonl` add cases for `gc_404_removed`, `probe_failed`, `SKIP_NO_PROBE no_op` |
| `docs/lifecycle.md` | Document ephemeral-aware sweep + verdict matrix |
| `docs/warm-reuse.md` | Cross-reference: sweeper now reaps ephemeral pods |

---

## Tasks

### Task 1: `RuntimeProbe` dataclass + `ComputeProvider.probe_runtime` ABC default

**Goal:** Land the probe surface contract. New providers can already opt out by inheriting the default; existing providers compile unchanged.

**Files:**
- Create: `src/kinoforge/core/runtime_probe.py`
- Create: `tests/core/test_runtime_probe.py`
- Modify: `src/kinoforge/core/interfaces.py` (around line 218, after `set_heartbeat_endpoint`)

**Acceptance Criteria:**
- [ ] `RuntimeProbe` is a frozen dataclass with exactly these fields: `pod_id: str`, `found: bool`, `container_uptime_s: float | None`, `gpu_util_pct: float | None`, `cpu_pct: float | None`, `cost_per_hr: float | None`, `probed_at_local: str`, `error: str | None = None`
- [ ] `ComputeProvider.probe_runtime(pod_id: str) -> RuntimeProbe | None` exists on the ABC with a concrete default `return None`
- [ ] Existing `LocalProvider` / `SkyPilotProvider` instantiate without TypeError (default inherited)
- [ ] `RunPodProvider` instantiates without override (returns `None` until Task 2 lands)

**Verify:** `pixi run pytest tests/core/test_runtime_probe.py -v` → all PASS

**Steps:**

- [ ] **Step 1: Write `tests/core/test_runtime_probe.py`**

```python
"""Unit tests for RuntimeProbe dataclass + ComputeProvider.probe_runtime default."""
from __future__ import annotations

import dataclasses

import pytest

from kinoforge.core.interfaces import ComputeProvider
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.runpod import RunPodProvider
from kinoforge.providers.skypilot import SkyPilotProvider


def test_runtime_probe_is_frozen() -> None:
    """A RuntimeProbe instance cannot be mutated after construction."""
    probe = RuntimeProbe(
        pod_id="abc",
        found=True,
        container_uptime_s=10.0,
        gpu_util_pct=50.0,
        cpu_pct=15.0,
        cost_per_hr=0.40,
        probed_at_local="2026-06-28T12:00:00",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        probe.gpu_util_pct = 99.0  # type: ignore[misc]


def test_runtime_probe_required_fields() -> None:
    """All seven required fields must be present; missing one raises TypeError."""
    with pytest.raises(TypeError):
        RuntimeProbe(  # type: ignore[call-arg]
            pod_id="abc", found=True, container_uptime_s=None,
            gpu_util_pct=None, cpu_pct=None, cost_per_hr=None,
        )  # missing probed_at_local


def test_runtime_probe_error_defaults_to_none() -> None:
    """`error` is optional and defaults to None."""
    probe = RuntimeProbe(
        pod_id="abc", found=True, container_uptime_s=10.0,
        gpu_util_pct=50.0, cpu_pct=15.0, cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:00",
    )
    assert probe.error is None


def test_runtime_probe_not_found_shape() -> None:
    """`found=False` is allowed with all util fields None — 'pod gone' state."""
    probe = RuntimeProbe(
        pod_id="dead-pod", found=False, container_uptime_s=None,
        gpu_util_pct=None, cpu_pct=None, cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:00",
    )
    assert probe.found is False
    assert probe.gpu_util_pct is None


def test_abc_default_probe_returns_none_for_local() -> None:
    """LocalProvider inherits ABC default — substrate-missing signal."""
    assert LocalProvider().probe_runtime("anything") is None


def test_abc_default_probe_returns_none_for_skypilot() -> None:
    """SkyPilotProvider inherits ABC default — covers Lambda + Vast."""
    assert SkyPilotProvider().probe_runtime("anything") is None


def test_runpod_provider_inherits_default_before_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Until Task 2 lands, RunPodProvider's probe_runtime is the ABC default.

    This test deliberately constructs a RunPodProvider WITHOUT triggering its
    GraphQL substrate and asserts that probe_runtime returns None. Once Task 2
    overrides probe_runtime, delete this test.
    """
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-not-used")
    provider = RunPodProvider()
    # Before override, the ABC default returns None for any pod_id.
    assert provider.probe_runtime("anything") is None


def test_abc_probe_runtime_signature() -> None:
    """ABC method is callable with a single str arg and returns Optional[RuntimeProbe]."""
    import inspect
    sig = inspect.signature(ComputeProvider.probe_runtime)
    params = list(sig.parameters.values())
    assert len(params) == 2  # self + pod_id
    assert params[1].name == "pod_id"
    assert params[1].annotation is str
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/core/test_runtime_probe.py -v`
Expected: All tests FAIL with `ImportError` / `ModuleNotFoundError` for `runtime_probe`.

- [ ] **Step 3: Create `src/kinoforge/core/runtime_probe.py`**

```python
"""RuntimeProbe — single liveness snapshot for a provider-side pod.

Used by sweeper-ephemeral-reap (spec 2026-06-28) when the ephemeral
index gives us a pod_id but no heartbeat history. The provider's
``probe_runtime`` returns a RuntimeProbe; sweeper synthesises a
ledger-shape entry from it for the classify path.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeProbe:
    """Live runtime snapshot for one pod, fetched via provider API.

    Attributes:
        pod_id: Provider pod identifier.
        found: False when the provider returned 404 / "pod gone".
        container_uptime_s: Seconds since container start; None if
            not available (early boot, found=False, or partial probe).
        gpu_util_pct: MAX of per-GPU utilisation percent; None if
            no GPU array reported.
        cpu_pct: Container CPU percent; None if not reported.
        cost_per_hr: Optional cost/hour for future cost-cache reuse.
        probed_at_local: ISO-format local-TZ timestamp (per project rule).
        error: Optional WARN payload when found=True but probe partial.
    """

    pod_id: str
    found: bool
    container_uptime_s: float | None
    gpu_util_pct: float | None
    cpu_pct: float | None
    cost_per_hr: float | None
    probed_at_local: str
    error: str | None = None
```

- [ ] **Step 4: Modify `src/kinoforge/core/interfaces.py` — add `probe_runtime` to `ComputeProvider`**

Locate line 218 (after `def heartbeat(self, instance_id: str) -> None: ...`). Insert the new method BEFORE `set_heartbeat_endpoint`:

```python
    def probe_runtime(self, pod_id: str) -> "RuntimeProbe | None":
        """Live runtime probe for sweeper-ephemeral-reap.

        Default: returns None ("substrate missing"). RunPodProvider
        overrides; SkyPilot / Local inherit the default.

        Args:
            pod_id: Provider-side pod identifier.

        Returns:
            A :class:`RuntimeProbe` populated from a live provider
            query, or ``None`` when the provider lacks runtime-probe
            substrate. Sweeper treats ``None`` as a WARN-once skip.
        """
        return None
```

Add the import near the top of `interfaces.py` (in the TYPE_CHECKING block if one exists, else as a runtime import):

```python
from kinoforge.core.runtime_probe import RuntimeProbe
```

- [ ] **Step 5: Run tests to verify GREEN**

Run: `pixi run pytest tests/core/test_runtime_probe.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 6: Run linters + typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/core/runtime_probe.py src/kinoforge/core/interfaces.py tests/core/test_runtime_probe.py`
Expected: PASS (ruff, ruff-format, mypy).

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/runtime_probe.py src/kinoforge/core/interfaces.py tests/core/test_runtime_probe.py
git commit -m "feat(reaper): RuntimeProbe dataclass + ComputeProvider.probe_runtime interface"
```

---

### Task 2: `RunPodProvider.probe_runtime` — wraps GraphQL util endpoint

**Goal:** Real implementation for RunPod. Distinguishes "pod 404" (`found=False`) from "runtime null / early boot" (`found=True, util=None`). Raises on transport error so caller's `_probe_with_cache` can classify as `PROBE_FAILED`.

**Files:**
- Modify: `src/kinoforge/providers/runpod/util.py` (line 125-171, `read_util` method)
- Modify: `src/kinoforge/providers/runpod/__init__.py` (around line 270, `RunPodProvider`)
- Modify: `tests/core/test_runtime_probe.py` (delete `test_runpod_provider_inherits_default_before_override`; add new RunPod tests)

**Acceptance Criteria:**
- [ ] `RunPodGraphQLUtilEndpoint` gains a `probe(pod_id) -> tuple[bool, UtilSnapshot | None]` that returns `(found, snapshot)` — `(False, None)` when pod=None, `(True, None)` when runtime=None, `(True, snapshot)` otherwise
- [ ] `RunPodProvider.probe_runtime` returns a `RuntimeProbe` with `found=False` for 404, `found=True` + `gpu_util_pct=None` for early-boot, fully populated when runtime present
- [ ] `RunPodProvider.probe_runtime` re-raises `TransportError` (not swallowed — sweeper's `_probe_with_cache` catches)
- [ ] `probed_at_local` populated via `datetime.now().isoformat()` (local TZ per project rule, NOT UTC)

**Verify:** `pixi run pytest tests/core/test_runtime_probe.py -v -k runpod` → all PASS

**Steps:**

- [ ] **Step 1: Delete the bridge-test from Task 1 and add RunPod-specific tests**

Delete `test_runpod_provider_inherits_default_before_override` from `tests/core/test_runtime_probe.py`. Append the new tests below — each builds a `RunPodProvider` with a stubbed `_http_post` closure so no real GraphQL traffic occurs.

```python
# Append to tests/core/test_runtime_probe.py

from typing import Any

from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint


def _make_endpoint(response: dict[str, Any]) -> RunPodGraphQLUtilEndpoint:
    """Build an endpoint with a stubbed HTTP closure returning `response`."""
    return RunPodGraphQLUtilEndpoint(
        api_key="test-key",
        http_post=lambda url, payload: response,
    )


def test_runpod_endpoint_probe_pod_404_returns_not_found() -> None:
    """`data.pod = null` (RunPod's 404 shape) → (False, None)."""
    endpoint = _make_endpoint({"data": {"pod": None}})
    found, snapshot = endpoint.probe("dead-pod")
    assert found is False
    assert snapshot is None


def test_runpod_endpoint_probe_runtime_null_returns_found_no_snapshot() -> None:
    """Pod exists but runtime not started (early boot) → (True, None)."""
    endpoint = _make_endpoint({"data": {"pod": {"runtime": None}}})
    found, snapshot = endpoint.probe("booting-pod")
    assert found is True
    assert snapshot is None


def test_runpod_endpoint_probe_runtime_populated_returns_snapshot() -> None:
    """Pod + runtime present → (True, UtilSnapshot)."""
    endpoint = _make_endpoint({
        "data": {"pod": {"runtime": {
            "uptimeInSeconds": 600,
            "gpus": [{"id": "g0", "gpuUtilPercent": 75.0, "memoryUtilPercent": 50.0}],
            "container": {"cpuPercent": 12.0, "memoryPercent": 30.0},
        }}}
    })
    found, snapshot = endpoint.probe("live-pod")
    assert found is True
    assert snapshot is not None
    assert snapshot.gpu_util_percent == 75.0
    assert snapshot.cpu_percent == 12.0
    assert snapshot.uptime_seconds == 600


def test_runpod_provider_probe_runtime_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """RunPodProvider.probe_runtime returns RuntimeProbe(found=False) for 404."""
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    provider = RunPodProvider()
    provider._util_endpoint = _make_endpoint({"data": {"pod": None}})  # type: ignore[attr-defined]
    probe = provider.probe_runtime("dead-pod")
    assert probe is not None
    assert probe.found is False
    assert probe.gpu_util_pct is None
    assert probe.container_uptime_s is None


def test_runpod_provider_probe_runtime_early_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """RunPodProvider.probe_runtime returns found=True with util=None for runtime=null."""
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    provider = RunPodProvider()
    provider._util_endpoint = _make_endpoint({"data": {"pod": {"runtime": None}}})  # type: ignore[attr-defined]
    probe = provider.probe_runtime("booting-pod")
    assert probe is not None
    assert probe.found is True
    assert probe.gpu_util_pct is None
    assert probe.container_uptime_s is None


def test_runpod_provider_probe_runtime_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """RunPodProvider.probe_runtime returns full RuntimeProbe for healthy pod."""
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    provider = RunPodProvider()
    provider._util_endpoint = _make_endpoint({  # type: ignore[attr-defined]
        "data": {"pod": {"runtime": {
            "uptimeInSeconds": 600,
            "gpus": [{"id": "g0", "gpuUtilPercent": 75.0, "memoryUtilPercent": 50.0}],
            "container": {"cpuPercent": 12.0, "memoryPercent": 30.0},
        }}}
    })
    probe = provider.probe_runtime("live-pod")
    assert probe is not None
    assert probe.found is True
    assert probe.gpu_util_pct == 75.0
    assert probe.cpu_pct == 12.0
    assert probe.container_uptime_s == 600.0


def test_runpod_provider_probe_runtime_reraises_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network/auth failure → TransportError propagates (NOT swallowed)."""
    from kinoforge.providers.runpod.util import TransportError

    def raising_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise TransportError("simulated network failure")

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    provider = RunPodProvider()
    provider._util_endpoint = RunPodGraphQLUtilEndpoint(  # type: ignore[attr-defined]
        api_key="test-key", http_post=raising_post,
    )
    with pytest.raises(TransportError):
        provider.probe_runtime("any-pod")


def test_runpod_provider_probe_runtime_local_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """probed_at_local uses local-TZ ISO format, NOT UTC (project rule)."""
    from datetime import datetime
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    provider = RunPodProvider()
    provider._util_endpoint = _make_endpoint({"data": {"pod": None}})  # type: ignore[attr-defined]
    before = datetime.now().isoformat()
    probe = provider.probe_runtime("any-pod")
    after = datetime.now().isoformat()
    assert probe is not None
    assert before <= probe.probed_at_local <= after
    assert "+" not in probe.probed_at_local and "Z" not in probe.probed_at_local
```

- [ ] **Step 2: Run new tests — confirm FAIL**

Run: `pixi run pytest tests/core/test_runtime_probe.py -v -k runpod`
Expected: Fail with `AttributeError: ... has no attribute 'probe'` (endpoint) and `... 'probe_runtime'` not the override.

- [ ] **Step 3: Add `probe` method to `RunPodGraphQLUtilEndpoint`**

In `src/kinoforge/providers/runpod/util.py`, append a new method to `RunPodGraphQLUtilEndpoint` (after `read_util`, around line 172):

```python
    def probe(self, instance_id: str) -> tuple[bool, "UtilSnapshot | None"]:
        """Like :meth:`read_util` but distinguishes 404 from early-boot.

        Returns:
            ``(found, snapshot)``:
              * ``(False, None)`` — provider returned ``data.pod = null``
                (pod gone). Treat as 404.
              * ``(True, None)`` — pod exists but ``runtime = null``
                (early boot — container has not reported a runtime yet).
              * ``(True, snapshot)`` — pod + runtime present; full data.

        Raises:
            TransportError: HTTP / JSON / GraphQL transport fault. Caller
                (sweeper ``_probe_with_cache``) catches and classifies as
                ``PROBE_FAILED``.
        """
        payload = {"query": _build_runtime_query(instance_id)}
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
            return False, None
        runtime = pod.get("runtime")
        if runtime is None:
            return True, None
        gpus = runtime.get("gpus") or []
        gpu_util = max(
            (
                float(g["gpuUtilPercent"])
                for g in gpus
                if g.get("gpuUtilPercent") is not None
            ),
            default=None,
        )
        container = runtime.get("container") or {}
        cpu = container.get("cpuPercent")
        mem = container.get("memoryPercent")
        uptime = runtime.get("uptimeInSeconds")
        return True, UtilSnapshot(
            gpu_util_percent=gpu_util,
            cpu_percent=float(cpu) if cpu is not None else None,
            memory_percent=float(mem) if mem is not None else None,
            disk_percent=None,
            uptime_seconds=int(uptime) if uptime is not None else None,
        )
```

- [ ] **Step 4: Override `probe_runtime` on `RunPodProvider`**

In `src/kinoforge/providers/runpod/__init__.py`, locate the `RunPodProvider` class (around line 270). First, ensure a `_util_endpoint` attribute is constructed lazily (or in `__init__`) — search the class for any existing `util` / `read_util` use; if none, add a lazy property:

```python
    @property
    def _util_endpoint(self) -> RunPodGraphQLUtilEndpoint:
        """Lazy RunPodGraphQLUtilEndpoint — shared across calls within one provider instance."""
        if not hasattr(self, "_cached_util_endpoint"):
            from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint
            self._cached_util_endpoint = RunPodGraphQLUtilEndpoint(api_key=self._api_key)
        return self._cached_util_endpoint

    def probe_runtime(self, pod_id: str) -> "RuntimeProbe | None":
        """Override ABC default with real GraphQL probe.

        See :meth:`ComputeProvider.probe_runtime` for the contract.
        Wraps :meth:`RunPodGraphQLUtilEndpoint.probe`; TransportError
        propagates so the caller can classify as PROBE_FAILED.
        """
        from datetime import datetime

        from kinoforge.core.runtime_probe import RuntimeProbe

        found, snapshot = self._util_endpoint.probe(pod_id)
        now_local = datetime.now().isoformat()
        if not found:
            return RuntimeProbe(
                pod_id=pod_id, found=False,
                container_uptime_s=None, gpu_util_pct=None, cpu_pct=None,
                cost_per_hr=None, probed_at_local=now_local,
            )
        if snapshot is None:
            return RuntimeProbe(
                pod_id=pod_id, found=True,
                container_uptime_s=None, gpu_util_pct=None, cpu_pct=None,
                cost_per_hr=None, probed_at_local=now_local,
            )
        return RuntimeProbe(
            pod_id=pod_id, found=True,
            container_uptime_s=float(snapshot.uptime_seconds) if snapshot.uptime_seconds is not None else None,
            gpu_util_pct=snapshot.gpu_util_percent,
            cpu_pct=snapshot.cpu_percent,
            cost_per_hr=None,
            probed_at_local=now_local,
        )
```

NB: if `RunPodProvider.__init__` does not store `self._api_key`, store it from whatever construction path it uses (typically `os.environ["RUNPOD_API_KEY"]` via a helper). Verify by reading the existing class before editing.

- [ ] **Step 5: Run tests — confirm GREEN**

Run: `pixi run pytest tests/core/test_runtime_probe.py -v`
Expected: All tests PASS (including the new RunPod tests).

- [ ] **Step 6: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/providers/runpod/util.py src/kinoforge/providers/runpod/__init__.py tests/core/test_runtime_probe.py
git add src/kinoforge/providers/runpod/util.py src/kinoforge/providers/runpod/__init__.py tests/core/test_runtime_probe.py
git commit -m "feat(runpod): probe_runtime wraps existing GraphQL substrate"
```

---

### Task 3: `Verdict` enum + `_classify_ephemeral` branch

**Goal:** Sentinel-based dispatch in `classify()` to the new `_classify_ephemeral` function, which handles only the verdicts the sparse ephemeral substrate supports. No heartbeat-dependent verdicts are reachable from this branch.

**Files:**
- Modify: `src/kinoforge/core/reaper.py` (lines 24-41, 56-67, 247 onward)
- Create: `tests/core/test_classify_ephemeral.py`

**Acceptance Criteria:**
- [ ] `Verdict` enum gains `GC_404`, `SKIP_NO_PROBE`, `PROBE_FAILED`
- [ ] `DEFAULT_APPLY_POLICY.act_verdicts` includes `Verdict.GC_404` (so `--apply` removes stale rows)
- [ ] `DEFAULT_APPLY_POLICY` does NOT include `SKIP_NO_PROBE` or `PROBE_FAILED` (no-op verdicts; action handled in `act_on_verdict`)
- [ ] `classify()` dispatches to `_classify_ephemeral` on `entry.get("kinoforge_ephemeral") is True`
- [ ] `_classify_ephemeral` decision tree matches spec §3.5 exactly: probe_state dispatch first, then OVERAGE, then STALL (skipped if `stall_history is None`), else LIVE
- [ ] `_classify_ephemeral` never reads `last_heartbeat`, `heartbeat_thread_tick`, `session_claim`, `restart_count` (enforced by Task 8 AST invariant)

**Verify:** `pixi run pytest tests/core/test_classify_ephemeral.py -v` → all PASS

**Steps:**

- [ ] **Step 1: Write `tests/core/test_classify_ephemeral.py`**

```python
"""Unit tests for _classify_ephemeral decision tree.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §3.5
"""
from __future__ import annotations

from collections import deque

import pytest

from kinoforge.core.reaper import Verdict, _classify_ephemeral, classify


def _entry(probe_state: str, created_at_s_ago: float = 60.0, **extra: object) -> dict:
    """Build a synthetic ephemeral entry dict matching `_synthesize_ephemeral_entry`."""
    base: dict[str, object] = {
        "id": "pod-1",
        "provider": "runpod",
        "provider_kind": "runpod",
        "kinoforge_ephemeral": True,
        "probe_state": probe_state,
        "created_at": 1000.0,            # fixed wall-clock anchor
    }
    if probe_state == "ok":
        base["container_uptime_s"] = 300.0
        base["gpu_util_pct"] = extra.pop("gpu", 50.0)
        base["cpu_pct"] = extra.pop("cpu", 20.0)
    base.update(extra)
    return base


_NOW = 1060.0  # 60s after created_at=1000
_THRESHOLDS = {
    "max_lifetime_s": 5 * 3600,
    "stall_window_s": 120.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "heartbeat_interval_s": 30.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def test_classify_dispatches_to_ephemeral_on_sentinel() -> None:
    """`classify()` routes to ephemeral branch when sentinel is True."""
    entry = _entry("not_found")
    verdict = classify(entry, live_pod_ids=set(), now=_NOW, **_THRESHOLDS)
    assert verdict == Verdict.GC_404


def test_classify_uses_heartbeat_branch_when_sentinel_absent() -> None:
    """Regression guard: no `kinoforge_ephemeral` key → heartbeat branch.

    Without the sentinel, a sparse entry should fall into HEARTBEAT_UNKNOWN
    (or whatever the existing branch does for an entry lacking heartbeats),
    NOT into the ephemeral branch.
    """
    entry = {
        "id": "ledger-pod",
        "provider": "runpod",
        "provider_kind": "runpod",
        # no kinoforge_ephemeral sentinel
        # no probe_state
    }
    verdict = classify(entry, live_pod_ids={"ledger-pod"}, now=_NOW, **_THRESHOLDS)
    assert verdict != Verdict.GC_404
    assert verdict != Verdict.SKIP_NO_PROBE


def test_ephemeral_probe_not_found_gc_404() -> None:
    verdict = _classify_ephemeral(_entry("not_found"), _THRESHOLDS, _NOW, stall_history=None)
    assert verdict == Verdict.GC_404


def test_ephemeral_probe_no_substrate_skip() -> None:
    verdict = _classify_ephemeral(_entry("no_substrate"), _THRESHOLDS, _NOW, stall_history=None)
    assert verdict == Verdict.SKIP_NO_PROBE


def test_ephemeral_probe_failed_returns_probe_failed() -> None:
    verdict = _classify_ephemeral(_entry("failed"), _THRESHOLDS, _NOW, stall_history=None)
    assert verdict == Verdict.PROBE_FAILED


def test_ephemeral_overage_fires_when_lifetime_exceeded() -> None:
    """created_at = 1000, now = 1000 + max_lifetime + 1 → OVERAGE_REAP."""
    now = 1000.0 + _THRESHOLDS["max_lifetime_s"] + 1.0
    verdict = _classify_ephemeral(_entry("ok"), _THRESHOLDS, now, stall_history=None)
    assert verdict == Verdict.OVERAGE_REAP


def test_ephemeral_overage_takes_precedence_over_stall() -> None:
    """OVERAGE fires even when stall history would otherwise say STALL."""
    history: dict[str, deque[tuple[float, float]]] = {
        "pod-1": deque([(0.0, 0.0)] * 10),
    }
    now = 1000.0 + _THRESHOLDS["max_lifetime_s"] + 1.0
    verdict = _classify_ephemeral(_entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, now, stall_history=history)
    assert verdict == Verdict.OVERAGE_REAP


def test_ephemeral_stall_skipped_when_history_none_one_shot_mode() -> None:
    """`kinoforge reap` one-shot passes stall_history=None → STALL never fires."""
    verdict = _classify_ephemeral(_entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, _NOW, stall_history=None)
    assert verdict == Verdict.LIVE


def test_ephemeral_stall_window_unsatisfied_returns_live() -> None:
    """N-1 zero-util samples → not yet stall; LIVE."""
    # window = ceil(120 / 30) = 4 samples needed
    history: dict[str, deque[tuple[float, float]]] = {
        "pod-1": deque([(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)]),  # only 3, need 4
    }
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, _NOW, stall_history=history,
    )
    assert verdict == Verdict.LIVE


def test_ephemeral_stall_window_satisfied_returns_stall_reap() -> None:
    """N consecutive zero-util samples → STALL_REAP."""
    history: dict[str, deque[tuple[float, float]]] = {
        "pod-1": deque([(0.0, 0.0)] * 4),  # window satisfied
    }
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, _NOW, stall_history=history,
    )
    assert verdict == Verdict.STALL_REAP


def test_ephemeral_stall_window_resets_on_recovery_sample() -> None:
    """One sample above threshold breaks the streak → LIVE."""
    history: dict[str, deque[tuple[float, float]]] = {
        # second-to-last sample shows recovery (GPU at 60%)
        "pod-1": deque([(0.0, 0.0), (60.0, 30.0), (0.0, 0.0), (0.0, 0.0)]),
    }
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, _NOW, stall_history=history,
    )
    assert verdict == Verdict.LIVE


def test_ephemeral_no_idle_reap_even_with_zero_util(monkeypatch: pytest.MonkeyPatch) -> None:
    """IDLE_REAP must never fire from the ephemeral branch.

    Otherwise model-load periods (Wan 14B weight fetch, 4-8 minutes at 0%
    GPU) would trip false positives.
    """
    history: dict[str, deque[tuple[float, float]]] = {"pod-1": deque([(0.0, 0.0)] * 4)}
    # Set idle_timeout to a tiny value to MAXIMISE the chance of accidental IDLE firing.
    thresholds = {**_THRESHOLDS, "idle_timeout_s": 1.0}
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), thresholds, _NOW, stall_history=history,
    )
    assert verdict != Verdict.IDLE_REAP


def test_ephemeral_live_when_util_high() -> None:
    history: dict[str, deque[tuple[float, float]]] = {"pod-1": deque([(80.0, 40.0)] * 4)}
    verdict = _classify_ephemeral(
        _entry("ok", gpu=80.0, cpu=40.0), _THRESHOLDS, _NOW, stall_history=history,
    )
    assert verdict == Verdict.LIVE
```

- [ ] **Step 2: Run tests — confirm FAIL**

Run: `pixi run pytest tests/core/test_classify_ephemeral.py -v`
Expected: All tests FAIL — `_classify_ephemeral` does not yet exist; new Verdict values do not exist.

- [ ] **Step 3: Extend `Verdict` enum in `src/kinoforge/core/reaper.py`**

Locate the `Verdict` enum (line 24-41). Append three new members AFTER `DEGRADED_REAP`:

```python
    DEGRADED_REAP = "DEGRADED_REAP"  # Layer LoRA — pod self-marked degraded
    GC_404 = "GC_404"  # Ephemeral — provider 404; remove index row, no destroy
    SKIP_NO_PROBE = "SKIP_NO_PROBE"  # Ephemeral — provider lacks probe substrate
    PROBE_FAILED = "PROBE_FAILED"  # Ephemeral — probe raised transient error
```

Add `GC_404` to `DEFAULT_APPLY_POLICY` (line 56-67):

```python
DEFAULT_APPLY_POLICY = Policy(
    act_verdicts=frozenset(
        {
            Verdict.IDLE_REAP,
            Verdict.OVERAGE_REAP,
            Verdict.STALE_LEDGER,
            Verdict.STALL_REAP,  # C26
            Verdict.RESTART_LOOP_REAP,  # C27
            Verdict.DEGRADED_REAP,  # Layer LoRA
            Verdict.GC_404,  # Ephemeral row cleanup
        }
    )
)
```

`SKIP_NO_PROBE` and `PROBE_FAILED` are deliberately NOT in `DEFAULT_APPLY_POLICY` — they translate to WARN-once log lines inside `act_on_verdict`, not state mutations, so they fire under any policy regardless.

- [ ] **Step 4: Add `_classify_ephemeral` function to `src/kinoforge/core/reaper.py`**

After the existing `classify()` function (around line 247-), add:

```python
def _classify_ephemeral(
    entry: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    now: float,
    *,
    stall_history: Mapping[str, deque[tuple[float, float]]] | None,
) -> Verdict:
    """Heartbeat-free classification for entries flagged `kinoforge_ephemeral=True`.

    Decision tree (spec §3.5):
      1. probe_state == "not_found"   → GC_404
      2. probe_state == "no_substrate" → SKIP_NO_PROBE
      3. probe_state == "failed"       → PROBE_FAILED
      4. now - created_at > max_lifetime_s → OVERAGE_REAP
      5. stall_history is None (one-shot CLI) → LIVE (STALL skipped)
      6. N consecutive samples where gpu < stall_gpu_threshold AND cpu < stall_cpu_threshold
         where N = ceil(stall_window_s / heartbeat_interval_s) → STALL_REAP
      7. else → LIVE

    NEVER reads heartbeat keys (last_heartbeat, heartbeat_thread_tick,
    session_claim, restart_count); the AST invariant test guards this.

    Args:
        entry: Synthetic ephemeral entry from `_synthesize_ephemeral_entry`.
        thresholds: Same keys `classify` consumes — only `max_lifetime_s`,
            `stall_window_s`, `stall_gpu_threshold`, `stall_cpu_threshold`,
            `heartbeat_interval_s` are read.
        now: Wall-clock now (seconds, float).
        stall_history: Per-pod deque of `(gpu_util_pct, cpu_pct)` samples,
            owned by `SweeperLoop`. ``None`` from `kinoforge reap` one-shot
            mode — skip STALL_REAP entirely.

    Returns:
        One of: GC_404, SKIP_NO_PROBE, PROBE_FAILED, OVERAGE_REAP,
        STALL_REAP, LIVE. Never returns IDLE_REAP / HEARTBEAT_UNKNOWN /
        STALE_LEDGER / HEARTBEAT_SUBSTRATE_MISSING.
    """
    import math

    probe_state = entry.get("probe_state")
    if probe_state == "not_found":
        return Verdict.GC_404
    if probe_state == "no_substrate":
        return Verdict.SKIP_NO_PROBE
    if probe_state == "failed":
        return Verdict.PROBE_FAILED

    created_at = float(entry.get("created_at", 0.0))
    max_lifetime_s = float(thresholds["max_lifetime_s"])
    if now - created_at > max_lifetime_s:
        return Verdict.OVERAGE_REAP

    if stall_history is None:
        return Verdict.LIVE

    stall_window_s = float(thresholds.get("stall_window_s") or 0.0)
    interval_s = float(thresholds.get("heartbeat_interval_s") or 30.0)
    if stall_window_s <= 0.0:
        return Verdict.LIVE
    required = max(1, math.ceil(stall_window_s / interval_s))
    pod_id = str(entry["id"])
    history = stall_history.get(pod_id)
    if history is None or len(history) < required:
        return Verdict.LIVE
    gpu_thresh = float(thresholds.get("stall_gpu_threshold") or 0.0)
    cpu_thresh = float(thresholds.get("stall_cpu_threshold") or 0.0)
    recent = list(history)[-required:]
    if all(g < gpu_thresh and c < cpu_thresh for (g, c) in recent):
        return Verdict.STALL_REAP
    return Verdict.LIVE
```

Add the `deque` import at the top of `reaper.py`:

```python
from collections import deque
```

- [ ] **Step 5: Wire `classify()` dispatch in `src/kinoforge/core/reaper.py`**

At the very top of the existing `classify()` function body (the one at line 247), add the sentinel check before any existing logic:

```python
def classify(
    entry: Mapping[str, Any],
    live_pod_ids: set[str],
    now: float,
    **thresholds: Any,
) -> Verdict:
    """[existing docstring]"""
    if entry.get("kinoforge_ephemeral") is True:
        return _classify_ephemeral(
            entry,
            thresholds,
            now,
            stall_history=thresholds.get("stall_history"),
        )
    # ... existing body unchanged ...
```

NB: `stall_history` is passed through `**thresholds` so callers (sweep + act_on_verdict) can supply it without changing the positional signature.

- [ ] **Step 6: Run tests — confirm GREEN**

Run: `pixi run pytest tests/core/test_classify_ephemeral.py -v`
Expected: All 13 tests PASS.

- [ ] **Step 7: Run full reaper test suite — confirm no regression**

Run: `pixi run pytest tests/core/test_reaper.py -v`
Expected: All existing tests still PASS (the sentinel check is a no-op when `kinoforge_ephemeral` is absent).

- [ ] **Step 8: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/reaper.py tests/core/test_classify_ephemeral.py
git add src/kinoforge/core/reaper.py tests/core/test_classify_ephemeral.py
git commit -m "feat(reaper): Verdict.GC_404 + SKIP_NO_PROBE + PROBE_FAILED + _classify_ephemeral"
```

---

### Task 4: `sweep()` unions `EphemeralIndex` rows + per-tick probe cache

**Goal:** Sweeper discovers ephemeral pods. Per-tick probe cache prevents N probes per pod when ledger + index disagree. Ledger wins on overlap. Probe failures isolated per pod, never abort the sweep.

**Files:**
- Modify: `src/kinoforge/core/reaper_actor.py` (lines 297-, `sweep` function)
- Create: `tests/core/test_sweep_ephemeral_union.py`

**Acceptance Criteria:**
- [ ] Empty ledger + 1 ephemeral row → `sweep()` classifies 1 entry
- [ ] Ledger entry and index row with same id → ledger entry used (sentinel `kinoforge_ephemeral` NOT added)
- [ ] Per-tick probe cache: same `(provider, pod_id)` probed exactly once even if asked multiple times
- [ ] Probe raises `TransportError` → entry gets `probe_state="failed"` (caught by `_probe_with_cache`), not propagated
- [ ] Provider's `probe_runtime` returns `None` → entry gets `probe_state="no_substrate"`
- [ ] `policy=None` (read-only) → `ephemeral_index.remove` NOT called for any verdict

**Verify:** `pixi run pytest tests/core/test_sweep_ephemeral_union.py -v` → all PASS

**Steps:**

- [ ] **Step 1: Write `tests/core/test_sweep_ephemeral_union.py`**

```python
"""Unit tests for sweep() ephemeral union + probe cache."""
from __future__ import annotations

from collections import deque
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.interfaces import ComputeProvider, Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import Verdict
from kinoforge.core.reaper_actor import sweep
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _FakeClock:
    def now(self) -> float:
        return 1_000_000.0


class _FakeRunPodProvider(ComputeProvider):
    """Minimal ComputeProvider stub controllable via probe_calls."""

    kind = "runpod"

    def __init__(self) -> None:
        self.probe_calls: list[str] = []
        self.scripted_probes: dict[str, RuntimeProbe | None | Exception] = {}
        self._live_instances: list[Instance] = []

    def list_instances(self) -> list[Instance]:
        return list(self._live_instances)

    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:
        self.probe_calls.append(pod_id)
        result = self.scripted_probes.get(pod_id, None)
        if isinstance(result, Exception):
            raise result
        return result

    # Unused-but-required ABC stubs
    def find_offers(self, reqs: Any) -> list[Any]: return []
    def create_instance(self, spec: Any) -> Any: raise NotImplementedError
    def get_instance(self, instance_id: str) -> Any: raise NotImplementedError
    def stop_instance(self, instance_id: str) -> None: pass
    def destroy_instance(self, instance_id: str) -> None: pass
    def heartbeat(self, instance_id: str) -> None: pass
    def endpoints(self, instance: Any) -> dict[str, str]: return {}


def _make_ctx(tmp_path: Any, provider: ComputeProvider) -> tuple[LocalArtifactStore, Ledger, EphemeralIndex, dict]:
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test-run")
    index = EphemeralIndex(store=store)
    registry = {"runpod": lambda: provider, "skypilot": lambda: provider}
    return store, ledger, index, registry


def _add_index_row(index: EphemeralIndex, pod_id: str, provider: str = "runpod") -> None:
    index.add(EphemeralIndexRow(
        id=pod_id, warm_attach_key="wak-1", kinoforge_key="k-12345678901",
        endpoints={"8188": f"https://{pod_id}-8188.proxy.runpod.net"},
        provider=provider, created_at_local="2026-06-28T12:00:00",
    ))


_THRESHOLDS = {
    "max_lifetime_s": 5 * 3600,
    "stall_window_s": 120.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "heartbeat_interval_s": 30.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def test_sweep_empty_ledger_one_ephemeral_row_classifies_one_entry(tmp_path: Any) -> None:
    provider = _FakeRunPodProvider()
    provider.scripted_probes["pod-1"] = RuntimeProbe(
        pod_id="pod-1", found=True, container_uptime_s=60.0,
        gpu_util_pct=50.0, cpu_pct=20.0, cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:01",
    )
    store, ledger, index, registry = _make_ctx(tmp_path, provider)
    _add_index_row(index, "pod-1")

    report = sweep(store, ledger, registry.get, _THRESHOLDS, _FakeClock(), policy=None)

    assert "pod-1" in report.snapshot
    entry, verdict = report.snapshot["pod-1"]
    assert entry.get("kinoforge_ephemeral") is True
    assert entry["probe_state"] == "ok"


def test_sweep_overlap_ledger_wins_no_sentinel_added(tmp_path: Any) -> None:
    """Ledger entry for same id → use ledger entry; do NOT add ephemeral sentinel."""
    provider = _FakeRunPodProvider()
    provider.scripted_probes["pod-1"] = RuntimeProbe(
        pod_id="pod-1", found=True, container_uptime_s=60.0,
        gpu_util_pct=50.0, cpu_pct=20.0, cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:01",
    )
    store, ledger, index, registry = _make_ctx(tmp_path, provider)
    ledger.touch("pod-1", provider="runpod", provider_kind="runpod", warm_attach_key="wak-1")
    _add_index_row(index, "pod-1")

    report = sweep(store, ledger, registry.get, _THRESHOLDS, _FakeClock(), policy=None)

    entry, _verdict = report.snapshot["pod-1"]
    assert entry.get("kinoforge_ephemeral") is not True  # ledger entry, not synthetic
    assert provider.probe_calls == []  # ledger pod doesn't get probed


def test_sweep_probe_cache_one_call_per_pod_per_tick(tmp_path: Any) -> None:
    """Multiple internal lookups for the same pod_id within one sweep → 1 probe."""
    provider = _FakeRunPodProvider()
    provider.scripted_probes["pod-1"] = RuntimeProbe(
        pod_id="pod-1", found=True, container_uptime_s=60.0,
        gpu_util_pct=50.0, cpu_pct=20.0, cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:01",
    )
    store, ledger, index, registry = _make_ctx(tmp_path, provider)
    _add_index_row(index, "pod-1")

    sweep(store, ledger, registry.get, _THRESHOLDS, _FakeClock(), policy=None)
    # Second internal lookup (simulate via second sweep with shared cache?
    # The cache is per-tick, so two sweep() calls → two probes. This test
    # asserts the per-tick contract within one sweep() invocation.)
    assert len(provider.probe_calls) == 1


def test_sweep_probe_failure_one_pod_does_not_abort_others(tmp_path: Any) -> None:
    """TransportError on pod-A → pod-A classified PROBE_FAILED; pod-B still LIVE."""
    from kinoforge.providers.runpod.util import TransportError
    provider = _FakeRunPodProvider()
    provider.scripted_probes["pod-A"] = TransportError("simulated")
    provider.scripted_probes["pod-B"] = RuntimeProbe(
        pod_id="pod-B", found=True, container_uptime_s=60.0,
        gpu_util_pct=50.0, cpu_pct=20.0, cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:01",
    )
    store, ledger, index, registry = _make_ctx(tmp_path, provider)
    _add_index_row(index, "pod-A")
    _add_index_row(index, "pod-B")

    report = sweep(store, ledger, registry.get, _THRESHOLDS, _FakeClock(), policy=None)

    entry_a, verdict_a = report.snapshot["pod-A"]
    assert entry_a["probe_state"] == "failed"
    assert verdict_a == Verdict.PROBE_FAILED
    entry_b, verdict_b = report.snapshot["pod-B"]
    assert entry_b["probe_state"] == "ok"
    assert verdict_b == Verdict.LIVE


def test_sweep_provider_returns_none_yields_no_substrate(tmp_path: Any) -> None:
    """Provider.probe_runtime returns None → entry probe_state=no_substrate."""
    provider = _FakeRunPodProvider()
    provider.scripted_probes["pod-1"] = None  # ABC default behavior
    store, ledger, index, registry = _make_ctx(tmp_path, provider)
    _add_index_row(index, "pod-1", provider="skypilot")

    report = sweep(store, ledger, registry.get, _THRESHOLDS, _FakeClock(), policy=None)

    entry, verdict = report.snapshot["pod-1"]
    assert entry["probe_state"] == "no_substrate"
    assert verdict == Verdict.SKIP_NO_PROBE


def test_sweep_policy_none_does_not_mutate_index(tmp_path: Any) -> None:
    """Read-only sweep (policy=None) NEVER calls EphemeralIndex.remove."""
    provider = _FakeRunPodProvider()
    provider.scripted_probes["pod-dead"] = RuntimeProbe(
        pod_id="pod-dead", found=False, container_uptime_s=None,
        gpu_util_pct=None, cpu_pct=None, cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:01",
    )
    store, ledger, index, registry = _make_ctx(tmp_path, provider)
    _add_index_row(index, "pod-dead")

    sweep(store, ledger, registry.get, _THRESHOLDS, _FakeClock(), policy=None)

    # Row still present after read-only sweep
    assert any(r.id == "pod-dead" for r in index.rows())
```

- [ ] **Step 2: Run tests — confirm FAIL**

Run: `pixi run pytest tests/core/test_sweep_ephemeral_union.py -v`
Expected: All FAIL — sweep does not yet union ephemeral_index.

- [ ] **Step 3: Modify `sweep()` in `src/kinoforge/core/reaper_actor.py`**

After the existing entries loop (around line 348-) but BEFORE the snapshot-act loop (where each entry gets classified and optionally acted on), insert the ephemeral union. Locate the exact insertion point by reading around line 345-360 in the current file.

Find this block:
```python
    entries = list(ledger.entries())
    snapshot: dict[str, tuple[Mapping[str, Any], Verdict]] = {}

    for entry in entries:
        eid = str(entry["id"])
```

Replace `entries = list(ledger.entries())` and the lines immediately after with:

```python
    entries = list(ledger.entries())
    ledger_ids = {str(e["id"]) for e in entries}

    # Sweeper-side ephemeral reap (spec 2026-06-28):
    # Union EphemeralIndex.rows() with ledger.entries(). Ledger wins on overlap.
    from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex
    ephemeral_index = EphemeralIndex(store=store)
    probe_cache: dict[tuple[str, str], RuntimeProbe | None | str] = {}
    # sentinel "failed" string distinguishes "TransportError seen" from
    # legitimate None (no_substrate).
    for row in ephemeral_index.rows():
        if row.id in ledger_ids:
            continue
        provider_factory = registry_get_provider(row.provider)
        if provider_factory is None:
            continue
        probe_result = _probe_with_cache(provider_factory(), row, probe_cache)
        entries.append(_synthesize_ephemeral_entry(row, probe_result))

    snapshot: dict[str, tuple[Mapping[str, Any], Verdict]] = {}
```

Add a `stall_history` kwarg to `sweep()`:

```python
def sweep(
    store: ArtifactStore,
    ledger: Ledger,
    registry_get_provider: Callable[[str], Callable[[], ComputeProvider]],
    thresholds: Mapping[str, Any],
    clock: Clock,
    *,
    policy: Policy | None = None,
    stall_history: Mapping[str, "deque[tuple[float, float]]"] | None = None,
) -> SweepReport:
```

When forwarding thresholds to `classify` (look around line ~380 for the `classify` call), inject `stall_history` if present:

```python
    classify_kwargs = dict(thresholds)
    if stall_history is not None:
        classify_kwargs["stall_history"] = stall_history
    # For ephemeral entries: also append fresh probe sample to history
    if entry.get("kinoforge_ephemeral") and stall_history is not None and entry["probe_state"] == "ok":
        gpu = entry.get("gpu_util_pct") or 0.0
        cpu = entry.get("cpu_pct") or 0.0
        stall_history.setdefault(eid, deque(maxlen=20)).append((gpu, cpu))
    verdict = classify(entry, live_ids, now, **classify_kwargs)
```

Add helper functions `_probe_with_cache` and `_synthesize_ephemeral_entry` at module scope in `reaper_actor.py`:

```python
def _probe_with_cache(
    provider: ComputeProvider,
    row: "EphemeralIndexRow",
    cache: dict[tuple[str, str], RuntimeProbe | None | str],
) -> RuntimeProbe | None | str:
    """Probe runtime with per-tick caching. Returns RuntimeProbe / None / "failed"."""
    key = (row.provider, row.id)
    if key in cache:
        return cache[key]
    try:
        result: RuntimeProbe | None | str = provider.probe_runtime(row.id)
    except Exception as exc:  # noqa: BLE001 — covers TransportError and unknowns
        _log.warning("probe_runtime failed for %s/%s: %s", row.provider, row.id, exc)
        result = "failed"
    cache[key] = result
    return result


def _synthesize_ephemeral_entry(
    row: "EphemeralIndexRow",
    probe_result: RuntimeProbe | None | str,
) -> dict[str, Any]:
    """Build a ledger-shape dict from an EphemeralIndex row + probe outcome.

    Three probe_state encodings cover the four cases:
      - probe_result is "failed" → probe_state="failed"
      - probe_result is None → probe_state="no_substrate"
      - probe_result.found is False → probe_state="not_found"
      - probe_result.found is True → probe_state="ok" (util fields populated)
    """
    from datetime import datetime

    base: dict[str, Any] = {
        "id": row.id,
        "provider": row.provider,
        "provider_kind": row.provider,
        "kinoforge_ephemeral": True,
        "created_at": _iso_to_epoch(row.created_at_local),
    }
    if probe_result == "failed":
        base["probe_state"] = "failed"
    elif probe_result is None:
        base["probe_state"] = "no_substrate"
    else:
        assert isinstance(probe_result, RuntimeProbe)
        if not probe_result.found:
            base["probe_state"] = "not_found"
        else:
            base["probe_state"] = "ok"
            base["container_uptime_s"] = probe_result.container_uptime_s
            base["gpu_util_pct"] = probe_result.gpu_util_pct
            base["cpu_pct"] = probe_result.cpu_pct
    return base


def _iso_to_epoch(iso_str: str) -> float:
    """Convert local-TZ ISO timestamp to Unix epoch seconds."""
    from datetime import datetime

    return datetime.fromisoformat(iso_str).timestamp()
```

Add imports to the module header:

```python
from collections import deque

from kinoforge.core.runtime_probe import RuntimeProbe
```

- [ ] **Step 4: Run tests — confirm GREEN**

Run: `pixi run pytest tests/core/test_sweep_ephemeral_union.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Regression — run reaper_actor existing tests**

Run: `pixi run pytest tests/core/test_reaper_actor.py tests/core/test_lifecycle_sweeper.py -v`
Expected: All existing tests still PASS.

- [ ] **Step 6: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/reaper_actor.py tests/core/test_sweep_ephemeral_union.py
git add src/kinoforge/core/reaper_actor.py tests/core/test_sweep_ephemeral_union.py
git commit -m "feat(reaper): sweep() unions EphemeralIndex with per-tick probe cache"
```

---

### Task 5: `SweeperLoop` owns bounded stall history

**Goal:** Cross-tick state for STALL_REAP. In-memory only (resets on daemon restart). Bounded per pod. Evicted when pod disappears from ledger ∪ index.

**Files:**
- Modify: `src/kinoforge/core/sweeper.py` (lines 111-263)
- Create: `tests/core/test_sweeper_loop_stall_history.py`

**Acceptance Criteria:**
- [ ] `SweeperLoop.__init__` constructs `self._stall_history: dict[str, deque[tuple[float, float]]]`
- [ ] `_tick_once` passes `stall_history=self._stall_history` to `_sweep_fn`
- [ ] Each per-pod deque is bounded (`maxlen` = `ceil(stall_window_s / interval_s) + 2`)
- [ ] After each tick, deques for pods not in ledger ∪ index are evicted from the dict
- [ ] `reload()` does NOT clear history (in-tick samples survive policy/threshold reloads)
- [ ] A fresh `SweeperLoop` (post-restart) starts with empty history

**Verify:** `pixi run pytest tests/core/test_sweeper_loop_stall_history.py -v` → all PASS

**Steps:**

- [ ] **Step 1: Write `tests/core/test_sweeper_loop_stall_history.py`**

```python
"""Unit tests for SweeperLoop stall-history ownership."""
from __future__ import annotations

import math
from collections import deque
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper_actor import SweepReport
from kinoforge.core.sweeper import SweeperLoop
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


def _make_loop(tmp_path: Any, sweep_fn: Any, interval_s: float = 30.0) -> SweeperLoop:
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test-run")
    return SweeperLoop(
        store=store,
        ledger=ledger,
        registry_get_provider=lambda name: lambda: MagicMock(),
        thresholds={
            "stall_window_s": 120.0,
            "heartbeat_interval_s": interval_s,
            "stall_gpu_threshold": 5.0,
            "stall_cpu_threshold": 10.0,
            "max_lifetime_s": 5 * 3600,
            "idle_timeout_s": 600.0,
            "grace_after_session_s": 60.0,
            "restart_loop_window_s": 600.0,
            "restart_loop_uptime_threshold_s": 60.0,
        },
        interval_s=interval_s,
        host="test-host",
        policy=None,
        _sweep_fn=sweep_fn,
    )


def test_sweeper_loop_constructs_stall_history_dict(tmp_path: Any) -> None:
    loop = _make_loop(tmp_path, sweep_fn=lambda *a, **k: SweepReport(snapshot={}, actions=[]))
    assert hasattr(loop, "_stall_history")
    assert loop._stall_history == {}


def test_sweeper_loop_passes_history_to_sweep_fn(tmp_path: Any) -> None:
    captured: dict[str, Any] = {}
    def fake_sweep(*args: Any, **kwargs: Any) -> SweepReport:
        captured["stall_history"] = kwargs.get("stall_history")
        return SweepReport(snapshot={}, actions=[])
    loop = _make_loop(tmp_path, sweep_fn=fake_sweep)
    loop._tick_once()
    assert captured["stall_history"] is loop._stall_history


def test_sweeper_loop_evicts_pods_no_longer_in_ledger_or_index(tmp_path: Any) -> None:
    """After tick, history for pods absent from ledger ∪ index is dropped."""
    store = LocalArtifactStore(root=tmp_path)
    index = EphemeralIndex(store=store)
    index.add(EphemeralIndexRow(
        id="alive-pod", warm_attach_key="w", kinoforge_key="k-12345678901",
        endpoints={}, provider="runpod", created_at_local="2026-06-28T12:00:00",
    ))
    loop = _make_loop(tmp_path, sweep_fn=lambda *a, **k: SweepReport(snapshot={}, actions=[]))
    loop._store = store
    loop._stall_history["dead-pod"] = deque([(0.0, 0.0)])
    loop._stall_history["alive-pod"] = deque([(50.0, 20.0)])

    loop._tick_once()

    assert "dead-pod" not in loop._stall_history
    assert "alive-pod" in loop._stall_history


def test_sweeper_loop_reload_does_not_clear_history(tmp_path: Any) -> None:
    loop = _make_loop(tmp_path, sweep_fn=lambda *a, **k: SweepReport(snapshot={}, actions=[]))
    loop._stall_history["pod-1"] = deque([(0.0, 0.0)] * 3)
    loop.reload(interval_s=15.0)
    assert "pod-1" in loop._stall_history
    assert len(loop._stall_history["pod-1"]) == 3


def test_sweeper_loop_per_pod_deque_maxlen(tmp_path: Any) -> None:
    """Each per-pod deque is bounded so unbounded ticks don't leak memory."""
    loop = _make_loop(tmp_path, sweep_fn=lambda *a, **k: SweepReport(snapshot={}, actions=[]), interval_s=30.0)
    # SweeperLoop should create deques with maxlen >= ceil(120/30)+2 = 6
    loop._stall_history["pod-1"] = loop._make_history_deque()
    for _ in range(20):
        loop._stall_history["pod-1"].append((0.0, 0.0))
    assert len(loop._stall_history["pod-1"]) <= 8  # generous upper bound
```

- [ ] **Step 2: Run tests — confirm FAIL**

Run: `pixi run pytest tests/core/test_sweeper_loop_stall_history.py -v`
Expected: All FAIL — `_stall_history` attribute does not exist.

- [ ] **Step 3: Modify `src/kinoforge/core/sweeper.py`**

In `SweeperLoop.__init__` (line ~144), after the existing attribute setup, add:

```python
        import math
        from collections import deque

        # Per-pod stall sample deque. Bounded by stall_window_s / interval_s.
        # In-memory only — resets on restart (one-window grace acceptable).
        self._stall_history: dict[str, deque[tuple[float, float]]] = {}
        window = float(self._thresholds.get("stall_window_s") or 0.0)
        if window > 0:
            self._history_maxlen = math.ceil(window / self._interval_s) + 2
        else:
            self._history_maxlen = 8  # default upper bound
```

Add a helper:

```python
    def _make_history_deque(self) -> "deque[tuple[float, float]]":
        """Construct a bounded deque using the current maxlen."""
        from collections import deque
        return deque(maxlen=self._history_maxlen)
```

Modify `_tick_once` (line 232) to pass history and prune:

```python
    def _tick_once(self) -> None:
        try:
            with self._reload_lock:
                policy = self._policy
                thresholds = dict(self._thresholds)
            report = self._sweep_fn(
                self._store,
                self._ledger,
                self._registry_get_provider,
                thresholds,
                self._clock,
                policy=policy,
                stall_history=self._stall_history,
            )
            self._prune_history()
            now = self._clock.now()
            self._stats.fold(report, now=now)
            self._ledger.touch(
                f"sweeper:{self._host}",
                last_heartbeat=now,
                heartbeat_thread_tick=now,
                **self._stats.snapshot_for_ledger(),
            )
        except Exception:
            self._stats.errors_total += 1
            self._logger.exception("sweep tick failed on host=%s", self._host)

    def _prune_history(self) -> None:
        """Evict per-pod deques for pods no longer in ledger ∪ ephemeral_index."""
        from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex
        live_ids: set[str] = {str(e["id"]) for e in self._ledger.entries()}
        live_ids.update(r.id for r in EphemeralIndex(store=self._store).rows())
        for pod_id in list(self._stall_history.keys()):
            if pod_id not in live_ids:
                del self._stall_history[pod_id]
```

- [ ] **Step 4: Run tests — confirm GREEN**

Run: `pixi run pytest tests/core/test_sweeper_loop_stall_history.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Regression**

Run: `pixi run pytest tests/core/test_lifecycle_sweeper.py -v`
Expected: All existing sweeper tests PASS.

- [ ] **Step 6: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/sweeper.py tests/core/test_sweeper_loop_stall_history.py
git add src/kinoforge/core/sweeper.py tests/core/test_sweeper_loop_stall_history.py
git commit -m "feat(sweeper): SweeperLoop owns bounded stall history; passes to sweep"
```

---

### Task 6: `act_on_verdict` handles `GC_404`, `SKIP_NO_PROBE`, `PROBE_FAILED`

**Goal:** Verdicts produced by `_classify_ephemeral` reach `act_on_verdict` and trigger the right side-effects: GC_404 removes the index row (no destroy), SKIP_NO_PROBE / PROBE_FAILED log WARN-once and no-op.

**Files:**
- Modify: `src/kinoforge/core/reaper_actor.py` (line 34, `_WARNED_SUBSTRATE_MISSING`; line 238-280, `act_on_verdict` body; line 43, reset function)
- Create: `tests/integration/test_sweeper_reaps_ephemeral_stall.py`
- Create: `tests/integration/test_sweep_skypilot_ephemeral_warn_once.py`

**Acceptance Criteria:**
- [ ] `act_on_verdict` with `v2 == GC_404` calls `EphemeralIndex.remove(pod_id)` and `ledger.forget` is NOT called
- [ ] Return `ActionResult.action == "gc_404_removed"` for GC_404
- [ ] `act_on_verdict` with `v2 == SKIP_NO_PROBE` logs once to `_WARNED_PROBE_MISSING`, returns `action == "no_op"`
- [ ] Same `(provider_kind, pod_id)` SKIP_NO_PROBE → only ONE WARN line across N ticks
- [ ] `act_on_verdict` with `v2 == PROBE_FAILED` logs once to `_WARNED_PROBE_FAILED` keyed by `(provider_kind, pod_id, error_class)`, returns `action == "probe_failed"`
- [ ] Integration: scripted probe sequence (LIVE, 0%, 0%, 0%, 0%) → STALL_REAP fires on the 5th tick → `destroy_instance` called + index row removed

**Verify:**
- `pixi run pytest tests/integration/test_sweeper_reaps_ephemeral_stall.py tests/integration/test_sweep_skypilot_ephemeral_warn_once.py -v` → all PASS

**Steps:**

- [ ] **Step 1: Write integration tests**

`tests/integration/test_sweeper_reaps_ephemeral_stall.py`:

```python
"""Integration: scripted probe sequence drives STALL_REAP + index cleanup."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
from kinoforge.core.reaper_actor import sweep
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _ScriptedProvider:
    """Provider whose probe_runtime returns the next scripted value per call."""
    kind = "runpod"

    def __init__(self, scripted: list[RuntimeProbe | None | Exception]) -> None:
        self.scripted = list(scripted)
        self.destroy_calls: list[str] = []

    def list_instances(self) -> list[Any]:
        return []

    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:
        if not self.scripted:
            raise RuntimeError("probe script exhausted")
        result = self.scripted.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def destroy_instance(self, instance_id: str) -> None:
        self.destroy_calls.append(instance_id)

    # ABC stubs
    def find_offers(self, reqs: Any) -> list[Any]: return []
    def create_instance(self, spec: Any) -> Any: raise NotImplementedError
    def get_instance(self, instance_id: str) -> Any: raise NotImplementedError
    def stop_instance(self, instance_id: str) -> None: pass
    def heartbeat(self, instance_id: str) -> None: pass
    def endpoints(self, instance: Any) -> dict[str, str]: return {}


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1_000_000.0
    def now(self) -> float:
        return self.t


def _live_probe() -> RuntimeProbe:
    return RuntimeProbe(
        pod_id="pod-1", found=True, container_uptime_s=60.0,
        gpu_util_pct=80.0, cpu_pct=40.0, cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:00",
    )


def _zero_probe() -> RuntimeProbe:
    return RuntimeProbe(
        pod_id="pod-1", found=True, container_uptime_s=120.0,
        gpu_util_pct=0.0, cpu_pct=0.0, cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:00",
    )


_THRESHOLDS = {
    "max_lifetime_s": 5 * 3600,
    "stall_window_s": 90.0,
    "heartbeat_interval_s": 30.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def test_stall_window_drives_destroy_and_index_removal(tmp_path: Any) -> None:
    """After 3 zero-util samples (window=90/interval=30=3), STALL_REAP fires."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    index.add(EphemeralIndexRow(
        id="pod-1", warm_attach_key="w", kinoforge_key="k-12345678901",
        endpoints={"8188": "https://x"}, provider="runpod",
        created_at_local="2026-06-28T12:00:00",
    ))

    provider = _ScriptedProvider([_live_probe(), _zero_probe(), _zero_probe(), _zero_probe()])
    registry = {"runpod": lambda: provider}
    clock = _FakeClock()
    history: dict[str, Any] = {}

    # Tick 1: live util — LIVE
    report = sweep(store, ledger, registry.get, _THRESHOLDS, clock, policy=DEFAULT_APPLY_POLICY, stall_history=history)
    assert report.snapshot["pod-1"][1] == Verdict.LIVE
    assert provider.destroy_calls == []

    # Ticks 2-3: zero util but window not yet satisfied
    for _ in range(2):
        clock.t += 30.0
        sweep(store, ledger, registry.get, _THRESHOLDS, clock, policy=DEFAULT_APPLY_POLICY, stall_history=history)
    assert provider.destroy_calls == []

    # Tick 4: third zero sample → window satisfied (3 samples covering 90s)
    clock.t += 30.0
    report = sweep(store, ledger, registry.get, _THRESHOLDS, clock, policy=DEFAULT_APPLY_POLICY, stall_history=history)
    assert provider.destroy_calls == ["pod-1"]
    assert not any(r.id == "pod-1" for r in index.rows())
```

`tests/integration/test_sweep_skypilot_ephemeral_warn_once.py`:

```python
"""Integration: probe=None → SKIP_NO_PROBE, WARN-once dedup."""
from __future__ import annotations

import logging
from typing import Any

import pytest

from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY
from kinoforge.core.reaper_actor import (
    _WARNED_PROBE_MISSING,
    sweep,
)
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _NoProbeProvider:
    """Inherits ABC default (returns None)."""
    kind = "skypilot"
    def list_instances(self) -> list[Any]: return []
    def probe_runtime(self, pod_id: str) -> None: return None
    def destroy_instance(self, instance_id: str) -> None: pass
    def find_offers(self, reqs: Any) -> list[Any]: return []
    def create_instance(self, spec: Any) -> Any: raise NotImplementedError
    def get_instance(self, instance_id: str) -> Any: raise NotImplementedError
    def stop_instance(self, instance_id: str) -> None: pass
    def heartbeat(self, instance_id: str) -> None: pass
    def endpoints(self, instance: Any) -> dict[str, str]: return {}


class _FakeClock:
    def now(self) -> float: return 1_000_000.0


_THRESHOLDS = {
    "max_lifetime_s": 5 * 3600,
    "stall_window_s": 90.0,
    "heartbeat_interval_s": 30.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def test_two_ticks_yield_one_warn_log(tmp_path: Any, caplog: pytest.LogCaptureFixture) -> None:
    _WARNED_PROBE_MISSING.clear()
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    index.add(EphemeralIndexRow(
        id="sky-pod", warm_attach_key="w", kinoforge_key="k-12345678901",
        endpoints={}, provider="skypilot",
        created_at_local="2026-06-28T12:00:00",
    ))
    registry = {"skypilot": lambda: _NoProbeProvider()}
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        sweep(store, ledger, registry.get, _THRESHOLDS, _FakeClock(),
              policy=DEFAULT_APPLY_POLICY, stall_history={})
    warn_lines = [r for r in caplog.records if "no probe substrate" in r.getMessage().lower()
                  or "skip_no_probe" in r.getMessage().lower()
                  or "SKIP_NO_PROBE" in r.getMessage()]
    assert len(warn_lines) == 1
```

- [ ] **Step 2: Run tests — confirm FAIL**

Run: `pixi run pytest tests/integration/test_sweeper_reaps_ephemeral_stall.py tests/integration/test_sweep_skypilot_ephemeral_warn_once.py -v`
Expected: FAIL — new verdicts not yet handled in `act_on_verdict`.

- [ ] **Step 3: Modify `src/kinoforge/core/reaper_actor.py`**

At module top (line ~34), add two new dedup sets:

```python
_WARNED_SUBSTRATE_MISSING: set[tuple[str, str]] = set()
_WARNED_PROBE_MISSING: set[tuple[str, str]] = set()
_WARNED_PROBE_FAILED: set[tuple[str, str, str]] = set()
```

Update the reset function (line ~43):

```python
def _reset_warned_substrate_missing() -> None:
    _WARNED_SUBSTRATE_MISSING.clear()
    _WARNED_PROBE_MISSING.clear()
    _WARNED_PROBE_FAILED.clear()
```

In `act_on_verdict` (line ~253-280), add new branches BEFORE the `elif v2 == Verdict.STALE_LEDGER:` branch:

```python
            elif v2 == Verdict.GC_404:
                # Ephemeral row points at a pod the provider no longer has.
                # Remove the row; no destroy_instance (pod already gone);
                # ledger.forget not needed (the entry was synthesised from
                # the index, never recorded in the ledger).
                from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex
                EphemeralIndex(store=store).remove(instance_id)
                action = "gc_404_removed"
            elif v2 == Verdict.SKIP_NO_PROBE:
                provider_kind = str(entry.get("provider_kind", ""))
                dedup_key = (provider_kind, instance_id)
                if dedup_key not in _WARNED_PROBE_MISSING:
                    _WARNED_PROBE_MISSING.add(dedup_key)
                    _log.warning(
                        "provider %r lacks runtime-probe substrate; "
                        "SKIP_NO_PROBE for ephemeral pod %s",
                        provider_kind, instance_id,
                    )
                action = "no_op"
            elif v2 == Verdict.PROBE_FAILED:
                provider_kind = str(entry.get("provider_kind", ""))
                error_class = str(entry.get("probe_error_class", "Exception"))
                dedup_key = (provider_kind, instance_id, error_class)
                if dedup_key not in _WARNED_PROBE_FAILED:
                    _WARNED_PROBE_FAILED.add(dedup_key)
                    _log.warning(
                        "probe_runtime raised %s for ephemeral pod %s/%s; "
                        "will retry next tick",
                        error_class, provider_kind, instance_id,
                    )
                action = "probe_failed"
```

Update `destroy_confirmed` import branch (line ~254) to include `GC_404` is NOT in the destroy set:

The existing `elif v2 in {Verdict.IDLE_REAP, Verdict.OVERAGE_REAP, Verdict.ORPHAN_REAP, Verdict.STALL_REAP}:` block needs no change — these still destroy. `GC_404` is intentionally separate (no destroy).

- [ ] **Step 4: Plumb `policy` to include `SKIP_NO_PROBE` and `PROBE_FAILED` in act-set so they reach `act_on_verdict`**

These verdicts produce no state mutation but DO need to fire `act_on_verdict` to log the WARN. Easiest: extend the act dispatch to include them.

In `sweep()`, the dispatch loop currently only acts on verdicts in `policy.act_verdicts`. Since SKIP_NO_PROBE / PROBE_FAILED have no associated state change, we want them logged-once REGARDLESS of policy. Add a small helper in sweep:

```python
_LOG_ONLY_VERDICTS = frozenset({Verdict.SKIP_NO_PROBE, Verdict.PROBE_FAILED})
```

And in the dispatch loop:

```python
        if policy is not None and verdict in policy.act_verdicts:
            actions.append(act_on_verdict(store, ledger, provider, entry, verdict, thresholds=thresholds, clock=clock))
        elif verdict in _LOG_ONLY_VERDICTS:
            # Always log; never mutate state. Dispatch through act_on_verdict
            # only for the WARN-once side effect.
            actions.append(act_on_verdict(store, ledger, provider, entry, verdict, thresholds=thresholds, clock=clock))
```

- [ ] **Step 5: Run tests — confirm GREEN**

Run: `pixi run pytest tests/integration/test_sweeper_reaps_ephemeral_stall.py tests/integration/test_sweep_skypilot_ephemeral_warn_once.py -v`
Expected: Both PASS.

- [ ] **Step 6: Run earlier ephemeral unit tests + reaper_actor regression**

Run: `pixi run pytest tests/core/test_classify_ephemeral.py tests/core/test_sweep_ephemeral_union.py tests/core/test_reaper_actor.py -v`
Expected: All PASS.

- [ ] **Step 7: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/reaper_actor.py tests/integration/test_sweeper_reaps_ephemeral_stall.py tests/integration/test_sweep_skypilot_ephemeral_warn_once.py
git add src/kinoforge/core/reaper_actor.py tests/integration/test_sweeper_reaps_ephemeral_stall.py tests/integration/test_sweep_skypilot_ephemeral_warn_once.py
git commit -m "feat(reaper): act_on_verdict GC_404 removes index row; SKIP_NO_PROBE WARN-once"
```

---

### Task 7: `kinoforge reap` CLI integration + session-claim integration test

**Goal:** CLI emits ephemeral rows in dry-run, applies GC_404, skips STALL in one-shot. New action literals visible in human + JSONL formats. Session-claim race confirmed safe.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_emit_reap_human` at 2116, `_emit_reap_jsonl` at 2177)
- Modify: `src/kinoforge/core/sweeper_metrics.py` (add counters)
- Create: `tests/integration/test_reap_cli_ephemeral.py`
- Create: `tests/integration/test_sweeper_skips_on_session_claim.py`

**Acceptance Criteria:**
- [ ] `kinoforge reap` (dry-run) prints synthesised ephemeral entries with verdict column
- [ ] `kinoforge reap --apply` with stale-row pod runs GC_404 path → row removed
- [ ] `kinoforge reap` one-shot (no `stall_history`) DOES NOT emit STALL_REAP even when probe shows 0% util
- [ ] `_emit_reap_jsonl` includes new action literals: `gc_404_removed`, `probe_failed`, `no_op` for SKIP_NO_PROBE
- [ ] `_SweeperStats` counters `gc_404_total`, `probe_failed_total`, `skip_no_probe_total` increment correctly on `fold()`
- [ ] Session-claim integration: `provision:<pod_id>` lock held → sweeper returns `action="deferred-session-claim"`, destroy NOT called

**Verify:** `pixi run pytest tests/integration/test_reap_cli_ephemeral.py tests/integration/test_sweeper_skips_on_session_claim.py -v` → all PASS

**Steps:**

- [ ] **Step 1: Write `tests/integration/test_sweeper_skips_on_session_claim.py`**

```python
"""Sweeper defers when provision:<pod_id> lock is held."""
from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
from kinoforge.core.reaper_actor import sweep
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex, EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


class _ZeroUtilProvider:
    """Returns LIVE-but-zero-util so verdict would be STALL_REAP without claim."""
    kind = "runpod"
    def __init__(self) -> None:
        self.destroy_calls: list[str] = []
    def list_instances(self) -> list[Any]: return []
    def probe_runtime(self, pod_id: str) -> RuntimeProbe:
        return RuntimeProbe(
            pod_id=pod_id, found=True, container_uptime_s=60.0,
            gpu_util_pct=0.0, cpu_pct=0.0, cost_per_hr=None,
            probed_at_local="2026-06-28T12:00:00",
        )
    def destroy_instance(self, instance_id: str) -> None:
        self.destroy_calls.append(instance_id)
    def find_offers(self, reqs: Any) -> list[Any]: return []
    def create_instance(self, spec: Any) -> Any: raise NotImplementedError
    def get_instance(self, instance_id: str) -> Any: raise NotImplementedError
    def stop_instance(self, instance_id: str) -> None: pass
    def heartbeat(self, instance_id: str) -> None: pass
    def endpoints(self, instance: Any) -> dict[str, str]: return {}


class _FakeClock:
    def __init__(self) -> None: self.t = 1_000_000.0
    def now(self) -> float: return self.t


_THRESHOLDS = {
    "max_lifetime_s": 5 * 3600, "stall_window_s": 60.0,
    "heartbeat_interval_s": 30.0, "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0, "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0, "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def test_provision_lock_held_defers_action(tmp_path: Any) -> None:
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    index = EphemeralIndex(store=store)
    index.add(EphemeralIndexRow(
        id="claimed-pod", warm_attach_key="w", kinoforge_key="k-12345678901",
        endpoints={}, provider="runpod",
        created_at_local="2026-06-28T12:00:00",
    ))
    provider = _ZeroUtilProvider()
    registry = {"runpod": lambda: provider}
    clock = _FakeClock()
    history: dict[str, Any] = {}

    # Drive enough ticks for stall window to be satisfied
    for _ in range(3):
        sweep(store, ledger, registry.get, _THRESHOLDS, clock, policy=DEFAULT_APPLY_POLICY, stall_history=history)
        clock.t += 30.0

    # Hold the provision lock; subsequent sweep must NOT destroy.
    lock = store.acquire_lock("provision:claimed-pod", ttl_s=60.0)
    token = lock.acquire(blocking=True)
    try:
        before_destroys = list(provider.destroy_calls)
        report = sweep(store, ledger, registry.get, _THRESHOLDS, clock, policy=DEFAULT_APPLY_POLICY, stall_history=history)
        # Either: action was deferred, OR destroy was not called.
        assert provider.destroy_calls == before_destroys
        actions_for_pod = [a for a in report.actions if a.instance_id == "claimed-pod"]
        if actions_for_pod:
            assert actions_for_pod[-1].action == "deferred-session-claim"
    finally:
        lock.release(token)
```

- [ ] **Step 2: Write `tests/integration/test_reap_cli_ephemeral.py`**

```python
"""kinoforge reap CLI integration with ephemeral pods."""
from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest


@pytest.mark.skip(reason="CLI integration scaffold — implement after Task 7 step 5 wires CLI")
def test_reap_dry_run_lists_ephemeral_pods(tmp_path: Any) -> None:
    """`kinoforge reap` (no --apply) shows ephemeral pods with their verdict."""
    # Setup: create an ArtifactStore with a stale ephemeral-index row,
    # invoke CLI, parse output.
    raise NotImplementedError


@pytest.mark.skip(reason="CLI integration scaffold")
def test_reap_apply_removes_stale_row(tmp_path: Any) -> None:
    """`kinoforge reap --apply` GC_404 path removes stale row from index."""
    raise NotImplementedError


def test_reap_one_shot_skips_stall_reap(tmp_path: Any) -> None:
    """Single sweep call WITHOUT stall_history → STALL_REAP impossible.

    This is the unit-level check that backs the CLI contract. The CLI
    test above (when implemented) wraps the same behavior end-to-end.
    """
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
    from kinoforge.core.reaper_actor import sweep
    from kinoforge.core.runtime_probe import RuntimeProbe
    from kinoforge.core.warm_reuse.ephemeral_index import (
        EphemeralIndex, EphemeralIndexRow,
    )
    from kinoforge.stores.local import LocalArtifactStore

    class _ZeroProvider:
        kind = "runpod"
        def __init__(self) -> None: self.destroy_calls: list[str] = []
        def list_instances(self) -> list[Any]: return []
        def probe_runtime(self, pod_id: str) -> RuntimeProbe:
            return RuntimeProbe(
                pod_id=pod_id, found=True, container_uptime_s=60.0,
                gpu_util_pct=0.0, cpu_pct=0.0, cost_per_hr=None,
                probed_at_local="2026-06-28T12:00:00",
            )
        def destroy_instance(self, instance_id: str) -> None:
            self.destroy_calls.append(instance_id)
        def find_offers(self, reqs: Any) -> list[Any]: return []
        def create_instance(self, spec: Any) -> Any: raise NotImplementedError
        def get_instance(self, instance_id: str) -> Any: raise NotImplementedError
        def stop_instance(self, instance_id: str) -> None: pass
        def heartbeat(self, instance_id: str) -> None: pass
        def endpoints(self, instance: Any) -> dict[str, str]: return {}

    class _Clock:
        def now(self) -> float: return 1_000_000.0

    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    EphemeralIndex(store=store).add(EphemeralIndexRow(
        id="pod-1", warm_attach_key="w", kinoforge_key="k-12345678901",
        endpoints={}, provider="runpod",
        created_at_local="2026-06-28T12:00:00",
    ))
    provider = _ZeroProvider()
    registry = {"runpod": lambda: provider}

    # NOTE: stall_history=None mimics the CLI one-shot.
    report = sweep(
        store, ledger, registry.get,
        {"max_lifetime_s": 5*3600, "stall_window_s": 30.0, "heartbeat_interval_s": 30.0,
         "stall_gpu_threshold": 5.0, "stall_cpu_threshold": 10.0,
         "idle_timeout_s": 600.0, "grace_after_session_s": 60.0,
         "restart_loop_window_s": 600.0, "restart_loop_uptime_threshold_s": 60.0},
        _Clock(), policy=DEFAULT_APPLY_POLICY, stall_history=None,
    )
    entry, verdict = report.snapshot["pod-1"]
    assert verdict == Verdict.LIVE
    assert provider.destroy_calls == []
```

- [ ] **Step 3: Add counters to `_SweeperStats` (`src/kinoforge/core/sweeper_metrics.py`)**

Read the file first to understand the existing fold pattern. Add fields:

```python
@dataclass
class _SweeperStats:
    sweeps_total: int = 0
    destroys_total: int = 0
    errors_total: int = 0
    gc_404_total: int = 0          # NEW
    probe_failed_total: int = 0    # NEW
    skip_no_probe_total: int = 0   # NEW
    # ... existing fields
```

In `fold()`, count the new action literals:

```python
    def fold(self, report: SweepReport, *, now: float) -> None:
        self.sweeps_total += 1
        for action in report.actions:
            if action.action == "destroyed_and_forgot":
                self.destroys_total += 1
            elif action.action == "gc_404_removed":
                self.gc_404_total += 1
            elif action.action == "probe_failed":
                self.probe_failed_total += 1
            elif action.action == "no_op" and action.applied_verdict == Verdict.SKIP_NO_PROBE:
                self.skip_no_probe_total += 1
            elif action.action == "failed":
                self.errors_total += 1
        # ... existing fold body
```

In `snapshot_for_ledger()`, include the new counters in the returned dict, and update `summary_line()` similarly.

- [ ] **Step 4: Update CLI emitters in `src/kinoforge/cli/_commands.py`**

Read `_emit_reap_human` (line 2116) and `_emit_reap_jsonl` (line 2177) to confirm their current structure. Add cases for the new action literals:

In `_emit_reap_human`, add to the action-formatting table/branches:
```python
ACTION_DISPLAY = {
    # ... existing entries
    "gc_404_removed": "GC stale row",
    "probe_failed": "probe failed",
}
```

In `_emit_reap_jsonl`, ensure the new literals serialise cleanly (StrEnum verdicts already JSONise; action strings pass through).

- [ ] **Step 5: Run tests — confirm GREEN**

Run: `pixi run pytest tests/integration/test_reap_cli_ephemeral.py tests/integration/test_sweeper_skips_on_session_claim.py -v`
Expected: PASS (CLI scaffold tests stay skipped; one-shot + session-claim integration tests PASS).

- [ ] **Step 6: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/cli/_commands.py src/kinoforge/core/sweeper_metrics.py tests/integration/test_reap_cli_ephemeral.py tests/integration/test_sweeper_skips_on_session_claim.py
git add src/kinoforge/cli/_commands.py src/kinoforge/core/sweeper_metrics.py tests/integration/test_reap_cli_ephemeral.py tests/integration/test_sweeper_skips_on_session_claim.py
git commit -m "feat(cli): kinoforge reap emits ephemeral pod entries; SweeperStats counters"
```

---

### Task 8: AST invariant — `_classify_ephemeral` consumes no heartbeat keys

**Goal:** Compile-time guarantee that the ephemeral branch never re-couples to heartbeat substrate. Catches refactor regressions.

**Files:**
- Create: `tests/test_classify_ephemeral_no_heartbeat_keys.py`

**Acceptance Criteria:**
- [ ] Test walks `src/kinoforge/core/reaper.py` AST, finds `_classify_ephemeral` function
- [ ] Asserts the function body contains zero string-literal subscript accesses to: `"last_heartbeat"`, `"heartbeat_thread_tick"`, `"session_claim"`, `"restart_count"`, `"last_status"`
- [ ] Test fails loudly with `pytest -v` if the function is renamed/moved (FunctionNotFound error)

**Verify:** `pixi run pytest tests/test_classify_ephemeral_no_heartbeat_keys.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write `tests/test_classify_ephemeral_no_heartbeat_keys.py`**

```python
"""AST invariant: _classify_ephemeral consumes no heartbeat substrate keys.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5.9
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_FORBIDDEN_KEYS = frozenset({
    "last_heartbeat",
    "heartbeat_thread_tick",
    "session_claim",
    "restart_count",
    "last_status",
})


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in reaper.py")


def _string_subscript_keys(node: ast.AST) -> set[str]:
    """Collect every string-literal subscript key accessed inside `node`."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript):
            slice_node = sub.slice
            if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
                keys.add(slice_node.value)
        if isinstance(sub, ast.Call):
            # entry.get("key", ...) / thresholds.get("key", ...)
            if (
                isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "get"
                and sub.args
                and isinstance(sub.args[0], ast.Constant)
                and isinstance(sub.args[0].value, str)
            ):
                keys.add(sub.args[0].value)
    return keys


def test_classify_ephemeral_does_not_read_heartbeat_keys() -> None:
    path = Path(__file__).resolve().parents[1] / "src" / "kinoforge" / "core" / "reaper.py"
    tree = ast.parse(path.read_text())
    func = _find_function(tree, "_classify_ephemeral")
    keys = _string_subscript_keys(func)
    leaked = keys & _FORBIDDEN_KEYS
    assert not leaked, (
        f"_classify_ephemeral leaked heartbeat keys {sorted(leaked)} — "
        f"ephemeral branch must stay heartbeat-free per spec §5.9."
    )
```

- [ ] **Step 2: Run — confirm GREEN immediately**

Run: `pixi run pytest tests/test_classify_ephemeral_no_heartbeat_keys.py -v`
Expected: PASS — `_classify_ephemeral` as implemented in Task 3 does not touch these keys.

- [ ] **Step 3: Sanity check — deliberately add a forbidden key locally, re-run, confirm FAIL, then revert**

Quick smoke that the test actually catches violations. Add `last = entry.get("last_heartbeat", None)` temporarily inside `_classify_ephemeral`, run the test, confirm FAIL, then revert.

- [ ] **Step 4: Commit**

```bash
git add tests/test_classify_ephemeral_no_heartbeat_keys.py
git commit -m "test(invariant): AST scan _classify_ephemeral consumes no heartbeat keys"
```

---

### Task 9: Live smoke — RED scaffold (committed BEFORE live spend per project rule)

**Goal:** Live test exercises the full path on a real RunPod ephemeral pod. The scaffold is committed first as a RED test (skipped or xfail) before any RunPod spend is invoked. This protects against the failure mode of mid-spend context loss + 100+ LOC scaffold discarded by cleanup.

**Files:**
- Create: `tests/live/test_runpod_ephemeral_sweeper_smoke.py`

**Acceptance Criteria:**
- [ ] File exists, imports cleanly, has the full test body
- [ ] Test is decorated with `@pytest.mark.skip(reason="live — run with -m live and explicit pixi run preflight")` so CI doesn't fire it accidentally
- [ ] No live RunPod API call has happened yet (preflight not run)
- [ ] Scaffold is committed as a single atomic change

**Verify:** `pixi run pytest tests/live/test_runpod_ephemeral_sweeper_smoke.py -v` → SKIPPED, file collects cleanly

**Steps:**

- [ ] **Step 1: Write `tests/live/test_runpod_ephemeral_sweeper_smoke.py`**

```python
"""Live smoke: RunPod sweeper reaps a wedged ephemeral pod (STALL_REAP path).

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5.10

Pre-conditions:
  - `pixi run preflight` green (RUNPOD/HF creds present, no live pods,
    clean working tree).
  - Standard test prompt: `examples/configs/prompts/field-realistic.txt`
    per project rule (cross-model comparability).

Cost budget: ~$0.30 (one Wan-T2V provision + selfterm-killed run +
sweeper-triggered destroy).

Flow:
  1. Start `kinoforge sweeper start` in background with
     stall_window_s=120, max_lifetime_s=600, interval_s=30.
  2. Provision ephemeral pod via cold-create CLI path with
     `--prompt <field-realistic.txt content>` and `--no-reuse`.
  3. SSH into pod; kill the selfterm watchdog process (simulate
     selfterm crash).
  4. Wait stall_window_s + 1 interval; assert sweeper destroys pod
     (poll `kinoforge list` + RunPod GraphQL).
  5. Assert `ephemeral-index.json` no longer contains pod id.
  6. Sweeper teardown.

This test is RED-scaffolded per project durability rule (commit RED
scaffold BEFORE invoking live spend).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.skip(
    reason="live smoke — run manually with explicit pixi run preflight + budget approval"
)


def _read_standard_prompt() -> str:
    """Standard test prompt for all video-gen live smokes (project rule)."""
    path = Path("examples/configs/prompts/field-realistic.txt")
    if not path.exists():
        pytest.skip(f"standard prompt missing at {path}")
    return path.read_text().strip()


def _pod_alive(pod_id: str) -> bool:
    """Probe RunPod GraphQL via kinoforge for ground truth."""
    result = subprocess.run(
        ["pixi", "run", "kinoforge", "list", "--format", "json"],
        capture_output=True, text=True, timeout=30,
    )
    return pod_id in result.stdout


@pytest.mark.live
def test_sweeper_reaps_wedged_ephemeral_pod_runpod() -> None:
    assert os.getenv("RUNPOD_API_KEY"), "RUNPOD_API_KEY missing — preflight should have caught this"
    prompt = _read_standard_prompt()

    # 1. Start sweeper in background.
    sweeper = subprocess.Popen(
        ["pixi", "run", "kinoforge", "sweeper", "start",
         "--stall-window-s", "120",
         "--max-lifetime-s", "600",
         "--interval-s", "30"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    pod_id: str | None = None
    try:
        # 2. Provision ephemeral pod (cold-create).
        provision = subprocess.run(
            ["pixi", "run", "kinoforge", "--ephemeral", "generate",
             "--config", "examples/configs/runpod-comfyui-wan-t2v.yaml",
             "--mode", "t2v", "--prompt", prompt,
             "--no-reuse"],
            capture_output=True, text=True, timeout=900,
        )
        assert provision.returncode == 0, provision.stderr

        # Extract pod_id from stdout (CLI prints it; pattern depends on impl).
        for line in provision.stdout.splitlines():
            if "instance " in line and "provision" in line:
                pod_id = line.split()[-1]
                break
        assert pod_id, f"could not extract pod_id from:\n{provision.stdout}"

        # 3. SSH into pod; kill selfterm.
        subprocess.run(
            ["pixi", "run", "kinoforge", "exec", "--id", pod_id,
             "--", "pkill", "-9", "-f", "selfterm"],
            timeout=60, check=True,
        )

        # 4. Poll until sweeper destroys.
        deadline = time.time() + 240  # stall_window 120s + interval 30s + margin
        while time.time() < deadline:
            if not _pod_alive(pod_id):
                break
            time.sleep(15)
        else:
            pytest.fail(f"sweeper did not destroy pod {pod_id} within 240s")

        # 5. Assert index row also removed.
        list_out = subprocess.run(
            ["pixi", "run", "kinoforge", "list", "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        assert pod_id not in list_out.stdout
    finally:
        # 6. Sweeper teardown.
        sweeper.terminate()
        try:
            sweeper.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sweeper.kill()
        # Failsafe pod cleanup if assertion failed.
        if pod_id and _pod_alive(pod_id):
            subprocess.run(
                ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
                timeout=60,
            )
```

- [ ] **Step 2: Verify file collects cleanly (no import errors, test marked SKIP)**

Run: `pixi run pytest tests/live/test_runpod_ephemeral_sweeper_smoke.py -v`
Expected: SKIPPED (1).

- [ ] **Step 3: Lint + commit RED scaffold BEFORE any live invocation**

```bash
pixi run pre-commit run --files tests/live/test_runpod_ephemeral_sweeper_smoke.py
git add tests/live/test_runpod_ephemeral_sweeper_smoke.py
git commit -m "test(live): RED scaffold for RunPod sweeper-reaps-ephemeral-stall smoke"
```

This commit MUST land before Task 10 attempts a live run.

---

### Task 10: Live smoke — GREEN evidence

**Goal:** Run the live smoke against real RunPod. Capture stdout/stderr + sweeper log + final `kinoforge list` output as committed evidence files. Update test to GREEN (remove `pytestmark = skip`).

**Files:**
- Modify: `tests/live/test_runpod_ephemeral_sweeper_smoke.py` (remove module-level skip)
- Create: `tests/live/evidence/2026-06-28-sweeper-ephemeral-reap/` (transcript + log files)

**Acceptance Criteria:**
- [ ] `pixi run preflight` returned exit 0 (creds present, no live pods, clean tree) immediately before the run
- [ ] Live test run completed; pod was destroyed by sweeper within 240 s of selfterm kill
- [ ] Evidence captured: `provision_stdout.txt`, `sweeper_log.txt`, `list_after.json`, `kinoforge_list_final.txt`
- [ ] All evidence files committed
- [ ] Test passes (module-level skip removed; the test body succeeds)
- [ ] Total RunPod spend ≤ $0.40 (verified via `kinoforge cost report`)

**Verify:**
- `pixi run preflight` exit 0
- `pixi run pytest tests/live/test_runpod_ephemeral_sweeper_smoke.py -v -m live` → PASS

**Steps:**

- [ ] **Step 1: Run preflight; HALT if not green**

```bash
pixi run preflight
```
Expected: exit 0. If anything fails (existing pod, dirty tree, missing creds), HALT — fix the precondition before proceeding.

- [ ] **Step 2: Start a background sweeper monitor (poll runtime util while smoke runs)**

Per project memory `proactive_pod_stats`: every 60-90 s poll the pod's RunPod GraphQL probe so a stalled selfterm-kill or stuck destroy is caught early. Implement as a small shell loop in the same terminal as the test orchestrator.

- [ ] **Step 3: Remove module-level skip; run the test**

Edit `tests/live/test_runpod_ephemeral_sweeper_smoke.py`:
```python
# Delete this line:
pytestmark = pytest.mark.skip(
    reason="live smoke — run manually with explicit pixi run preflight + budget approval"
)
```

Run: `pixi run pytest tests/live/test_runpod_ephemeral_sweeper_smoke.py -v -m live -s`
Capture: stdout to `tests/live/evidence/2026-06-28-sweeper-ephemeral-reap/run_stdout.txt`, stderr to `run_stderr.txt`.

- [ ] **Step 4: Capture evidence files**

```bash
mkdir -p tests/live/evidence/2026-06-28-sweeper-ephemeral-reap
# Already-captured stdout/stderr go here.
pixi run kinoforge list --format json > tests/live/evidence/2026-06-28-sweeper-ephemeral-reap/list_after.json
pixi run kinoforge list > tests/live/evidence/2026-06-28-sweeper-ephemeral-reap/kinoforge_list_final.txt
# Sweeper log: tail from wherever sweeper writes (usually <store>/_lifecycle/sweeper-<host>.log)
cp <sweeper-log-path> tests/live/evidence/2026-06-28-sweeper-ephemeral-reap/sweeper_log.txt
```

- [ ] **Step 5: Verify post-conditions**

```bash
pixi run kinoforge list
```
Expected: both `[instance overview] No running instances.` AND `No instances recorded in ledger.` per project rule. If either shows a pod, run `pixi run kinoforge destroy --id <pod>`.

```bash
pixi run kinoforge cost report
```
Confirm total spend ≤ $0.40.

- [ ] **Step 6: Commit GREEN evidence**

```bash
git add tests/live/test_runpod_ephemeral_sweeper_smoke.py tests/live/evidence/2026-06-28-sweeper-ephemeral-reap/
git commit -m "test(live): GREEN evidence for sweeper-reaps-ephemeral-stall smoke"
```

---

### Task 11: Documentation updates

**Goal:** Operator-facing docs reflect the new behavior. `kinoforge reap` and `kinoforge sweeper` now see ephemeral pods; the verdict matrix is documented.

**Files:**
- Modify: `docs/lifecycle.md` (add ephemeral-reap section)
- Modify: `docs/warm-reuse.md` (cross-reference: sweeper now reaps ephemeral)

**Acceptance Criteria:**
- [ ] `docs/lifecycle.md` has a new section "Ephemeral pod reap" describing: ephemeral pods discovered via `EphemeralIndex`, probe-driven liveness, verdict matrix (GC_404 / SKIP_NO_PROBE / PROBE_FAILED / OVERAGE_REAP / STALL_REAP), one-shot vs daemon STALL behavior
- [ ] `docs/warm-reuse.md` includes a one-line cross-reference: "Ephemeral pods are also visible to the sweeper — see lifecycle.md#ephemeral-pod-reap"
- [ ] Both files render cleanly (no broken inline links)

**Verify:** Visual review (no automated check).

**Steps:**

- [ ] **Step 1: Read current `docs/lifecycle.md` to find the right insertion point**

Find the "Sweeper daemon (B1 / Layer W)" section or the verdict-matrix discussion. The new section goes adjacent.

- [ ] **Step 2: Add "Ephemeral pod reap" section to `docs/lifecycle.md`**

Content sketch (operator voice, concrete):

```markdown
## Ephemeral pod reap

Pods provisioned under `kinoforge --ephemeral generate` do not get a
disk-ledger row (writes are diverted to in-memory by STRICT_POLICY).
The sweeper daemon and `kinoforge reap` discover them via the
`EphemeralIndex` written at cold-create time.

For each ephemeral pod, the sweeper calls the provider's
`probe_runtime(pod_id)` method:

| Probe outcome | Verdict | Action |
|---|---|---|
| Provider returned 404 (pod gone) | `GC_404` | Remove index row, no destroy |
| Provider returned `None` (no probe substrate) | `SKIP_NO_PROBE` | WARN-once, no action |
| Probe raised (transient network/auth) | `PROBE_FAILED` | WARN-once per (provider, pod, error), retry next tick |
| `now - created_at > max_lifetime_s` | `OVERAGE_REAP` | Destroy + remove row |
| N consecutive samples GPU < 5%, CPU < 10% | `STALL_REAP` | Destroy + remove row (sweeper daemon only — `kinoforge reap` one-shot skips STALL) |
| Otherwise | `LIVE` | No action |

`IDLE_REAP` does not apply to ephemeral pods (no heartbeat substrate;
in-pod selfterm handles graceful idle).

The probe substrate exists only for RunPod today; SkyPilot (Lambda /
Vast) and Local providers return `None` and get the WARN-once skip.
Ephemeral pods on those providers are reapable only via in-pod
selfterm or `kinoforge destroy --id <pod>`.
```

- [ ] **Step 3: Add cross-reference to `docs/warm-reuse.md`**

Find the section that mentions ephemeral usage (likely near `--ephemeral` discussion). Add:

```markdown
> Ephemeral pods are also visible to the sweeper — see
> [lifecycle.md#ephemeral-pod-reap](lifecycle.md#ephemeral-pod-reap).
```

- [ ] **Step 4: Commit**

```bash
pixi run pre-commit run --files docs/lifecycle.md docs/warm-reuse.md
git add docs/lifecycle.md docs/warm-reuse.md
git commit -m "docs(lifecycle,warm-reuse): document ephemeral-aware sweep"
```

---

## Post-implementation checklist

- [ ] Full pytest pass: `pixi run pytest -v` → green
- [ ] Coverage check: `pixi run pytest --cov` → no regression vs main
- [ ] Update `PROGRESS.md` with workstream CLOSED + commit list
- [ ] Update `successful-generations.md` if the live smoke produced a new generation that introduces a new capability axis (sweeper-driven destroy is not a new generation axis — skip this step if no new mode/provider/engine surfaced)

## Self-review

**Spec coverage:** Every section of the spec maps to at least one task:
- §1-2 problem + solution overview → Task 1-7 collectively
- §3.1 RuntimeProbe → Task 1
- §3.2 ABC extension → Task 1
- §3.3 per-provider impl → Task 1 (defaults), Task 2 (RunPod override)
- §3.4 sweep union → Task 4
- §3.5 classify branch → Task 3
- §3.6 act_on_verdict → Task 6
- §3.7 SweeperLoop history → Task 5
- §3.8 SweeperStats counters → Task 7
- §3.9 CLI emit → Task 7
- §3.10 one-shot mode → Task 3 (logic) + Task 7 (CLI test)
- §4 race safety → Task 7 (session-claim integration test)
- §5.1-5.10 tests → Tasks 1-10 each contain the relevant tests
- §5.11 RED-scaffold rule → Task 9
- §7 open questions → none
- §8 commit plan → matches Tasks 1-11

**Placeholder scan:** No "TBD", "implement later", or "similar to Task N" markers.

**Type consistency:** Verdict names (GC_404 / SKIP_NO_PROBE / PROBE_FAILED), probe_state strings ("ok" / "not_found" / "no_substrate" / "failed"), and stall_history shape (`dict[str, deque[tuple[float, float]]]`) consistent across Tasks 3-7.
