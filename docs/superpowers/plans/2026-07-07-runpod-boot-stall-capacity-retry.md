# RunPod Boot-Stall Fast-Fail + Capacity-Retry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kill the 900s dead-boot wait (fail fast when a pod's boot is provably dead) and ride out transient RunPod capacity droughts, so the FlashVSR pipeline fails cheaply-and-honestly or succeeds instead of hanging 15 minutes / failing on the first capacity miss.

**Architecture:** Two independent components behind seams that default to today's behavior. (1) A `BootLivenessProbe` injected into `wait_for_ready` — a pure `classify_boot_liveness` decision fed by the RunPod util probe + `bootstrap.log` tail; aborts on GONE/STALLED. (2) A capacity-wait retry loop wrapping `find_offers`+create in the orchestrator, keyed on the existing `CapacityError` (RunPod's create-error classifier is extended to raise it for the "no instances available" variants), bounded by a new `lifecycle.capacity_wait` cfg field.

**Tech Stack:** Python 3.13, pydantic config, pytest. Existing kinoforge `core/config.py`, `core/orchestrator.py`, `core/interfaces.py`, `providers/runpod/`, `engines/diffusers` + `engines/comfyui` `wait_for_ready`, `core/util_endpoints.UtilSnapshot`.

**User decisions (already made):**
- Scope = "Fast-fail + capacity (#1+#3)" — NO auto-reprovision/self-heal. (quoted appetite choice)
- Boot-stall signal = "Flatline + bootstrap-trap": abort on `[bootstrap-trap] rc!=0` OR util flatline (CPU 0% + flat mem + no disk growth) for K probes, behind a grace window.
- Capacity retry = "Configurable deadline": bounded-wait loop with the deadline as a `lifecycle.capacity_wait` duration-string cfg field (default `5m`), matching `boot_timeout` siblings. No CLI flag (YAGNI).
- Reuse existing `CapacityError` (do NOT add a new error type) — extend the RunPod create-error match.
- Widen `examples/configs/upscale-flashvsr-1080p.yaml` only (Ampere/Hopper 80GB, cap $3.00); skip Blackwell RTX-6000 (BSA wheel is cu124).

---

## File Structure

- **New:** `src/kinoforge/core/boot_liveness.py` — `BootVerdict` enum, `BootLivenessProbe` protocol, `BootLivenessResult`, and the pure `classify_boot_liveness(...)` decision function. One responsibility: decide ALIVE/GONE/STALLED/UNKNOWN from raw signals. No network.
- **New:** `tests/core/test_boot_liveness.py`.
- **Modify:** `src/kinoforge/core/config.py` — add `capacity_wait` to `LifecycleConfig` + duration validator + map to `InterfaceLifecycle`.
- **Modify:** `src/kinoforge/core/interfaces.py` — add `capacity_wait_s` to the `Lifecycle` dataclass.
- **Modify:** `src/kinoforge/providers/runpod/__init__.py` — extend `_create_pod` capacity match → `CapacityError`; add `RunPodBootLivenessProbe` + `make_boot_liveness_probe(instance)`.
- **Modify:** `src/kinoforge/core/orchestrator.py` — capacity-wait retry loop around `find_offers`+create; attach the boot probe to the engine.
- **Modify:** `src/kinoforge/engines/diffusers/__init__.py` + `src/kinoforge/engines/comfyui/__init__.py` — `attach_boot_liveness_probe` setter + consult it in `wait_for_ready`; map `get_instance` KeyError → GONE.
- **Modify:** `examples/configs/upscale-flashvsr-1080p.yaml` — widened prefs/cap + `capacity_wait`.
- **Tests:** `tests/core/test_boot_liveness.py`, `tests/providers/test_runpod_boot_liveness.py`, `tests/providers/test_runpod_capacity_error.py`, `tests/core/test_capacity_wait_retry.py`, `tests/core/test_config.py` (extend), `tests/engines/test_diffusers_wait_for_ready_boot_stall.py`.

---

## Task 0: `lifecycle.capacity_wait` config field

**Goal:** A `capacity_wait` duration field on the lifecycle config, parsed like `boot_timeout`, surfaced as `Lifecycle.capacity_wait_s` (default 300s).

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (`Lifecycle` dataclass, ~line 24-37)
- Modify: `src/kinoforge/core/config.py` (`LifecycleConfig` ~90-142; `lifecycle()` map ~1365-1385)
- Test: `tests/core/test_config.py` (extend)

**Acceptance Criteria:**
- [ ] `LifecycleConfig(budget=1.0, capacity_wait="5m").capacity_wait == 300.0` (duration string parses).
- [ ] `LifecycleConfig(budget=1.0).capacity_wait == 300.0` (default).
- [ ] `cfg.lifecycle().capacity_wait_s` reflects the configured value.
- [ ] `Lifecycle().capacity_wait_s == 300.0` (interface default).

**Verify:** `pixi run pytest tests/core/test_config.py -k capacity_wait -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing test** — append to `tests/core/test_config.py`:

```python
def test_capacity_wait_parses_duration_and_defaults() -> None:
    """capacity_wait accepts a duration string, defaults to 300s, maps to the interface.

    Bug caught: the field is added as a bare int (no duration parse) so
    `capacity_wait: 5m` in YAML raises, or the default drifts from 300s.
    """
    from kinoforge.core.config import LifecycleConfig

    assert LifecycleConfig(budget=1.0).capacity_wait == 300.0
    assert LifecycleConfig(budget=1.0, capacity_wait="5m").capacity_wait == 300.0
    assert LifecycleConfig(budget=1.0, capacity_wait=0).capacity_wait == 0.0


def test_capacity_wait_surfaces_on_interface_lifecycle() -> None:
    """cfg.lifecycle().capacity_wait_s carries the configured value.

    Bug caught: the LifecycleConfig field exists but isn't threaded into the
    InterfaceLifecycle the orchestrator actually reads.
    """
    from kinoforge.core.interfaces import Lifecycle

    assert Lifecycle().capacity_wait_s == 300.0
```

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/core/test_config.py -k capacity_wait -v` → Expected: FAIL (`unexpected keyword argument 'capacity_wait'` / `no attribute capacity_wait_s`).

- [ ] **Step 3: Add the interface field.** In `src/kinoforge/core/interfaces.py`, inside the `Lifecycle` dataclass (after `boot_timeout_s: float = 900.0`, ~line 31):

```python
    #: Max seconds to keep retrying create on a RunPod capacity miss before
    #: giving up (2026-07-07). 0 = fail on the first miss.
    capacity_wait_s: float = 300.0
```

- [ ] **Step 4: Add the config field + validator.** In `src/kinoforge/core/config.py` `LifecycleConfig` (after `boot_timeout: float = 900.0`, ~line 112):

```python
    capacity_wait: float = 300.0
```

