"""Tests for Task 18 lifecycle extensions: Ledger, destroy_confirmed, reap, BudgetTracker.

AC #1: destroy_confirmed happy path — returns normally when instance disappears first try.
AC #2: destroy_confirmed retry path — flaky provider: first call no-op, second removes instance.
AC #3: destroy_confirmed failure path — defiant provider never removes; TeardownError raised,
        ERROR logged.
AC #4: sweeper destroys over-age, leaves fresh.
AC #5: sweeper destroys idle (should_reap True), fresh instance left.
AC #6: Ledger persistence round-trip — separate Ledger instances, same entries.
AC #7: Ledger forget — recorded then forgotten; entry absent.
AC #8: BudgetTracker enforce — destroys instance AND raises BudgetExceeded at > budget.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import BudgetExceeded, TeardownError
from kinoforge.core.interfaces import (
    ComputeProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Lifecycle,
    Offer,
)
from kinoforge.core.lifecycle import (
    BudgetTracker,
    Ledger,
    LifecycleManager,
    destroy_confirmed,
    reap,
)
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# ---------------------------------------------------------------------------
# Helpers / shared factories
# ---------------------------------------------------------------------------

_SPEC = InstanceSpec(image="test")


def _make_instance(
    instance_id: str = "i-test",
    created_at: float = 0.0,
    cost_rate_usd_per_hr: float = 0.0,
) -> Instance:
    """Return a minimal Instance for ledger tests."""
    return Instance(
        id=instance_id,
        provider="test",
        status="ready",
        created_at=created_at,
        cost_rate_usd_per_hr=cost_rate_usd_per_hr,
    )


# ---------------------------------------------------------------------------
# Fake providers for destroy_confirmed tests
# ---------------------------------------------------------------------------


class _EasyProvider(ComputeProvider):
    """Destroys immediately; instance is gone on first poll."""

    name = "easy"

    def __init__(self) -> None:
        self._instances: dict[str, Instance] = {}

    def create(self, instance_id: str) -> Instance:
        """Register and return a fake instance."""
        inst = _make_instance(instance_id)
        self._instances[instance_id] = inst
        return inst

    def destroy_instance(self, instance_id: str) -> None:
        """Remove the instance immediately."""
        self._instances.pop(instance_id, None)

    def list_instances(self) -> list[Instance]:
        """Return live instances."""
        return list(self._instances.values())

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:  # noqa: D102
        return []

    def create_instance(self, spec: InstanceSpec) -> Instance:  # noqa: D102
        raise NotImplementedError

    def get_instance(self, instance_id: str) -> Instance:  # noqa: D102
        return self._instances[instance_id]

    def stop_instance(self, instance_id: str) -> None:  # noqa: D102
        pass

    def heartbeat(self, instance_id: str) -> None:  # noqa: D102
        pass

    def endpoints(self, instance: Instance) -> dict[str, str]:  # noqa: D102
        return {}


class _FlakyProvider(ComputeProvider):
    """First destroy call is a no-op; second actually removes."""

    name = "flaky"

    def __init__(self) -> None:
        self._instances: dict[str, Instance] = {}
        self._destroy_calls: int = 0

    def create(self, instance_id: str) -> Instance:
        """Register and return a fake instance."""
        inst = _make_instance(instance_id)
        self._instances[instance_id] = inst
        return inst

    def destroy_instance(self, instance_id: str) -> None:
        """No-op on first call; removes on subsequent calls."""
        self._destroy_calls += 1
        if self._destroy_calls >= 2:
            self._instances.pop(instance_id, None)

    def list_instances(self) -> list[Instance]:
        """Return live instances."""
        return list(self._instances.values())

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:  # noqa: D102
        return []

    def create_instance(self, spec: InstanceSpec) -> Instance:  # noqa: D102
        raise NotImplementedError

    def get_instance(self, instance_id: str) -> Instance:  # noqa: D102
        return self._instances[instance_id]

    def stop_instance(self, instance_id: str) -> None:  # noqa: D102
        pass

    def heartbeat(self, instance_id: str) -> None:  # noqa: D102
        pass

    def endpoints(self, instance: Instance) -> dict[str, str]:  # noqa: D102
        return {}


class _DefiantProvider(ComputeProvider):
    """Never removes the instance regardless of how many destroy calls are made."""

    name = "defiant"

    def __init__(self) -> None:
        self._instances: dict[str, Instance] = {}
        self._destroy_calls: int = 0

    def create(self, instance_id: str) -> Instance:
        """Register and return a fake instance."""
        inst = _make_instance(instance_id)
        self._instances[instance_id] = inst
        return inst

    def destroy_instance(self, instance_id: str) -> None:
        """Count calls but never remove the instance."""
        self._destroy_calls += 1

    def list_instances(self) -> list[Instance]:
        """Return live instances (always present)."""
        return list(self._instances.values())

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:  # noqa: D102
        return []

    def create_instance(self, spec: InstanceSpec) -> Instance:  # noqa: D102
        raise NotImplementedError

    def get_instance(self, instance_id: str) -> Instance:  # noqa: D102
        return self._instances[instance_id]

    def stop_instance(self, instance_id: str) -> None:  # noqa: D102
        pass

    def heartbeat(self, instance_id: str) -> None:  # noqa: D102
        pass

    def endpoints(self, instance: Instance) -> dict[str, str]:  # noqa: D102
        return {}


# ---------------------------------------------------------------------------
# AC #1 — destroy_confirmed happy path
# ---------------------------------------------------------------------------


def test_destroy_confirmed_happy_path() -> None:
    """AC #1: destroy_confirmed returns normally when instance vanishes immediately."""
    provider = _EasyProvider()
    provider.create("i-easy")

    # Should not raise
    destroy_confirmed(provider, "i-easy", retries=3, sleep=lambda _: None)

    # Instance should be gone
    assert not any(i.id == "i-easy" for i in provider.list_instances())


