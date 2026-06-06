"""Layer V T5: sweep integration tests.

Covers spec ACs:
- AC11: provider.list_instances() cached per provider name
- AC12: failure isolation — one TeardownError doesn't abort sweep
- sweep w/ policy=None is read-only (no actions)
- sweep w/ DEFAULT_APPLY_POLICY routes correct subset
- Amendment: UNROUTABLE force-forget path (policy.act_verdicts includes UNROUTABLE)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import TeardownError
from kinoforge.core.interfaces import Instance
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Policy, Verdict
from kinoforge.core.reaper_actor import sweep


class _FakeStore:
    def __init__(self) -> None:
        self.acquires: list[tuple[str, float]] = []

    def acquire_lock(self, key: str, *, ttl_s: float) -> _FakeLock:
        self.acquires.append((key, ttl_s))
        return _FakeLock()


class _FakeLock:
    def __enter__(self) -> _FakeLock:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _FakeLedger:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = list(entries)
        self.forgotten: list[str] = []

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)
        self._entries = [e for e in self._entries if e.get("id") != instance_id]


class _FakeProvider:
    def __init__(
        self,
        live_ids: set[str],
        *,
        list_raises: bool = False,
        destroy_raises: bool = False,
    ) -> None:
        self.live_ids = set(live_ids)
        self.list_calls = 0
        self.destroyed: list[str] = []
        self._list_raises = list_raises
        self._destroy_raises = destroy_raises

    def list_instances(self) -> list[Instance]:
        self.list_calls += 1
        if self._list_raises:
            raise RuntimeError("network down")
        return [
            Instance(
                id=i,
                provider="fake",
                created_at=0.0,
                status="ready",
                cost_rate_usd_per_hr=0.5,
                tags={},
            )
            for i in self.live_ids
        ]

    def destroy_instance(self, instance_id: str) -> None:
        if self._destroy_raises:
            raise TeardownError("destroy raises")
        self.destroyed.append(instance_id)
        self.live_ids.discard(instance_id)

    def get_instance(self, instance_id: str) -> Instance:
        raise KeyError(instance_id)


_THR: Mapping[str, Any] = dict(
    idle_timeout_s=100.0,
    max_lifetime_s=10_000.0,
    heartbeat_interval_s=30.0,
    grace_after_session_s=500.0,
)


def _registry(
    providers: dict[str, _FakeProvider],
) -> Any:
    """Build a registry_get_provider stub that maps name → zero-arg factory."""

    def _resolver(name: str) -> Any:
        if name not in providers:
            raise KeyError(name)

        def _factory() -> _FakeProvider:
            return providers[name]

        return _factory

    return _resolver


# ---------------------------------------------------------------------------
# Empty / read-only paths
# ---------------------------------------------------------------------------


def test_sweep_empty_ledger_returns_empty_report() -> None:
    report = sweep(
        store=_FakeStore(),  # type: ignore[arg-type]
        ledger=_FakeLedger([]),  # type: ignore[arg-type]
        registry_get_provider=_registry({}),
        thresholds=_THR,
        clock=FakeClock(start=0.0),
        policy=None,
    )
    assert report.snapshot == {}
    assert report.actions == []


def test_sweep_policy_none_skips_all_actions() -> None:
    """Dry-run = policy=None. Snapshot present; actions empty."""
    prov = _FakeProvider(live_ids={"i-1"})
    ledger = _FakeLedger(
        [
            {
                "id": "i-1",
                "provider": "fake",
                "created_at": 0.0,
                "last_heartbeat": 0.0,
                "heartbeat_thread_tick": 499.0,
            }
        ]
    )
    report = sweep(
        store=_FakeStore(),  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        registry_get_provider=_registry({"fake": prov}),
        thresholds=_THR,
        clock=FakeClock(start=500.0),
        policy=None,
    )
    assert report.snapshot["i-1"][1] == Verdict.IDLE_REAP
    assert report.actions == []
    assert prov.destroyed == []
    assert ledger.forgotten == []


# ---------------------------------------------------------------------------
# Provider cache (AC11)
# ---------------------------------------------------------------------------


def test_sweep_caches_list_instances_per_provider() -> None:
    """Two entries → same provider → exactly one list_instances() call."""
    prov = _FakeProvider(live_ids={"i-1", "i-2"})
    entries = [
        {
            "id": "i-1",
            "provider": "fake",
            "created_at": 0.0,
            "last_heartbeat": 100.0,
            "heartbeat_thread_tick": 100.0,
        },
        {
            "id": "i-2",
            "provider": "fake",
            "created_at": 0.0,
            "last_heartbeat": 100.0,
            "heartbeat_thread_tick": 100.0,
        },
    ]
    sweep(
        store=_FakeStore(),  # type: ignore[arg-type]
        ledger=_FakeLedger(entries),  # type: ignore[arg-type]
        registry_get_provider=_registry({"fake": prov}),
        thresholds=_THR,
        clock=FakeClock(start=101.0),
        policy=None,
    )
    assert prov.list_calls == 1


# ---------------------------------------------------------------------------
# list_instances failure → UNROUTABLE (AC12 cousin)
# ---------------------------------------------------------------------------


def test_sweep_list_instances_failure_demotes_provider_to_unroutable() -> None:
    """list_instances raises → all that provider's entries become UNROUTABLE."""
    prov_a = _FakeProvider(live_ids=set(), list_raises=True)
    prov_b = _FakeProvider(live_ids={"i-b"})
    entries = [
        {"id": "i-a", "provider": "broken", "created_at": 0.0},
        {
            "id": "i-b",
            "provider": "fine",
            "created_at": 0.0,
            "last_heartbeat": 100.0,
            "heartbeat_thread_tick": 100.0,
        },
    ]
    report = sweep(
        store=_FakeStore(),  # type: ignore[arg-type]
        ledger=_FakeLedger(entries),  # type: ignore[arg-type]
        registry_get_provider=_registry({"broken": prov_a, "fine": prov_b}),
        thresholds=_THR,
        clock=FakeClock(start=101.0),
        policy=None,
    )
    assert report.snapshot["i-a"][1] == Verdict.UNROUTABLE
    assert report.snapshot["i-b"][1] == Verdict.LIVE


