"""Layer V T4: act_on_verdict + provider_for tests.

Covers spec §3.5 acceptance criteria AC9–AC10 plus per-verdict
dispatch and TeardownError isolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import AuthError, TeardownError, UnknownAdapter
from kinoforge.core.interfaces import Instance
from kinoforge.core.reaper import Verdict
from kinoforge.core.reaper_actor import (
    act_on_verdict,
    provider_for,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Tracks calls. ``live_ids`` controls what list_instances returns."""

    def __init__(self, live_ids: set[str] | None = None) -> None:
        self.live_ids: set[str] = set(live_ids) if live_ids else set()
        self.destroyed: list[str] = []
        self.list_calls: int = 0
        self._raise_on_destroy: bool = False

    def list_instances(self) -> list[Instance]:
        self.list_calls += 1
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
        if self._raise_on_destroy:
            raise TeardownError("simulated network error")
        self.destroyed.append(instance_id)
        self.live_ids.discard(instance_id)

    # Needed by destroy_confirmed's post-destroy verification
    def get_instance(self, instance_id: str) -> Instance:
        raise KeyError(instance_id)


class _FakeStore:
    """Captures lock acquires; provides a context-manager dummy lock."""

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
    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)


_THR: Mapping[str, Any] = dict(
    idle_timeout_s=100.0,
    max_lifetime_s=10_000.0,
    heartbeat_interval_s=30.0,
    grace_after_session_s=500.0,
)


def _entry(id_: str = "i-1", **overrides: Any) -> dict[str, Any]:
    """Default-fresh entry suitable for IDLE_REAP-on-re-classify tests."""
    base: dict[str, Any] = {
        "id": id_,
        "provider": "fake",
        "created_at": 0.0,
        "last_heartbeat": 0.0,  # very old
        "heartbeat_thread_tick": 0.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# provider_for — caching + failure modes
# ---------------------------------------------------------------------------


def test_provider_for_caches_by_provider_name() -> None:
    """Two entries with same provider name → one factory call."""
    factory = MagicMock(return_value=_FakeProvider())
    registry = MagicMock(return_value=factory)
    cache: dict[str, Any] = {}
    e1 = {"id": "a", "provider": "runpod"}
    e2 = {"id": "b", "provider": "runpod"}

    p1 = provider_for(e1, registry, cache)
    p2 = provider_for(e2, registry, cache)

    assert p1 is p2
    assert factory.call_count == 1


def test_provider_for_returns_none_on_unknown_adapter() -> None:
    factory = MagicMock(side_effect=UnknownAdapter("nope"))
    registry = MagicMock(return_value=factory)
    cache: dict[str, Any] = {}

    result = provider_for({"id": "a", "provider": "bogus"}, registry, cache)

    assert result is None
    assert cache["bogus"] is None


def test_provider_for_returns_none_on_auth_error() -> None:
    factory = MagicMock(side_effect=AuthError("RUNPOD_API_KEY unset"))
    registry = MagicMock(return_value=factory)
    result = provider_for({"id": "a", "provider": "runpod"}, registry, {})
    assert result is None


def test_provider_for_returns_none_on_generic_exception() -> None:
    """Any vendor SDK exception during construction → unroutable, never crash."""
    factory = MagicMock(side_effect=RuntimeError("network down"))
    registry = MagicMock(return_value=factory)
    result = provider_for({"id": "a", "provider": "runpod"}, registry, {})
    assert result is None


# ---------------------------------------------------------------------------
# act_on_verdict — lock acquisition (AC10)
# ---------------------------------------------------------------------------


def test_act_on_verdict_acquires_per_instance_lock() -> None:
    """Lock key is `reaper/<id>` with ttl_s=30.0."""
    store = _FakeStore()
    ledger = _FakeLedger()
    # Set up so that re-classify returns LIVE → no destruction; we only
    # care about the lock acquire side effect.
    provider = _FakeProvider(live_ids={"i-1"})
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=1.0,
        heartbeat_thread_tick=1.0,
    )
    clock = FakeClock(start=1.0)  # everything sentinel-fresh + hb-fresh → LIVE
    act_on_verdict(
        store,  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        e,
        Verdict.IDLE_REAP,
        thresholds=_THR,
        clock=clock,
    )
    assert store.acquires == [("reaper/i-1", 30.0)]


# ---------------------------------------------------------------------------
# act_on_verdict — drift skip (AC9)
# ---------------------------------------------------------------------------


def test_act_on_verdict_drift_skips_destruction() -> None:
    """Snapshot=ORPHAN_REAP; re-classify=LIVE → skipped, no destroy."""
    store = _FakeStore()
    ledger = _FakeLedger()
    # Pod up, sentinel fresh, hb fresh → re-classify yields LIVE.
    provider = _FakeProvider(live_ids={"i-1"})
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=1.0,
        heartbeat_thread_tick=1.0,
    )
    clock = FakeClock(start=2.0)

    result = act_on_verdict(
        store,  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        e,
        Verdict.ORPHAN_REAP,
        thresholds=_THR,
        clock=clock,
    )

    assert result.action == "skipped"
    assert result.reason is not None and "drift" in result.reason
    assert provider.destroyed == []
    assert ledger.forgotten == []


