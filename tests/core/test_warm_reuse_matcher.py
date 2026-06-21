"""find_warm_attach_candidate matcher — 8 test scenarios.

Uses fake ledger / fake pod_lock_registry / fake re_probe + a small stub Config
that exposes the (warm_attach_key, lora_stack) factor accessors the matcher
relies on. Avoids the real Ledger to keep these tests focused on matcher
algorithmic decisions, not store I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest

from kinoforge.core.interfaces import CapabilityKey
from kinoforge.core.warm_reuse.matcher import (
    SwapPlan,
    find_warm_attach_candidate,
)


@dataclass
class _StubCfg:
    cap_key: CapabilityKey

    def capability_key(self) -> CapabilityKey:
        return self.cap_key


class _FakeLedger:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    def find_pods_by_warm_attach_key(self, wak_hex: str) -> list[dict[str, Any]]:
        return [e for e in self._entries if e.get("warm_attach_key") == wak_hex]


@dataclass
class _FakeLockRegistry:
    locked: set[str] = field(default_factory=set)
    acquired_now: list[str] = field(default_factory=list)

    def acquire(self, pod_id: str, *, blocking: bool = False) -> bool:
        if pod_id in self.locked:
            return False
        self.locked.add(pod_id)
        self.acquired_now.append(pod_id)
        return True

    def __contains__(self, pod_id: str) -> bool:
        return pod_id in self.locked


def _cfg(base: str, loras: tuple[str, ...]) -> _StubCfg:
    return _StubCfg(
        cap_key=CapabilityKey(
            base_model=base, loras=loras, engine="diffusers", precision="fp16"
        )
    )


def _entry(
    *,
    pod_id: str,
    base: str,
    loras: tuple[str, ...] = (),
    inventory: list[dict[str, Any]] | None = None,
    free_bytes: int = 10_000_000,
    observed_at: str | None = None,
    status: str = "ready",
) -> dict[str, Any]:
    key = CapabilityKey(
        base_model=base, loras=loras, engine="diffusers", precision="fp16"
    )
    return {
        "id": pod_id,
        "capability_key_hex": key.derive(),
        "warm_attach_key": key.warm_attach_key().derive(),
        "lora_inventory": inventory or [],
        "loras_dir_free_bytes": free_bytes,
        "loras_dir_free_bytes_observed_at_local": observed_at
        or datetime.now().isoformat(),
        "status": status,
    }


def _inv(ref: str, size: int = 100, last_used: str | None = None) -> dict[str, Any]:
    return {
        "ref": ref,
        "filename": f"{ref}.s",
        "size_bytes": size,
        "loras_dir_path": f"/loras/{ref}.s",
        "downloaded_at_local": last_used or "2026-06-20T10:00:00-07:00",
        "last_used_at_local": last_used or "2026-06-20T10:00:00-07:00",
        "adapter_name": "lora_0",
    }


def test_exact_byte_fast_path_returns_zero_cost_match() -> None:
    """When candidate.capability_key_hex == cfg.capability_key().derive() →
    zero-cost match with empty swap plan.

    Bug: matcher skips the byte-equal path and re-evaluates as a delta,
    producing a plan that re-loads adapters already loaded on the pod.
    """
    cfg = _cfg("hf:m", ("A", "B"))
    pod = _entry(
        pod_id="pod-a",
        base="hf:m",
        loras=("A", "B"),
        inventory=[_inv("A"), _inv("B")],
    )
    result = find_warm_attach_candidate(
        cfg, _FakeLedger([pod]), pod_lock_registry=_FakeLockRegistry()
    )
    assert result is not None
    assert result.pod_id == "pod-a"
    assert result.swap_plan == SwapPlan(
        evict=[], download=[], estimated_cost_seconds=0.0
    )


def test_delta_path_no_eviction_returns_correct_swap_plan() -> None:
    """Pre-existing {A}, target {A,B} → evict=[], download=[B].

    Bug: matcher returns download=[A,B] because it diffs against an empty
    inventory baseline.
    """
    cfg = _cfg("hf:m", ("A", "B"))
    pod = _entry(pod_id="pod-a", base="hf:m", loras=("A",), inventory=[_inv("A")])
    specs = {"B": {"url": "x", "headers": {}, "filename": "b.s", "size_hint": 500}}
    result = find_warm_attach_candidate(
        cfg,
        _FakeLedger([pod]),
        pod_lock_registry=_FakeLockRegistry(),
        download_specs=specs,
    )
    assert result is not None
    assert result.swap_plan.evict == []
    assert result.swap_plan.download == ["B"]
    assert result.swap_plan.estimated_cost_seconds > 0


def test_delta_path_with_eviction_returns_lru_ordered_evict() -> None:
    """Tight disk, current={X(old),Y(new)}, target={B} → evict X first (LRU).

    Bug: matcher sorts evict candidates by ref name instead of last_used_at,
    evicting hot adapters before cold ones.
    """
    cfg = _cfg("hf:m", ("B",))
    pod = _entry(
        pod_id="pod-a",
        base="hf:m",
        loras=("X", "Y"),
        inventory=[
            _inv("Y", size=100, last_used="2026-06-20T11:00:00-07:00"),
            _inv("X", size=100, last_used="2026-06-20T09:00:00-07:00"),  # oldest
        ],
        free_bytes=50,
    )
    specs = {"B": {"url": "x", "headers": {}, "filename": "b.s", "size_hint": 100}}
    result = find_warm_attach_candidate(
        cfg,
        _FakeLedger([pod]),
        pod_lock_registry=_FakeLockRegistry(),
        download_specs=specs,
    )
    assert result is not None
    assert result.swap_plan.evict == ["X"]
    assert result.swap_plan.download == ["B"]


def test_skips_degraded_pods() -> None:
    """status=degraded pod is removed from consideration.

    Bug: matcher returns a degraded pod, which immediately fails the swap
    + sends the operator into a destroy-and-retry loop.
    """
    cfg = _cfg("hf:m", ())
    degraded = _entry(pod_id="pod-bad", base="hf:m", loras=(), status="degraded")
    healthy = _entry(pod_id="pod-good", base="hf:m", loras=())
    result = find_warm_attach_candidate(
        cfg, _FakeLedger([degraded, healthy]), pod_lock_registry=_FakeLockRegistry()
    )
    assert result is not None
    assert result.pod_id == "pod-good"


def test_skips_pods_locked_by_other_jobs() -> None:
    """Already-held pod is filtered, fallback picks the next one.

    Bug: matcher returns a pod already locked by another in-flight swap,
    deadlocking the new job on acquire().
    """
    cfg = _cfg("hf:m", ())
    locked_pod = _entry(pod_id="pod-locked", base="hf:m", loras=())
    free_pod = _entry(pod_id="pod-free", base="hf:m", loras=())
    registry = _FakeLockRegistry(locked={"pod-locked"})
    result = find_warm_attach_candidate(
        cfg, _FakeLedger([locked_pod, free_pod]), pod_lock_registry=registry
    )
    assert result is not None
    assert result.pod_id == "pod-free"


def test_returns_none_when_no_candidate_viable() -> None:
    """All candidates are degraded → returns None (cold-boot path).

    Bug: matcher returns a degraded pod or crashes; should silently signal
    'no warm match, cold-boot a fresh one'.
    """
    cfg = _cfg("hf:m", ())
    result = find_warm_attach_candidate(
        cfg, _FakeLedger([]), pod_lock_registry=_FakeLockRegistry()
    )
    assert result is None


def test_re_probes_when_snapshot_stale() -> None:
    """observed_at older than threshold → re_probe is invoked, snapshot wins.

    Bug: matcher trusts the stale free_bytes snapshot, picks a pod that
    appears to have room, then the swap fails ENOSPC.
    """
    cfg = _cfg("hf:m", ("B",))
    stale_when = (datetime.now() - timedelta(seconds=600)).isoformat()
    pod = _entry(
        pod_id="pod-a",
        base="hf:m",
        loras=(),
        inventory=[],
        free_bytes=0,
        observed_at=stale_when,
    )

    reprobe_calls: list[str] = []

    def _reprobe(pod_id: str) -> dict[str, Any]:
        reprobe_calls.append(pod_id)
        return {"inventory": [], "free_bytes": 10_000_000}

    specs = {"B": {"url": "x", "headers": {}, "filename": "b.s", "size_hint": 100}}
    result = find_warm_attach_candidate(
        cfg,
        _FakeLedger([pod]),
        pod_lock_registry=_FakeLockRegistry(),
        re_probe=_reprobe,
        re_probe_threshold_s=300.0,
        download_specs=specs,
    )
    assert reprobe_calls == ["pod-a"]
    assert result is not None
    assert result.swap_plan.evict == []
    assert result.swap_plan.download == ["B"]


def test_re_probes_always_under_ephemeral(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under EphemeralSession with ledger_record=False → re_probe forced
    even on a fresh snapshot.

    Bug: ephemeral runs reuse a snapshot from a previous (non-ephemeral)
    session, leaking inventory refs that the registry-substitution layer
    never saw.
    """
    # Target has a LoRA, pod has none — forces delta path (otherwise
    # exact-byte fast path would skip the re_probe branch entirely).
    cfg = _cfg("hf:m", ("B",))
    fresh = datetime.now().isoformat()
    pod = _entry(
        pod_id="pod-a",
        base="hf:m",
        loras=(),
        inventory=[],
        free_bytes=10_000_000,
        observed_at=fresh,
    )
    reprobe_calls: list[str] = []

    def _reprobe(pod_id: str) -> dict[str, Any]:
        reprobe_calls.append(pod_id)
        return {"inventory": [], "free_bytes": 10_000_000}

    class _FakePolicy:
        ledger_record = False

    class _FakeSession:
        policy = _FakePolicy()

        @classmethod
        def current(cls) -> _FakeSession:
            return cls()

    monkeypatch.setattr(
        "kinoforge.core.warm_reuse.matcher.EphemeralSession", _FakeSession
    )

    specs = {"B": {"url": "x", "headers": {}, "filename": "b.s", "size_hint": 100}}
    find_warm_attach_candidate(
        cfg,
        _FakeLedger([pod]),
        pod_lock_registry=_FakeLockRegistry(),
        re_probe=_reprobe,
        download_specs=specs,
    )
    assert reprobe_calls == ["pod-a"]