Add `"capacity_wait"` to the `@field_validator(...)` duration list (the decorator at ~line 129 listing `"idle_timeout", "job_timeout", "time_buffer", "max_lifetime", "boot_timeout"`) so it becomes:

```python
    @field_validator(
        "idle_timeout",
        "job_timeout",
        "time_buffer",
        "max_lifetime",
        "boot_timeout",
        "capacity_wait",
        mode="before",
    )
```

- [ ] **Step 5: Map into the interface Lifecycle.** In `config.py` `lifecycle()` (~1380, the `return InterfaceLifecycle(...)` call), add the kwarg:

```python
            capacity_wait_s=lc.capacity_wait,
```

(Match the local variable name used for the `LifecycleConfig` in that method — read lines ~1365-1385 first; it may be `lc` or `self.lifecycle_cfg`. Use whatever the sibling `boot_timeout_s=...` line uses.)

- [ ] **Step 6: Run tests** — Run: `pixi run pytest tests/core/test_config.py -k capacity_wait -v` → Expected: PASS.

- [ ] **Step 7: Guard regression** — Run: `pixi run pytest tests/core/test_config.py -q` → Expected: PASS (existing lifecycle tests unaffected).

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "feat(config): lifecycle.capacity_wait duration field (default 5m) -> Lifecycle.capacity_wait_s"
```

---

## Task 1: RunPod classifies "no instances available" as `CapacityError`

**Goal:** `_create_pod` raises the existing `CapacityError` (not a raw `ValueError`) for the two "no longer any instances available" variants, so the offer-retry + capacity-wait loop can catch them.

**Files:**
- Modify: `src/kinoforge/providers/runpod/__init__.py` (`_create_pod` capacity match, ~line 899-907)
- Test: `tests/providers/test_runpod_capacity_error.py` (new)

**Acceptance Criteria:**
- [ ] A create response whose error message contains "no longer any instances available with the requested specifications" raises `CapacityError`.
- [ ] The "…with enough disk space" variant also raises `CapacityError`.
- [ ] The pre-existing "resources to deploy" variant still raises `CapacityError`.
- [ ] A non-capacity GraphQL error (e.g. "bad field") still raises the raw `ValueError`.

**Verify:** `pixi run pytest tests/providers/test_runpod_capacity_error.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing test** — `tests/providers/test_runpod_capacity_error.py`:

```python
"""RunPod _create_pod classifies capacity-exhaustion messages as CapacityError."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import CapacityError
from kinoforge.core.interfaces import InstanceSpec, Offer
from kinoforge.providers.runpod import RunPodProvider


def _spec() -> InstanceSpec:
    return InstanceSpec(
        image="img",
        offer=Offer(
            id="NVIDIA A100 80GB PCIe",
            gpu_type="NVIDIA A100 80GB PCIe",
            vram_gb=80,
            cuda="12.8",
            cost_rate_usd_per_hr=1.19,
            mode="pod",
        ),
        ports=(8000,),
        env={},
        run_id="r",
        provision_script="#!/bin/sh\ntrue\n",
    )


def _provider_returning(error_message: str) -> RunPodProvider:
    def fake_post(_url: str, _body: dict[str, Any]) -> dict[str, Any]:
        return {"errors": [{"message": error_message}]}

    return RunPodProvider(http_post=fake_post)


@pytest.mark.parametrize(
    "msg",
    [
        "There are no longer any instances available with the requested specifications. Please refresh and try again.",
        "There are no longer any instances available with enough disk space.",
        "There are no resources to deploy for this request.",
    ],
)
def test_capacity_messages_raise_capacity_error(msg: str) -> None:
    # Bug caught: the "no longer any instances available" variants fell through
    # to a raw ValueError, so _create_with_offer_retry (which catches only
    # CapacityError) never retried and the run died on the first miss.
    provider = _provider_returning(msg)
    with pytest.raises(CapacityError):
        provider.create_instance(_spec())


def test_non_capacity_error_stays_value_error() -> None:
    # Bug caught: over-broad match swallows real create failures (bad schema,
    # auth) as retryable capacity misses, hiding a hard error behind a 5min wait.
    provider = _provider_returning("Field 'bogus' is not defined in the input type.")
    with pytest.raises(ValueError) as exc_info:
        provider.create_instance(_spec())
    assert not isinstance(exc_info.value, CapacityError)
```

Note: `RunPodProvider(http_post=fake_post)` uses the injectable POST seam (constructor accepts `http_post`). If `create_instance` requires more of the spec than shown, read `_create_pod` and add the minimal fields — do NOT stub past the capacity branch.

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/providers/test_runpod_capacity_error.py -v` → Expected: FAIL (the two "no longer any instances available" cases raise `ValueError`, not `CapacityError`).

- [ ] **Step 3: Extend the match.** In `src/kinoforge/providers/runpod/__init__.py` `_create_pod`, the existing block (~line 903):

```python
            joined_lower = "\n".join(error_msgs).lower()
            if "resources to deploy" in joined_lower:
                raise CapacityError(
                    f"RunPod has no current capacity for {gpu_type_id!r}: {assembled}"
                ) from value_error
            raise value_error
```

becomes:

```python
            joined_lower = "\n".join(error_msgs).lower()
            # Capacity exhaustion has three observed phrasings; all mean "the
            # offer find_offers listed is gone by create time" and are transient
            # (2026-07-07). Classify them so _create_with_offer_retry + the
            # capacity-wait loop retry instead of failing the whole run.
            _CAPACITY_MARKERS = (
                "resources to deploy",
                "no longer any instances available",
            )
            if any(marker in joined_lower for marker in _CAPACITY_MARKERS):
                raise CapacityError(
                    f"RunPod has no current capacity for {gpu_type_id!r}: {assembled}"
                ) from value_error
            raise value_error
```

- [ ] **Step 4: Run tests** — Run: `pixi run pytest tests/providers/test_runpod_capacity_error.py -v` → Expected: PASS.

- [ ] **Step 5: Guard regression** — Run: `pixi run pytest tests/providers/ -k "capacity or create" -q` → Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/providers/runpod/__init__.py tests/providers/test_runpod_capacity_error.py
git commit -m "fix(runpod): classify 'no longer any instances available' create errors as CapacityError"
```

---

## Task 2: Capacity-wait retry loop in the orchestrator

**Goal:** On `CapacityError` (empty offers OR all offers exhausted), re-query `find_offers` and retry create every 25s until `capacity_wait_s`, then re-raise clean.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (`_provision_instance_and_build_backend`, ~679-748; add a module const + a small retry helper)
- Test: `tests/core/test_capacity_wait_retry.py` (new)

**Acceptance Criteria:**
- [ ] A `find_offers`/create sequence that raises `CapacityError` twice then succeeds returns the instance, having re-queried `find_offers` each attempt.
- [ ] `capacity_wait_s=0` → the first `CapacityError` propagates (no retry).
- [ ] When the deadline elapses with sustained `CapacityError`, the last `CapacityError` re-raises.
- [ ] A non-`CapacityError` from create propagates immediately (no retry).
- [ ] Clock + sleep are injected so the test runs instantly.