# ---------------------------------------------------------------------------
# act_on_verdict — destruction paths
# ---------------------------------------------------------------------------


def test_act_on_verdict_idle_reap_destroys_and_forgets() -> None:
    """IDLE_REAP confirmed → destroy_confirmed + ledger.forget."""
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids={"i-1"})
    # hb very old → re-classify yields IDLE_REAP (sentinel-fresh, hb-stale)
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=0.0,
        heartbeat_thread_tick=499.0,
    )
    clock = FakeClock(start=500.0)

    result = act_on_verdict(
        store,  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        e,
        Verdict.IDLE_REAP,
        thresholds=_THR,
        clock=clock,
    )

    assert result.action == "destroyed_and_forgot"
    assert provider.destroyed == ["i-1"]
    assert ledger.forgotten == ["i-1"]


def test_act_on_verdict_orphan_reap_destroys_and_forgets() -> None:
    """ORPHAN_REAP confirmed (sentinel-stale + past grace) → destroy + forget."""
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids={"i-1"})
    # sentinel-stale; pod_age > grace → ORPHAN_REAP both times
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=10.0,
        heartbeat_thread_tick=10.0,
    )
    clock = FakeClock(start=1_000.0)  # sent_age=990>90; pod_age=1000>500

    result = act_on_verdict(
        store,  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        e,
        Verdict.ORPHAN_REAP,
        thresholds=_THR,
        clock=clock,
    )

    assert result.action == "destroyed_and_forgot"
    assert provider.destroyed == ["i-1"]


def test_act_on_verdict_stale_ledger_only_forgets() -> None:
    """STALE_LEDGER → ledger.forget; never call destroy_instance."""
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids=set())  # pod_up=False both times
    e = _entry(id_="i-1")
    clock = FakeClock(start=500.0)

    result = act_on_verdict(
        store,  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        e,
        Verdict.STALE_LEDGER,
        thresholds=_THR,
        clock=clock,
    )

    assert result.action == "forgot"
    assert provider.destroyed == []
    assert ledger.forgotten == ["i-1"]


def test_act_on_verdict_unroutable_only_forgets() -> None:
    """UNROUTABLE → ledger.forget (callers only reach this with --force-forget)."""
    store = _FakeStore()
    ledger = _FakeLedger()
    # Provider doesn't matter; we pre-stamp UNROUTABLE snapshot.
    provider = _FakeProvider(live_ids=set())
    e = _entry(id_="i-1")
    clock = FakeClock(start=500.0)

    # NB: classify never returns UNROUTABLE — so to test the action="forgot_unroutable"
    # path, we need snapshot=UNROUTABLE AND re-classify must also yield UNROUTABLE.
    # But classify can't yield UNROUTABLE. So we exercise via the drift-skip code path
    # by asserting that an UNROUTABLE snapshot always drifts → "skipped". That tests
    # the safe-by-default branch. The "forgot_unroutable" branch is exercised by
    # test_reaper_sweep with a mocked classify.
    result = act_on_verdict(
        store,  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        e,
        Verdict.UNROUTABLE,
        thresholds=_THR,
        clock=clock,
    )
    # re-classify yields STALE_LEDGER (pod_up=False) — drift
    assert result.action == "skipped"
    assert ledger.forgotten == []


def test_act_on_verdict_live_is_no_op() -> None:
    """LIVE snapshot + re-classify LIVE → no destroy, no forget."""
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids={"i-1"})
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=1.0,
        heartbeat_thread_tick=1.0,
    )
    clock = FakeClock(start=2.0)
    result = act_on_verdict(
        store,  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        e,
        Verdict.LIVE,
        thresholds=_THR,
        clock=clock,
    )
    assert result.action == "no_op"
    assert provider.destroyed == []
    assert ledger.forgotten == []


# ---------------------------------------------------------------------------
# act_on_verdict — TeardownError isolation
# ---------------------------------------------------------------------------


def test_act_on_verdict_swallows_teardown_error() -> None:
    """TeardownError from destroy_confirmed → ActionResult(action='failed').

    Must not propagate out of act_on_verdict — sweep continues across
    one-instance failures.
    """
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids={"i-1"})
    provider._raise_on_destroy = True
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=0.0,
        heartbeat_thread_tick=499.0,
    )
    clock = FakeClock(start=500.0)

    result = act_on_verdict(
        store,  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        e,
        Verdict.IDLE_REAP,
        thresholds=_THR,
        clock=clock,
    )

    assert result.action == "failed"
    assert result.reason is not None
    # Ledger.forget MUST NOT have been called — destroyer didn't confirm.
    assert ledger.forgotten == []