# ---------------------------------------------------------------------------
# Policy dispatch — DEFAULT_APPLY_POLICY routes the right subset
# ---------------------------------------------------------------------------


def test_sweep_default_apply_policy_acts_on_idle_overage_stale() -> None:
    """IDLE_REAP / OVERAGE_REAP / STALE_LEDGER acted; ORPHAN_REAP skipped."""
    # i-idle: sentinel-fresh, hb-stale → IDLE_REAP → act
    # i-orphan: sentinel-stale, past grace → ORPHAN_REAP → NOT acted
    # i-gone: pod_up=False → STALE_LEDGER → act
    prov = _FakeProvider(live_ids={"i-idle", "i-orphan"})
    entries = [
        {
            "id": "i-idle",
            "provider": "fake",
            "created_at": 0.0,
            "last_heartbeat": 0.0,
            "heartbeat_thread_tick": 950.0,  # sent_age=50 < 90 → sentinel-fresh; hb_age=1000 > 100 → IDLE_REAP
        },
        {
            "id": "i-orphan",
            "provider": "fake",
            "created_at": 0.0,
            "last_heartbeat": 10.0,
            "heartbeat_thread_tick": 10.0,
        },
        {"id": "i-gone", "provider": "fake", "created_at": 0.0},
    ]
    report = sweep(
        store=_FakeStore(),  # type: ignore[arg-type]
        ledger=_FakeLedger(entries),  # type: ignore[arg-type]
        registry_get_provider=_registry({"fake": prov}),
        thresholds=_THR,
        clock=FakeClock(start=1_000.0),
        policy=DEFAULT_APPLY_POLICY,
    )
    acted_ids = {a.instance_id for a in report.actions}
    # i-orphan must NOT be acted (ORPHAN_REAP not in DEFAULT_APPLY_POLICY)
    assert "i-orphan" not in acted_ids
    # i-idle + i-gone are acted
    assert {"i-idle", "i-gone"}.issubset(acted_ids)


# ---------------------------------------------------------------------------
# Failure isolation (AC12)
# ---------------------------------------------------------------------------


def test_sweep_one_teardown_failure_does_not_abort_remaining() -> None:
    """First entry's destroy_confirmed raises; second entry still processed."""
    prov = _FakeProvider(live_ids={"i-1", "i-2"}, destroy_raises=True)
    # Both entries IDLE_REAP. i-1 destroy raises; i-2 still attempted.
    entries = [
        {
            "id": "i-1",
            "provider": "fake",
            "created_at": 0.0,
            "last_heartbeat": 0.0,
            "heartbeat_thread_tick": 499.0,
        },
        {
            "id": "i-2",
            "provider": "fake",
            "created_at": 0.0,
            "last_heartbeat": 0.0,
            "heartbeat_thread_tick": 499.0,
        },
    ]
    report = sweep(
        store=_FakeStore(),  # type: ignore[arg-type]
        ledger=_FakeLedger(entries),  # type: ignore[arg-type]
        registry_get_provider=_registry({"fake": prov}),
        thresholds=_THR,
        clock=FakeClock(start=500.0),
        policy=DEFAULT_APPLY_POLICY,
    )
    actions_by_id = {a.instance_id: a for a in report.actions}
    assert actions_by_id["i-1"].action == "failed"
    assert actions_by_id["i-2"].action == "failed"  # destroy still raises
    # Critical: BOTH were attempted (sweep didn't abort after first failure).
    assert len(report.actions) == 2


# ---------------------------------------------------------------------------
# Amendment: UNROUTABLE force-forget path
# ---------------------------------------------------------------------------


def test_sweep_force_forget_unroutable_with_policy_extension() -> None:
    """When policy.act_verdicts includes UNROUTABLE, sweep emits
    forgot_unroutable for each UNROUTABLE entry — without calling
    list_instances or destroy_instance on the unrouted provider.
    """

    # Provider factory raises → entry classified UNROUTABLE.
    def _registry_with_broken(name: str) -> Any:
        def _factory() -> object:
            raise RuntimeError("RUNPOD_API_KEY unset")

        return _factory

    store = _FakeStore()
    ledger = _FakeLedger(
        [
            {"id": "i-orphaned", "provider": "broken", "created_at": 0.0},
        ]
    )
    policy = Policy(act_verdicts=frozenset({Verdict.UNROUTABLE}))

    report = sweep(
        store=store,  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        registry_get_provider=_registry_with_broken,
        thresholds=_THR,
        clock=FakeClock(start=500.0),
        policy=policy,
    )

    assert report.snapshot["i-orphaned"][1] == Verdict.UNROUTABLE
    assert len(report.actions) == 1
    assert report.actions[0].action == "forgot_unroutable"
    assert report.actions[0].instance_id == "i-orphaned"
    assert ledger.forgotten == ["i-orphaned"]
    # Lock acquired for the force-forget.
    assert ("reaper/i-orphaned", 30.0) in store.acquires