**Verify:** `pixi run pytest tests/core/test_capacity_wait_retry.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing test** — `tests/core/test_capacity_wait_retry.py`:

```python
"""Capacity-wait retry: re-query offers + retry create on CapacityError."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import CapacityError
from kinoforge.core.orchestrator import _create_with_capacity_wait


class _Clock:
    def __init__(self, times: list[float]) -> None:
        self._times = times
        self._i = 0

    def now(self) -> float:
        t = self._times[min(self._i, len(self._times) - 1)]
        self._i += 1
        return t


def test_retries_then_succeeds() -> None:
    # Bug caught: a transient capacity miss fails the whole run instead of
    # riding the ~seconds-to-minutes drought RunPod recovers from.
    query_calls = {"n": 0}

    def find_offers() -> list[str]:
        query_calls["n"] += 1
        return ["offer"]  # non-empty

    attempts = {"n": 0}

    def create(_offers: list[str]) -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise CapacityError("no capacity")
        return "instance-ok"

    result = _create_with_capacity_wait(
        find_offers=find_offers,
        create=create,
        capacity_wait_s=300.0,
        retry_interval_s=25.0,
        clock=_Clock([0.0, 10.0, 20.0, 30.0]),
        sleep=lambda _s: None,
    )
    assert result == "instance-ok"
    assert attempts["n"] == 3
    assert query_calls["n"] == 3  # re-queried offers each attempt


def test_zero_wait_fails_on_first_miss() -> None:
    # Bug caught: capacity_wait=0 (smoke) still hangs retrying.
    def create(_offers: list[str]) -> str:
        raise CapacityError("no capacity")

    with pytest.raises(CapacityError):
        _create_with_capacity_wait(
            find_offers=lambda: ["offer"],
            create=create,
            capacity_wait_s=0.0,
            retry_interval_s=25.0,
            clock=_Clock([0.0, 1.0]),
            sleep=lambda _s: None,
        )


def test_deadline_exceeded_reraises() -> None:
    # Bug caught: an infinite loop when capacity never returns.
    def create(_offers: list[str]) -> str:
        raise CapacityError("still no capacity")

    with pytest.raises(CapacityError):
        _create_with_capacity_wait(
            find_offers=lambda: ["offer"],
            create=create,
            capacity_wait_s=60.0,
            retry_interval_s=25.0,
            clock=_Clock([0.0, 30.0, 61.0, 62.0]),
            sleep=lambda _s: None,
        )


def test_non_capacity_error_propagates() -> None:
    # Bug caught: a hard create error (auth/schema) is swallowed as retryable.
    def create(_offers: list[str]) -> str:
        raise RuntimeError("bad schema")

    with pytest.raises(RuntimeError):
        _create_with_capacity_wait(
            find_offers=lambda: ["offer"],
            create=create,
            capacity_wait_s=300.0,
            retry_interval_s=25.0,
            clock=_Clock([0.0, 10.0]),
            sleep=lambda _s: None,
        )
```

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/core/test_capacity_wait_retry.py -v` → Expected: FAIL (`_create_with_capacity_wait` undefined).

- [ ] **Step 3: Add the helper.** In `src/kinoforge/core/orchestrator.py`, near `_create_with_offer_retry` (~line 500), add a module const and the generic helper (generic so it unit-tests without provider/InstanceSpec machinery):

```python
_CAPACITY_RETRY_INTERVAL_S: float = 25.0


def _create_with_capacity_wait[T](
    *,
    find_offers: Callable[[], list[Any]],
    create: Callable[[list[Any]], T],
    capacity_wait_s: float,
    retry_interval_s: float = _CAPACITY_RETRY_INTERVAL_S,
    clock: Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry ``find_offers`` + ``create`` while it raises CapacityError.

    Capacity is fluid: a RunPod offer listed by find_offers can vanish by
    create time, and a currently-empty pool can free up seconds later. Retry
    the whole find+create on CapacityError, re-querying offers each attempt,
    until ``capacity_wait_s`` elapses; then re-raise the last CapacityError.
    Non-CapacityError propagates immediately. ``capacity_wait_s <= 0`` fails on
    the first miss.

    Args:
        find_offers: Re-queries provider offers (fresh each attempt).
        create: Builds the instance from offers; may raise CapacityError.
        capacity_wait_s: Deadline; 0 disables retry.
        retry_interval_s: Sleep between attempts.
        clock: Injected clock (defaults to RealClock).
        sleep: Injected sleep seam.

    Returns:
        The created instance (whatever ``create`` returns).

    Raises:
        CapacityError: Deadline elapsed with sustained capacity exhaustion.
    """
    the_clock = clock if clock is not None else RealClock()
    start = the_clock.now()
    last_exc: CapacityError | None = None
    while True:
        try:
            return create(find_offers())
        except CapacityError as exc:
            last_exc = exc
            if the_clock.now() - start >= capacity_wait_s:
                raise
            _log.warning(
                "[capacity-wait] no capacity yet; retry in %.0fs (waited %.0fs / %.0fs)",
                retry_interval_s,
                the_clock.now() - start,
                capacity_wait_s,
            )
            sleep(retry_interval_s)
```

Confirm `Clock` and `RealClock` are already imported in orchestrator.py (they are used elsewhere); if not, add `from kinoforge.core.clock import Clock, RealClock`.

- [ ] **Step 4: Run tests** — Run: `pixi run pytest tests/core/test_capacity_wait_retry.py -v` → Expected: PASS.

- [ ] **Step 5: Wire into `_provision_instance_and_build_backend`.** Replace the current sequence (`orchestrator.py` ~679-748):

```python
    hw_reqs = cfg.hardware_requirements()
    offers = resolved_provider.find_offers(hw_reqs)
    ...
    instance, _chosen_offer = _create_with_offer_retry(
        resolved_provider, _build_spec, offers
    )
```

so the find+create runs inside the capacity-wait loop. Read the exact lines first (the `_build_spec` closure sits between). The minimal change: keep `_build_spec` as-is; wrap the offer query + create:

```python
    hw_reqs = cfg.hardware_requirements()
    capacity_wait_s = cfg.lifecycle().capacity_wait_s

    def _find_offers() -> list[Offer]:
        found = resolved_provider.find_offers(hw_reqs)
        if not found:
            raise CapacityError(
                f"provider {getattr(resolved_provider, 'name', '?')!r} returned "
                f"no offers for {hw_reqs!r}"
            )
        return found

    def _create(offers: list[Offer]) -> tuple[Instance, Offer]:
        return _create_with_offer_retry(resolved_provider, _build_spec, offers)

    instance, _chosen_offer = _create_with_capacity_wait(
        find_offers=_find_offers,
        create=_create,
        capacity_wait_s=capacity_wait_s,
    )