# ---------------------------------------------------------------------------
# AC #2 — destroy_confirmed retry path
# ---------------------------------------------------------------------------


def test_destroy_confirmed_retry_path() -> None:
    """AC #2: flaky provider — first call no-op; succeeds after 2nd attempt."""
    provider = _FlakyProvider()
    provider.create("i-flaky")

    sleep_calls: list[float] = []

    destroy_confirmed(
        provider,
        "i-flaky",
        retries=3,
        sleep=lambda s: sleep_calls.append(s),
    )

    # Instance is gone
    assert not any(i.id == "i-flaky" for i in provider.list_instances())
    # destroy was called twice (first no-op, second removes)
    assert provider._destroy_calls == 2
    # sleep was called at least once (between attempts)
    assert len(sleep_calls) >= 1


# ---------------------------------------------------------------------------
# AC #3 — destroy_confirmed failure path
# ---------------------------------------------------------------------------


def test_destroy_confirmed_failure_path(caplog: pytest.LogCaptureFixture) -> None:
    """AC #3: defiant provider → TeardownError raised; ERROR logged with instance id."""
    provider = _DefiantProvider()
    provider.create("i-defiant")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(TeardownError, match="i-defiant"):
            destroy_confirmed(
                provider,
                "i-defiant",
                retries=2,
                sleep=lambda _: None,
            )

    # An ERROR-level log message must have been emitted
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected at least one ERROR log record"
    assert any("i-defiant" in r.message for r in error_records)


# ---------------------------------------------------------------------------
# AC #4 — sweeper: destroys over-age, leaves fresh
# ---------------------------------------------------------------------------


def test_reap_destroys_over_age_leaves_fresh(tmp_path: Path) -> None:
    """AC #4: two instances; over-age one destroyed, fresh one kept."""
    clock = FakeClock(start=0.0)
    provider = LocalProvider(clock=clock)
    lc = Lifecycle(
        idle_timeout_s=2 * 3600,
        job_timeout_s=30 * 60,
        time_buffer_s=30 * 60,
        max_lifetime_s=1 * 3600,  # 1 hour max lifetime
    )
    manager = LifecycleManager(
        provider=provider, clock=clock, lifecycle=lc, run_id="r-reap"
    )
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_reap_test")

    # Create instance A at t=0 (over-age: max_lifetime=1h, clock will be at 5h)
    inst_a = provider.create_instance(_SPEC)
    manager.register(inst_a.id, inst_a.created_at)
    ledger.record(inst_a)

    # Advance clock to t=4h, create instance B (fresh at t=4h; at t=5h only 1h old = at limit)
    clock.advance(4 * 3600)
    inst_b = provider.create_instance(_SPEC)
    manager.register(inst_b.id, inst_b.created_at)
    ledger.record(inst_b)

    # Advance clock to t=5h — inst_a is 5h old (>> 1h limit), inst_b is 1h old (== limit, not >)
    clock.advance(1 * 3600)

    destroyed = reap(provider, manager, ledger)

    # inst_a should be destroyed, inst_b should survive
    assert inst_a.id in destroyed
    assert inst_b.id not in destroyed

    live_ids = {i.id for i in provider.list_instances()}
    assert inst_a.id not in live_ids
    assert inst_b.id in live_ids


# ---------------------------------------------------------------------------
# AC #5 — sweeper: destroys idle (should_reap True)
# ---------------------------------------------------------------------------


def test_reap_destroys_idle_instance(tmp_path: Path) -> None:
    """AC #5: idle instance past idle_timeout → reaped and forgotten."""
    clock = FakeClock(start=0.0)
    provider = LocalProvider(clock=clock)
    lc = Lifecycle(
        idle_timeout_s=2 * 3600,
        job_timeout_s=30 * 60,
        time_buffer_s=30 * 60,
        max_lifetime_s=24 * 3600,  # large — won't trip on lifetime
    )
    manager = LifecycleManager(
        provider=provider, clock=clock, lifecycle=lc, run_id="r-idle"
    )
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_idle_test")

    # Create + register + record
    inst = provider.create_instance(_SPEC)
    manager.register(inst.id, inst.created_at)
    ledger.record(inst)

    # Complete a job so idle_since is set
    manager.start_job(inst.id, "job-1", num_segments=1)
    manager.finish_job(inst.id, "job-1")

    # Advance 3h (> 2h idle_timeout) → should_reap True
    clock.advance(3 * 3600)
    assert manager.should_reap(inst.id)

    destroyed = reap(provider, manager, ledger)

    assert inst.id in destroyed
    assert inst.id not in {i.id for i in provider.list_instances()}
    # Ledger entry should be forgotten
    assert not any(e["id"] == inst.id for e in ledger.entries())


# ---------------------------------------------------------------------------
# AC #6 — Ledger persistence round-trip
# ---------------------------------------------------------------------------


def test_ledger_persistence_round_trip(tmp_path: Path) -> None:
    """AC #6: records persist across fresh Ledger instances."""
    store = LocalArtifactStore(tmp_path)
    run_id = "_ledger_rt"

    inst1 = _make_instance("i-1", created_at=1.0)
    inst2 = _make_instance("i-2", created_at=2.0)

    # Write via two separate Ledger instances to test persistence
    Ledger(store=store, run_id=run_id).record(inst1)
    Ledger(store=store, run_id=run_id).record(inst2)

    # Fresh Ledger — must see both entries
    fresh = Ledger(store=store, run_id=run_id)
    entries = fresh.entries()
    ids = {e["id"] for e in entries}
    assert "i-1" in ids
    assert "i-2" in ids
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# AC #7 — Ledger forget
# ---------------------------------------------------------------------------


def test_ledger_forget_removes_entry(tmp_path: Path) -> None:
    """AC #7: forget removes the entry; subsequent entries() does not include it."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_forget_test")

    inst = _make_instance("i-forget", created_at=42.0)
    ledger.record(inst)
    assert any(e["id"] == "i-forget" for e in ledger.entries())

    ledger.forget("i-forget")
    assert not any(e["id"] == "i-forget" for e in ledger.entries())


def test_ledger_forget_nonexistent_is_noop(tmp_path: Path) -> None:
    """forget on unknown id must not raise."""
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_forget_noop")
    # Should not raise
    ledger.forget("does-not-exist")


# ---------------------------------------------------------------------------
# AC #8 — BudgetTracker enforce
# ---------------------------------------------------------------------------


def test_budget_tracker_enforce_destroys_and_raises(
    tmp_path: Path,
) -> None:
    """AC #8: over budget → instance destroyed AND BudgetExceeded raised."""
    clock = FakeClock(start=0.0)
    provider = LocalProvider(clock=clock)
    lc = Lifecycle(budget_usd=0.50)
    manager = LifecycleManager(
        provider=provider, clock=clock, lifecycle=lc, run_id="r-budget"
    )
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_budget_test")
    tracker = BudgetTracker(
        lifecycle_manager=manager, ledger=ledger, clock=clock, budget_usd=0.50
    )

    # Create instance with cost_rate_usd_per_hr=1.0; LocalProvider always sets 0.0,
    # so we build the Instance directly and inject it into provider + ledger.
    inst = Instance(
        id="i-budget",
        provider="local",
        status="ready",
        created_at=0.0,
        cost_rate_usd_per_hr=1.0,
    )
    provider._instances[inst.id] = inst
    manager.register(inst.id, inst.created_at)
    ledger.record(inst)

    # At t=30min: accrued = 0.5h * $1.00/hr = $0.50 — exactly at threshold, not over
    clock.advance(30 * 60)
    assert not tracker.over_budget(inst.id)

    # At t=31min: accrued = 31/60 * $1.00 ≈ $0.517 > $0.50 — over budget
    clock.advance(60)
    assert tracker.over_budget(inst.id)

    with pytest.raises(BudgetExceeded, match="i-budget"):
        tracker.enforce(inst.id, provider)

    # Instance must be destroyed BEFORE raise (destroy happens inside enforce)
    assert inst.id not in {i.id for i in provider.list_instances()}