```

Delete the now-redundant standalone `offers = resolved_provider.find_offers(...)` line and the standalone `_create_with_offer_retry(...)` call they replace. Preserve the existing empty-offers `CapacityError` message contract (the docstring at ~671 says "`find_offers` returned an empty list" raises `CapacityError` — `_find_offers` above keeps that). `Instance` and `Offer` are already imported in orchestrator.py.

- [ ] **Step 6: Guard the wiring** — Run: `pixi run pytest tests/core/ -k "provision or orchestrat or capacity" -q` → Expected: PASS (existing provision tests still green; empty-offers still raises CapacityError).

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_capacity_wait_retry.py
git commit -m "feat(orchestrator): capacity-wait retry loop — re-query offers + retry create until lifecycle.capacity_wait"
```

---

## Task 3: Pure boot-liveness decision (`core/boot_liveness.py`)

**Goal:** A pure `classify_boot_liveness(...)` that returns a `BootVerdict` + updated flatline counter from raw signals — no network, fully unit-tested. Plus the `BootVerdict` enum + `BootLivenessProbe` protocol.

**Files:**
- Create: `src/kinoforge/core/boot_liveness.py`
- Test: `tests/core/test_boot_liveness.py` (new)

**Acceptance Criteria:**
- [ ] `exists=False` → `GONE` (counter reset to 0), regardless of grace/elapsed.
- [ ] `log_tail` containing `[bootstrap-trap] rc=1` → `STALLED`.
- [ ] `log_tail` containing `[bootstrap-trap] rc=0` → NOT stalled (ALIVE/UNKNOWN).
- [ ] `elapsed_s < grace_s` → never `STALLED` (returns ALIVE, counter 0), even on flatline.
- [ ] After grace: CPU 0% + mem delta ≈0 + (disk None or delta ≈0) increments the counter; reaching `consecutive_needed` → `STALLED`.
- [ ] Any progress (CPU>0, or mem/disk growing) → ALIVE + counter reset to 0.
- [ ] `snap is None` (probe error) → `UNKNOWN`, counter unchanged.

**Verify:** `pixi run pytest tests/core/test_boot_liveness.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** — `tests/core/test_boot_liveness.py`:

```python
"""Pure boot-liveness verdict logic — no network, fakes only."""

from __future__ import annotations

from kinoforge.core.boot_liveness import BootVerdict, classify_boot_liveness
from kinoforge.core.util_endpoints import UtilSnapshot


def _snap(cpu: float, mem: float, disk: float | None = None) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=0.0,
        cpu_percent=cpu,
        memory_percent=mem,
        disk_percent=disk,
        uptime_seconds=120,
    )


def _classify(**kw):
    base = dict(
        exists=True,
        log_tail=None,
        snap=_snap(0.0, 5.0),
        prev_snap=_snap(0.0, 5.0),
        consecutive_flat=0,
        elapsed_s=300.0,
        grace_s=90.0,
        consecutive_needed=3,
    )
    base.update(kw)
    return classify_boot_liveness(**base)


def test_gone_when_not_exists() -> None:
    # Bug caught: a reclaimed pod is not detected during boot and waits 900s.
    r = _classify(exists=False, consecutive_flat=2)
    assert r.verdict is BootVerdict.GONE
    assert r.consecutive_flat == 0


def test_trap_nonzero_is_stalled() -> None:
    # Bug caught: provision script crashed under the trap (rc!=0) but wait_for_ready
    # keeps polling /health for the full boot_timeout.
    r = _classify(log_tail="... \n[bootstrap-trap] rc=1 at 2026-07-07T00:00:00Z\n")
    assert r.verdict is BootVerdict.STALLED


def test_trap_zero_is_not_stalled() -> None:
    # Bug caught: rc=0 (provision succeeded, server coming up) misread as dead.
    r = _classify(log_tail="[bootstrap-trap] rc=0 at 2026-07-07T00:00:00Z\n")
    assert r.verdict is not BootVerdict.STALLED


def test_grace_window_suppresses_flatline() -> None:
    # Bug caught: early-boot quiet trips a false STALLED before the pod has had
    # time to start downloading.
    r = _classify(
        elapsed_s=30.0,
        snap=_snap(0.0, 5.0),
        prev_snap=_snap(0.0, 5.0),
        consecutive_flat=2,
    )
    assert r.verdict is not BootVerdict.STALLED
    assert r.consecutive_flat == 0


def test_flatline_reaches_threshold_stalls() -> None:
    # Bug caught: a hung boot (CPU 0, mem flat, disk flat) is never declared dead.
    r = _classify(
        snap=_snap(0.0, 5.0, disk=40.0),
        prev_snap=_snap(0.0, 5.0, disk=40.0),
        consecutive_flat=2,
    )
    assert r.verdict is BootVerdict.STALLED
    assert r.consecutive_flat == 3


def test_progress_resets_counter() -> None:
    # Bug caught: a slow-but-healthy download (disk growing) is killed as stalled.
    r = _classify(
        snap=_snap(0.0, 5.0, disk=42.0),
        prev_snap=_snap(0.0, 5.0, disk=40.0),
        consecutive_flat=2,
    )
    assert r.verdict is BootVerdict.ALIVE
    assert r.consecutive_flat == 0


def test_cpu_active_is_progress() -> None:
    r = _classify(snap=_snap(13.0, 5.0), prev_snap=_snap(0.0, 5.0), consecutive_flat=2)
    assert r.verdict is BootVerdict.ALIVE
    assert r.consecutive_flat == 0


def test_snap_none_is_unknown() -> None:
    # Bug caught: a transient util-probe error is treated as flatline → false kill.
    r = _classify(snap=None, consecutive_flat=1)
    assert r.verdict is BootVerdict.UNKNOWN
    assert r.consecutive_flat == 1
```

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/core/test_boot_liveness.py -v` → Expected: FAIL (module missing).

- [ ] **Step 3: Implement** `src/kinoforge/core/boot_liveness.py`:

```python
"""Boot-liveness classification — decide if a booting pod is dead.

Pure decision logic (no network) plus the probe protocol. A live-but-dead
server (bootstrap crashed under its trap, or a hung download) otherwise burns
the full boot_timeout (900s); this lets wait_for_ready bail in ~2-3min.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from kinoforge.core.util_endpoints import UtilSnapshot

#: Percent-point epsilon below which a mem/disk delta counts as "flat".
_FLAT_EPS: float = 0.5

_TRAP_RE = re.compile(r"\[bootstrap-trap\]\s+rc=(\d+)")


class BootVerdict(StrEnum):
    ALIVE = "ALIVE"        # progressing or indeterminate-but-present → keep waiting
    GONE = "GONE"          # pod reclaimed → abort
    STALLED = "STALLED"    # provision script died / util flatline → abort
    UNKNOWN = "UNKNOWN"    # probe error → treat as ALIVE (never a false abort)


@dataclass(frozen=True)
class BootLivenessResult:
    verdict: BootVerdict
    consecutive_flat: int


class BootLivenessProbe(Protocol):
    """Stateful per-provision probe consulted by wait_for_ready."""

    def check(self, instance_id: str) -> BootVerdict:  # noqa: D102
        ...


def _last_trap_rc(log_tail: str | None) -> int | None:
    """Return the rc of the last ``[bootstrap-trap] rc=N`` line, or None."""
    if not log_tail:
        return None
    matches = _TRAP_RE.findall(log_tail)
    return int(matches[-1]) if matches else None


def _is_flat(snap: UtilSnapshot, prev: UtilSnapshot) -> bool:
    """True iff CPU is 0 AND mem is flat AND disk is flat/unknown."""
    if snap.cpu_percent > 0.0:
        return False
    if abs(snap.memory_percent - prev.memory_percent) >= _FLAT_EPS:
        return False
    if snap.disk_percent is not None and prev.disk_percent is not None:
        if abs(snap.disk_percent - prev.disk_percent) >= _FLAT_EPS:
            return False
    return True


def classify_boot_liveness(
    *,
    exists: bool,
    log_tail: str | None,
    snap: UtilSnapshot | None,
    prev_snap: UtilSnapshot | None,
    consecutive_flat: int,
    elapsed_s: float,
    grace_s: float,
    consecutive_needed: int,
) -> BootLivenessResult:
    """Decide the boot verdict from raw signals. See module docstring.

    Precedence: GONE (unambiguous) > trap-rc!=0 (ground truth) > grace window
    (suppress) > util flatline (counted) > progress (reset) > unknown.
    """
    if not exists:
        return BootLivenessResult(BootVerdict.GONE, 0)

    rc = _last_trap_rc(log_tail)
    if rc is not None and rc != 0:
        return BootLivenessResult(BootVerdict.STALLED, consecutive_flat)

    if elapsed_s < grace_s:
        return BootLivenessResult(BootVerdict.ALIVE, 0)

    if snap is None:
        return BootLivenessResult(BootVerdict.UNKNOWN, consecutive_flat)

    if prev_snap is not None and _is_flat(snap, prev_snap):
        n = consecutive_flat + 1
        if n >= consecutive_needed:
            return BootLivenessResult(BootVerdict.STALLED, n)
        return BootLivenessResult(BootVerdict.ALIVE, n)

    return BootLivenessResult(BootVerdict.ALIVE, 0)
```

- [ ] **Step 4: Run tests** — Run: `pixi run pytest tests/core/test_boot_liveness.py -v` → Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/boot_liveness.py tests/core/test_boot_liveness.py
git commit -m "feat(core): pure boot-liveness classifier (trap-rc + util flatline + grace) + probe protocol"
```

---

## Task 4: RunPod `BootLivenessProbe` implementation

**Goal:** A stateful RunPod probe whose `check(id)` fetches the util snapshot + `bootstrap.log` tail, feeds them to `classify_boot_liveness`, and tracks prior snapshot + flatline counter across calls.

**Files:**
- Modify: `src/kinoforge/providers/runpod/__init__.py` (add `RunPodBootLivenessProbe` + `make_boot_liveness_probe`)
- Test: `tests/providers/test_runpod_boot_liveness.py` (new)

**Acceptance Criteria:**
- [ ] `check()` returns `GONE` when the util endpoint's `probe()` reports the pod absent.
- [ ] `check()` returns `STALLED` when the fetched `bootstrap.log` tail has `[bootstrap-trap] rc=1`.
- [ ] `check()` returns `STALLED` after `consecutive_needed` flatline snapshots (post-grace).
- [ ] `check()` returns `ALIVE`/`UNKNOWN` (never raises) when the log fetch or util read errors.
- [ ] The probe uses an injected clock so elapsed/grace is testable.

**Verify:** `pixi run pytest tests/providers/test_runpod_boot_liveness.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** — `tests/providers/test_runpod_boot_liveness.py`:

```python
"""RunPodBootLivenessProbe: util + bootstrap.log → BootVerdict."""

from __future__ import annotations

from kinoforge.core.boot_liveness import BootVerdict
from kinoforge.core.util_endpoints import UtilSnapshot
from kinoforge.providers.runpod import RunPodBootLivenessProbe


class _Clock:
    def __init__(self, times: list[float]) -> None:
        self._t = times
        self._i = 0

    def now(self) -> float:
        v = self._t[min(self._i, len(self._t) - 1)]
        self._i += 1
        return v


class _UtilEP:
    """Stub util endpoint: probe()->(exists, snap), read_util()->snap."""

    def __init__(self, exists: bool, snap: UtilSnapshot | None) -> None:
        self._exists = exists
        self._snap = snap

    def probe(self, _iid: str) -> tuple[bool, UtilSnapshot | None]:
        return (self._exists, self._snap)


def _snap(cpu: float, mem: float, disk: float | None = None) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=0.0, cpu_percent=cpu, memory_percent=mem,
        disk_percent=disk, uptime_seconds=120,
    )


def _probe(*, exists=True, snap=None, log_tail="", clock_times=None):
    return RunPodBootLivenessProbe(
        instance_id="pod1",
        util_endpoint=_UtilEP(exists, snap),
        fetch_bootstrap_log=lambda _iid: log_tail,
        grace_s=90.0,
        consecutive_needed=3,
        clock=_Clock(clock_times or [0.0, 100.0, 200.0, 300.0]),
    )


def test_gone_when_probe_absent() -> None:
    # Bug caught: reclaimed-during-boot pod not detected → 900s wait.
    p = _probe(exists=False, snap=_snap(0.0, 5.0))
    assert p.check("pod1") is BootVerdict.GONE


def test_trap_nonzero_stalled() -> None:
    # Bug caught: crashed provision script (rc!=0) not detected.
    p = _probe(snap=_snap(0.0, 5.0), log_tail="[bootstrap-trap] rc=1 at T\n")
    assert p.check("pod1") is BootVerdict.STALLED


def test_flatline_across_calls_stalls() -> None:
    # Bug caught: probe is stateless → never accumulates flatline → never stalls.
    p = _probe(
        snap=_snap(0.0, 5.0, disk=40.0),
        log_tail="",
        clock_times=[0.0, 100.0, 130.0, 160.0],  # all past 90s grace
    )
    # First post-grace call establishes prev_snap; subsequent flat calls count up.
    verdicts = [p.check("pod1") for _ in range(4)]
    assert verdicts[-1] is BootVerdict.STALLED


def test_log_fetch_error_never_raises() -> None:
    # Bug caught: a transient 502 on the log fetch kills a healthy boot.
    def boom(_iid: str) -> str:
        raise RuntimeError("proxy 502")

    p = RunPodBootLivenessProbe(
        instance_id="pod1",
        util_endpoint=_UtilEP(True, _snap(13.0, 5.0)),
        fetch_bootstrap_log=boom,
        grace_s=90.0,
        consecutive_needed=3,
        clock=_Clock([0.0, 100.0]),
    )
    assert p.check("pod1") in (BootVerdict.ALIVE, BootVerdict.UNKNOWN)
```

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/providers/test_runpod_boot_liveness.py -v` → Expected: FAIL (`RunPodBootLivenessProbe` undefined).

- [ ] **Step 3: Implement** in `src/kinoforge/providers/runpod/__init__.py` (add near the util-probe usage; import the boot-liveness symbols + `Clock`/`RealClock` at top if absent):

```python
class RunPodBootLivenessProbe:
    """Stateful boot-liveness probe for one RunPod pod (2026-07-07).

    Each check() reads the util snapshot + bootstrap.log tail and feeds them to
    classify_boot_liveness, tracking the prior snapshot + flatline counter. Boot
    start time is captured at construction for the grace/elapsed window. Any
    fetch/read error degrades to UNKNOWN/ALIVE — never a false STALLED.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        util_endpoint: Any,
        fetch_bootstrap_log: Callable[[str], str | None],
        grace_s: float = 90.0,
        consecutive_needed: int = 3,
        clock: Clock | None = None,
    ) -> None:
        self._id = instance_id
        self._util = util_endpoint
        self._fetch_log = fetch_bootstrap_log
        self._grace_s = grace_s
        self._needed = consecutive_needed
        self._clock = clock if clock is not None else RealClock()
        self._start = self._clock.now()
        self._prev_snap: UtilSnapshot | None = None
        self._consecutive_flat = 0

    def check(self, instance_id: str) -> BootVerdict:  # noqa: D102
        try:
            exists, snap = self._util.probe(instance_id)
        except Exception:  # noqa: BLE001 — transport uncertain → keep waiting
            return BootVerdict.UNKNOWN
        try:
            log_tail = self._fetch_log(instance_id)
        except Exception:  # noqa: BLE001 — log fetch best-effort
            log_tail = None
        result = classify_boot_liveness(
            exists=exists,
            log_tail=log_tail,
            snap=snap,
            prev_snap=self._prev_snap,
            consecutive_flat=self._consecutive_flat,
            elapsed_s=self._clock.now() - self._start,
            grace_s=self._grace_s,
            consecutive_needed=self._needed,
        )
        self._consecutive_flat = result.consecutive_flat
        if snap is not None:
            self._prev_snap = snap
        return result.verdict


def _fetch_bootstrap_log_tail(instance: Instance, *, lines: int = 40) -> str | None:
    """Best-effort GET of the pod's :8001/bootstrap.log tail (or None)."""
    base = instance.endpoints.get("8001")
    if not base:
        # Derive from the 8000 proxy host if only that is present.
        base = next(iter(instance.endpoints.values()), "")
        if base:
            base = base.replace("-8000.", "-8001.")
    if not base:
        return None
    url = f"{base.rstrip('/')}/bootstrap.log"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    return "\n".join(text.splitlines()[-lines:])
```

Then add a factory method on `RunPodProvider`:

```python
    def make_boot_liveness_probe(
        self, instance: Instance
    ) -> RunPodBootLivenessProbe | None:
        """Fresh boot-liveness probe for this pod, or None if no util endpoint."""
        if self._util_endpoint is None:
            return None
        return RunPodBootLivenessProbe(
            instance_id=instance.id,
            util_endpoint=self._util_endpoint,
            fetch_bootstrap_log=lambda _iid: _fetch_bootstrap_log_tail(instance),
        )
```

(`self._util_endpoint` already exists — set in `__init__` ~line 360. `Instance`, `urllib.request`, `Callable`, `Any` are already imported.)

- [ ] **Step 4: Run tests** — Run: `pixi run pytest tests/providers/test_runpod_boot_liveness.py -v` → Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/providers/runpod/__init__.py tests/providers/test_runpod_boot_liveness.py
git commit -m "feat(runpod): RunPodBootLivenessProbe — util + bootstrap.log tail -> BootVerdict, provider factory"
```

---

## Task 5: Consult the boot probe in `wait_for_ready`

**Goal:** `wait_for_ready` (diffusers + comfyui) consults an injected `BootLivenessProbe` on a throttle interval and aborts `GONE`/`STALLED` with `ProvisionFailed`; a `get_instance` KeyError maps to the same clean abort; `probe=None` preserves today's poll-until-timeout behavior. Orchestrator attaches the provider's probe.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py` (`wait_for_ready` ~1096-1159; add `attach_boot_liveness_probe` + `_boot_liveness_probe` field; call site ~895)
- Modify: `src/kinoforge/engines/comfyui/__init__.py` (`wait_for_ready` ~1402; same treatment)
- Modify: `src/kinoforge/core/orchestrator.py` (attach after `attach_get_instance`, ~767)
- Test: `tests/engines/test_diffusers_wait_for_ready_boot_stall.py` (new)

**Acceptance Criteria:**
- [ ] Probe returning `STALLED` → `wait_for_ready` raises `ProvisionFailed` promptly (before `timeout_s`).
- [ ] Probe returning `GONE` → `ProvisionFailed`.
- [ ] `get_instance` raising `KeyError` → `ProvisionFailed` ("vanished"), NOT an unhandled `KeyError`.
- [ ] `boot_liveness_probe=None` → existing behavior: polls until `timeout_s` then `ProvisionTimeout` (regression guard).
- [ ] The probe is consulted at most once per `_BOOT_PROBE_INTERVAL_S`, not every `/health` poll.

**Verify:** `pixi run pytest tests/engines/test_diffusers_wait_for_ready_boot_stall.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** — `tests/engines/test_diffusers_wait_for_ready_boot_stall.py`:

```python
"""wait_for_ready consults the boot-liveness probe and aborts dead boots."""

from __future__ import annotations

import pytest

from kinoforge.core.boot_liveness import BootVerdict
from kinoforge.core.errors import ProvisionFailed, ProvisionTimeout
from kinoforge.core.interfaces import Instance
from kinoforge.engines.diffusers import DiffusersEngine


def _inst() -> Instance:
    return Instance(
        id="pod-abc", provider="runpod", status="ready", created_at=0.0,
        endpoints={"8000": "http://pod-abc.runpod.io"},
    )


class _Probe:
    def __init__(self, verdict: BootVerdict) -> None:
        self._v = verdict
        self.calls = 0

    def check(self, _iid: str) -> BootVerdict:
        self.calls += 1
        return self._v


def _wait(engine: DiffusersEngine, *, probe, timeout_s=900.0, get_instance=None):
    calls = {"health": 0}

    def http_get(_url):
        calls["health"] += 1
        raise RuntimeError("health not up")

    def _get_instance(_iid):
        return _inst()

    engine.attach_boot_liveness_probe(probe)
    engine.wait_for_ready(
        _inst(),
        http_get=http_get,
        sleep=lambda _s: None,
        get_instance=get_instance or _get_instance,
        timeout_s=timeout_s,
    )


def test_stalled_probe_aborts_fast() -> None:
    # Bug caught: a dead boot burns the full boot_timeout (900s) instead of
    # bailing when the probe says STALLED.
    engine = DiffusersEngine()
    with pytest.raises(ProvisionFailed):
        _wait(engine, probe=_Probe(BootVerdict.STALLED))


def test_gone_probe_aborts() -> None:
    engine = DiffusersEngine()
    with pytest.raises(ProvisionFailed):
        _wait(engine, probe=_Probe(BootVerdict.GONE))


def test_get_instance_keyerror_maps_to_provisionfailed() -> None:
    # Bug caught: a reclaimed pod makes get_instance KeyError, which today
    # escapes as an unhandled KeyError instead of a clean ProvisionFailed.
    engine = DiffusersEngine()

    def gone(_iid):
        raise KeyError("pod gone")

    with pytest.raises(ProvisionFailed):
        _wait(engine, probe=_Probe(BootVerdict.ALIVE), get_instance=gone)


def test_none_probe_preserves_timeout() -> None:
    # Bug caught: adding the probe changes the no-probe path (must still time out).
    engine = DiffusersEngine()
    with pytest.raises(ProvisionTimeout):
        _wait(engine, probe=None, timeout_s=0.0)
```

Read `wait_for_ready`'s real signature (`diffusers/__init__.py` ~1096) and the `DiffusersEngine` constructor before running — adjust `_inst()`/`_wait` kwargs to match (the engine may need `base_url`/other ctor args; pass the minimum). Do NOT weaken the assertions.

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/engines/test_diffusers_wait_for_ready_boot_stall.py -v` → Expected: FAIL (`attach_boot_liveness_probe` undefined; no abort).

- [ ] **Step 3: Add the setter + field.** In `DiffusersEngine.__init__` (wherever `_get_instance`/`_asset_paths` defaults are set), add:

```python
        self._boot_liveness_probe: BootLivenessProbe | None = None
```

and a setter mirroring `attach_get_instance`:

```python
    def attach_boot_liveness_probe(self, probe: "BootLivenessProbe | None") -> None:
        """Wire the provider's boot-liveness probe (or None) for wait_for_ready."""
        self._boot_liveness_probe = probe
```

Import at top: `from kinoforge.core.boot_liveness import BootLivenessProbe, BootVerdict`.

- [ ] **Step 4: Consult it in the poll loop.** In `wait_for_ready` (the `while True:` at ~1139), the existing tail is:

```python
            try:
                http_get(ready_url)
                return
            except Exception:  # noqa: BLE001, S110
                pass
            current = get_instance(instance.id)
            if current.status in ("terminated", "stopped"):
                raise ProvisionFailed(
                    f"pod {instance.id!r} entered terminal status "
                    f"{current.status!r} before ready"
                )
            sleep(_READY_POLL_INTERVAL_S)
```

Replace it with:

```python
            try:
                http_get(ready_url)
                return
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                current = get_instance(instance.id)
            except KeyError as exc:
                raise ProvisionFailed(
                    f"pod {instance.id!r} vanished during boot (provider "
                    f"no longer knows it)"
                ) from exc
            if current.status in ("terminated", "stopped"):
                raise ProvisionFailed(
                    f"pod {instance.id!r} entered terminal status "
                    f"{current.status!r} before ready"
                )
            # 2026-07-07 boot-stall fast-fail: consult the injected liveness
            # probe on its own throttle (not every /health poll). GONE/STALLED
            # abort in ~2-3min instead of waiting the full boot_timeout.
            probe = self._boot_liveness_probe
            if probe is not None and now - last_probe >= _BOOT_PROBE_INTERVAL_S:
                last_probe = now
                verdict = probe.check(instance.id)
                if verdict is BootVerdict.GONE:
                    raise ProvisionFailed(
                        f"pod {instance.id!r} vanished during boot"
                    )
                if verdict is BootVerdict.STALLED:
                    raise ProvisionFailed(
                        f"pod {instance.id!r} boot stalled (provision crashed "
                        f"or util flatline) — aborting before boot_timeout"
                    )
            sleep(_READY_POLL_INTERVAL_S)
```

Add, just before the `while True:`, the throttle bookkeeping + module const. Near the other module consts (top of file) add:

```python
_BOOT_PROBE_INTERVAL_S: float = 30.0
```

and inside `wait_for_ready` before the loop (alongside `start = time.monotonic()`):

```python
        last_probe = start - _BOOT_PROBE_INTERVAL_S  # allow a probe on the first idle poll
```

Ensure `now` is defined in the loop (the existing loop computes `now = time.monotonic()` near the top of each iteration — reuse it; if not present, add `now = time.monotonic()` at the loop top).

- [ ] **Step 5: Mirror into comfyui.** Apply the identical `attach_boot_liveness_probe` + field + poll-loop consult + KeyError-map + `_BOOT_PROBE_INTERVAL_S` to `engines/comfyui/__init__.py` `wait_for_ready` (~1402). Read that method first; its `/health`-equivalent readiness check + poll structure mirror diffusers.

- [ ] **Step 6: Attach in the orchestrator.** In `orchestrator.py`, right after `resolved_engine.attach_get_instance(resolved_provider.get_instance)` (~767):

```python
    # 2026-07-07: give the engine a boot-liveness probe when the provider
    # supplies one (RunPod). Providers without one → None → no boot-stall check.
    _make_probe = getattr(resolved_provider, "make_boot_liveness_probe", None)
    resolved_engine.attach_boot_liveness_probe(
        _make_probe(instance) if _make_probe is not None else None
    )
```

Guard: `attach_boot_liveness_probe` must exist on every engine the orchestrator drives. If comfyui/other engines share a base or are duck-typed, add a no-op default (`getattr(resolved_engine, "attach_boot_liveness_probe", lambda _p: None)(...)`) OR add the setter to the shared base. Prefer adding it to both concrete engines (diffusers done in Step 3, comfyui in Step 5); use the `getattr` guard for any third engine (e.g. fake/hosted) that lacks it.

- [ ] **Step 7: Run tests** — Run: `pixi run pytest tests/engines/test_diffusers_wait_for_ready_boot_stall.py -v` → Expected: PASS.

- [ ] **Step 8: Guard regressions** — Run: `pixi run pytest tests/engines/ -k "wait_for_ready or provision" -q` → Expected: PASS (existing wait_for_ready tests — incl. the cancel + timeout ones — still green; `None`-probe path unchanged).

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/engines/diffusers/__init__.py src/kinoforge/engines/comfyui/__init__.py src/kinoforge/core/orchestrator.py tests/engines/test_diffusers_wait_for_ready_boot_stall.py
git commit -m "feat(engines): boot-stall fast-fail in wait_for_ready — abort GONE/STALLED + map get_instance KeyError to ProvisionFailed"
```

---

## Task 6: Widen the FlashVSR config + set `capacity_wait`

**Goal:** `upscale-flashvsr-1080p.yaml` qualifies four Ampere/Hopper 80GB GPU types under a `$3.00` cap and sets `capacity_wait: 5m`, so capacity misses are rarer and ridden out.

**Files:**
- Modify: `examples/configs/upscale-flashvsr-1080p.yaml` (`requirements` + `lifecycle`)
- Test: `tests/core/test_config.py` (extend — the widened cfg loads)

**Acceptance Criteria:**
- [ ] The cfg loads and `gpu_preference` == the four named types in order.
- [ ] `max_usd_per_hr == 3.00`.
- [ ] The parsed lifecycle `capacity_wait` == 300.0.

**Verify:** `pixi run pytest tests/core/test_config.py -k flashvsr_widened -v` → pass.

**Steps:**

- [ ] **Step 1: Write the failing test** — append to `tests/core/test_config.py`:

```python
def test_flashvsr_widened_config_loads() -> None:
    """The widened FlashVSR upscale cfg parses with 4 prefs, $3 cap, 5m capacity_wait.

    Bug caught: a typo'd GPU name or an unparseable capacity_wait silently ships
    a config that fails only on a live create.
    """
    from pathlib import Path

    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/upscale-flashvsr-1080p.yaml"))
    reqs = cfg.compute.requirements
    assert list(reqs.gpu_preference) == [
        "NVIDIA A100 80GB PCIe",
        "NVIDIA A100-SXM4-80GB",
        "NVIDIA H100 80GB HBM3",
        "NVIDIA H100 NVL",
    ]
    assert reqs.max_usd_per_hr == 3.00
    assert cfg.lifecycle().capacity_wait_s == 300.0
```

Use whatever config-loading entry point the codebase exposes (`load_config` / `Config.from_yaml` — grep `def load_config` or how other `tests/core/test_config.py` tests load a file). Match the existing pattern in that test module.

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/core/test_config.py -k flashvsr_widened -v` → Expected: FAIL (current cfg has 2 prefs, cap 2.50, no capacity_wait).

- [ ] **Step 3: Edit the config.** In `examples/configs/upscale-flashvsr-1080p.yaml`, `requirements`:

```yaml
    max_usd_per_hr: 3.00
    disk_gb: 60
    gpu_preference:
      - "NVIDIA A100 80GB PCIe"
      - "NVIDIA A100-SXM4-80GB"
      - "NVIDIA H100 80GB HBM3"
      - "NVIDIA H100 NVL"
```

and in `lifecycle`, add:

```yaml
    capacity_wait: 5m
```

Update the `# Dedicated hosts…` comment above `cloud_type: secure` if needed, and the `max_usd_per_hr` inline comment. Leave everything else (image, upscale block, disk_gb) unchanged.

- [ ] **Step 4: Run tests** — Run: `pixi run pytest tests/core/test_config.py -k flashvsr_widened -v` → Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/configs/upscale-flashvsr-1080p.yaml tests/core/test_config.py
git commit -m "feat(config): widen FlashVSR upscale cfg — 4 Ampere/Hopper 80GB types, \$3 cap, 5m capacity_wait"
```

---

## Task 7: Full-suite + lint/type green, update PROGRESS

**Goal:** Whole suite green, lint/type clean, PROGRESS records the feature.

**Files:**
- Modify: `PROGRESS.md` (RESUME SNAPSHOT)

**Acceptance Criteria:**
- [ ] `pixi run test` green (no new failures vs. the ~3907-passed baseline plus the new tests).
- [ ] `pixi run lint` + `pixi run typecheck` clean.
- [ ] `pixi run pre-commit run --all-files` passes.
- [ ] PROGRESS RESUME SNAPSHOT mentions boot-stall fast-fail + capacity-retry + spec/plan paths.

**Verify:** `pixi run pre-commit run --all-files` → all pass; `pixi run test` → green.

**Steps:**

- [ ] **Step 1: Full suite** — Run: `pixi run test` → Expected: green; note the passed count. Re-run any lone live-test flake in isolation to confirm it's not a regression (live tests hit real RunPod).
- [ ] **Step 2: Lint + type** — Run: `pixi run lint` then `pixi run typecheck` → Expected: clean.
- [ ] **Step 3: Update PROGRESS.md** RESUME SNAPSHOT: boot-stall fast-fail + capacity-retry shipped; point to `docs/superpowers/specs/2026-07-07-runpod-boot-stall-capacity-retry-design.md` and this plan.
- [ ] **Step 4: pre-commit + commit**

```bash
pixi run pre-commit run --all-files
git add PROGRESS.md
git commit -m "docs(progress): RunPod boot-stall fast-fail + capacity-retry shipped"
```

---

## Self-Review

**Spec coverage:**
- Component 1 (boot-stall fast-fail): pure classifier → Task 3; RunPod probe → Task 4; `wait_for_ready` wiring + KeyError→GONE → Task 5. ✓
- Component 2 (capacity-retry + config): `CapacityError` classification → Task 1; retry loop → Task 2; `capacity_wait` field → Task 0; config widening → Task 6. ✓
- Testing section (all unit, fakes) → each task's tests; full suite → Task 7. ✓
- Non-goals honored: no auto-reprovision (retry loop only re-queries+retries create, never destroys/reprovisions a booted pod); reuse `CapacityError` (Task 1, no new type); no CLI flag (Task 0 cfg-only); non-RunPod untouched (`None` probe default, provider-gated `make_boot_liveness_probe`). ✓

**Placeholder scan:** No TBD/TODO. Deferred details are grounded reads ("read wait_for_ready's real signature at ~1096", "match the local var name in lifecycle()"), not vague instructions — each with the exact file:line to consult.

**Type consistency:** `classify_boot_liveness(...) -> BootLivenessResult(verdict, consecutive_flat)` defined Task 3, consumed Task 4. `BootVerdict` {ALIVE,GONE,STALLED,UNKNOWN} used identically across 3/4/5. `_create_with_capacity_wait(find_offers, create, capacity_wait_s, retry_interval_s, clock, sleep)` defined + called Task 2. `attach_boot_liveness_probe` / `_boot_liveness_probe` consistent across Task 5 + orchestrator. `capacity_wait` (cfg, duration) → `capacity_wait_s` (interface + `_create_with_capacity_wait` arg) mapping consistent Tasks 0/2/6. `UtilSnapshot` fields (`cpu_percent`, `memory_percent`, `disk_percent`) match the live probe shape.

**Note for implementer:** `disk_percent` on `UtilSnapshot` can be `None` (RunPod returns null) — the flatline predicate treats `None` disk as "no growth signal" and relies on CPU+mem, exactly as `_is_flat` encodes. Don't `float()` a `None` disk.
