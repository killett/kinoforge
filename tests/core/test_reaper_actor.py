"""Layer V T4: act_on_verdict + provider_for tests.

Covers spec §3.5 acceptance criteria AC9–AC10 plus per-verdict
dispatch and TeardownError isolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock

import pytest

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

    def uri_for(self, namespace: str, filename: str) -> str:
        return f"fake://{namespace}/{filename}"

    def get_json(self, uri: str) -> dict[str, Any]:
        raise FileNotFoundError(uri)


class _FakeLock:
    def __enter__(self) -> _FakeLock:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    # B7: the probe helper calls acquire(blocking=False) on the lock
    # returned from store.acquire_lock("provision:<id>", ...). The fake
    # store's lock is always "free" — returns a sentinel token.
    def acquire(
        self, *, blocking: bool = True, timeout_s: float | None = None
    ) -> object | None:
        return object()

    def release(self, token: object) -> None:
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
    # B7: act_on_verdict acquires the per-instance reaper lock first, then
    # non-blocking-probes provision:<id> before the destroy flow.
    assert store.acquires == [("reaper/i-1", 30.0), ("provision:i-1", 0.0)]


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
    assert ledger.forgotten == ["i-1"]


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


def test_act_on_verdict_unroutable_snapshot_drifts_to_stale_ledger() -> None:
    """UNROUTABLE-snapshot calls drift to STALE_LEDGER on re-classify.

    classify() never returns UNROUTABLE (T1 contract), so a caller
    passing snapshot_verdict=UNROUTABLE will always observe drift on
    re-classify. In practice, sweep() never reaches act_on_verdict
    for UNROUTABLE entries (no provider to invoke); the
    `forgot_unroutable` action is produced directly in sweep() — see
    Layer V T5. This test documents the drift behaviour for any
    hypothetical caller that passes UNROUTABLE.
    """
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids=set())  # pod_up=False → STALE_LEDGER
    e = _entry(id_="i-1")
    clock = FakeClock(start=500.0)

    result = act_on_verdict(
        store,  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        e,
        Verdict.UNROUTABLE,
        thresholds=_THR,
        clock=clock,
    )

    assert result.action == "skipped"
    assert result.applied_verdict == Verdict.STALE_LEDGER
    assert provider.destroyed == []
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


# ---------------------------------------------------------------------------
# B5a Task d — HEARTBEAT_SUBSTRATE_MISSING no-destroy + WARN-once arm
# ---------------------------------------------------------------------------


def test_act_on_verdict_substrate_missing_does_not_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conservative-on-ignorance contract: operator cannot fix the
    substrate by destroying the pod; sweeper must skip.

    Bug catch: a forgotten case-arm could fall through to the destroy
    branch and silently kill working SkyPilot pods during the
    B5a-shipped-B5b-pending window.
    """
    from kinoforge.core.clock import FakeClock
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import act_on_verdict

    # Build fakes for store, ledger, provider — only the destroy and
    # forget calls matter for this assertion.
    class _DummyLock:
        def __enter__(self) -> _DummyLock:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def acquire(
            self, *, blocking: bool = True, timeout_s: float | None = None
        ) -> object | None:
            return object()

        def release(self, _token: object) -> None:
            return None

    class _StubStore:
        def acquire_lock(self, _key: str, ttl_s: float = 30.0) -> _DummyLock:
            return _DummyLock()

    destroy_calls: list[str] = []
    forget_calls: list[str] = []

    class _StubProvider:
        def list_instances(self) -> list[Instance]:
            return [
                Instance(
                    id="pod-x", provider="skypilot", created_at=1_000.0, status="ready"
                )
            ]

        def destroy_instance(self, instance_id: str) -> None:
            destroy_calls.append(instance_id)

    class _StubLedger:
        def forget(self, instance_id: str) -> None:
            forget_calls.append(instance_id)

    entry = {
        "id": "pod-x",
        "provider_kind": "skypilot",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    result = act_on_verdict(
        store=_StubStore(),  # type: ignore[arg-type]
        ledger=_StubLedger(),  # type: ignore[arg-type]
        provider=_StubProvider(),  # type: ignore[arg-type]
        entry=entry,
        snapshot_verdict=Verdict.HEARTBEAT_SUBSTRATE_MISSING,
        thresholds={
            "idle_timeout_s": 600.0,
            "max_lifetime_s": 18_000.0,
            "heartbeat_interval_s": 30.0,
            "grace_after_session_s": 300.0,
        },
        clock=FakeClock(start=2_000.0),
    )
    assert destroy_calls == []
    assert forget_calls == []
    assert result.action == "no_op"
    assert result.applied_verdict == Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_act_on_verdict_substrate_missing_warns_once_per_pair(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Across N sweeps over the same (provider_kind, instance_id) pair,
    only the FIRST WARNING fires. Operators don't want 100 lines of
    'skypilot has no substrate' per minute."""
    import logging

    from kinoforge.core.clock import FakeClock
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import act_on_verdict, reset_warning_dedup

    reset_warning_dedup()  # test-helper from reaper_actor

    class _DummyLock:
        def __enter__(self) -> _DummyLock:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def acquire(
            self, *, blocking: bool = True, timeout_s: float | None = None
        ) -> object | None:
            return object()

        def release(self, _token: object) -> None:
            return None

    class _StubStore:
        def acquire_lock(self, _key: str, ttl_s: float = 30.0) -> _DummyLock:
            return _DummyLock()

    class _StubProvider:
        def list_instances(self) -> list[Instance]:
            return [
                Instance(
                    id="cluster-x",
                    provider="skypilot",
                    created_at=1_000.0,
                    status="ready",
                )
            ]

        def destroy_instance(self, _instance_id: str) -> None:
            pass

    class _StubLedger:
        def forget(self, _instance_id: str) -> None:
            pass

    entry = {
        "id": "cluster-x",
        "provider_kind": "skypilot",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    caplog.set_level(logging.WARNING, logger="kinoforge.core.reaper_actor")

    for _ in range(5):
        act_on_verdict(
            store=_StubStore(),  # type: ignore[arg-type]
            ledger=_StubLedger(),  # type: ignore[arg-type]
            provider=_StubProvider(),  # type: ignore[arg-type]
            entry=entry,
            snapshot_verdict=Verdict.HEARTBEAT_SUBSTRATE_MISSING,
            thresholds={
                "idle_timeout_s": 600.0,
                "max_lifetime_s": 18_000.0,
                "heartbeat_interval_s": 30.0,
                "grace_after_session_s": 300.0,
            },
            clock=FakeClock(start=2_000.0),
        )

    # Only one WARNING log line per (provider_kind, instance_id)
    relevant = [
        r for r in caplog.records if "heartbeat substrate" in r.getMessage().lower()
    ]
    assert len(relevant) == 1


# ---------------------------------------------------------------------------
# B7 — act_on_verdict non-blocking probe of provision:<id>
# ---------------------------------------------------------------------------


def test_act_on_verdict_defers_when_provision_lock_held(
    tmp_path: Any,
) -> None:
    """B7: orchestrator-side holds provision:<id>; reaper probe sees the
    sidecar holder_pid; act_on_verdict returns deferred-session-claim and
    skips destroy.

    Bug catch: a forgotten probe would race the orchestrator's first-tick
    polling window — reaper destroys the boot-mid pod the session is
    still claiming.
    """
    import os

    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path)
    provision_lock = store.acquire_lock("provision:i-deferred", ttl_s=300.0)
    token = provision_lock.acquire(blocking=True, timeout_s=5.0)
    assert token is not None
    try:
        ledger = _FakeLedger()
        provider = _FakeProvider(live_ids={"i-deferred"})
        entry = _entry(
            id_="i-deferred",
            created_at=0.0,
            last_heartbeat=0.0,
            heartbeat_thread_tick=0.0,
        )
        clock = FakeClock(start=1.0)

        result = act_on_verdict(
            store,
            ledger,  # type: ignore[arg-type]
            provider,  # type: ignore[arg-type]
            entry,
            Verdict.IDLE_REAP,
            thresholds=_THR,
            clock=clock,
        )

        assert result.action == "deferred-session-claim"
        assert f"pid {os.getpid()}" in (result.reason or "")
        assert provider.destroyed == []
        # Re-classify never ran on the defer path.
        assert result.applied_verdict == Verdict.IDLE_REAP
    finally:
        provision_lock.release(token)


def test_act_on_verdict_proceeds_after_provision_lock_released(
    tmp_path: Any,
) -> None:
    """B7: when provision:<id> is free, probe-success releases instantly
    and act_on_verdict continues into re-classify + destroy.

    Discriminating: pins down probe-success-immediate-release. A bug that
    forgot to release the probe would deadlock the next sweep.
    """
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path)
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids={"i-released"})
    entry = _entry(
        id_="i-released",
        created_at=0.0,
        last_heartbeat=0.0,
        heartbeat_thread_tick=499.0,
    )
    # Sentinel-fresh (499 within 3*30=90s of 500) + hb-stale (500-0 > 100)
    # → classify yields IDLE_REAP confirming the snapshot.
    clock = FakeClock(start=500.0)

    result = act_on_verdict(
        store,
        ledger,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        entry,
        Verdict.IDLE_REAP,
        thresholds=_THR,
        clock=clock,
    )

    assert result.action == "destroyed_and_forgot"
    assert provider.destroyed == ["i-released"]
    # Probe MUST release: a second act_on_verdict against the same store
    # would block forever if the probe leaked the file lock.
    second_provider = _FakeProvider(live_ids={"i-second"})
    second_entry = _entry(
        id_="i-second",
        created_at=0.0,
        last_heartbeat=0.0,
        heartbeat_thread_tick=499.0,
    )
    second = act_on_verdict(
        store,
        ledger,  # type: ignore[arg-type]
        second_provider,  # type: ignore[arg-type]
        second_entry,
        Verdict.IDLE_REAP,
        thresholds=_THR,
        clock=clock,
    )
    assert second.action == "destroyed_and_forgot"


# ---------------------------------------------------------------------------
# B3 Task h — reaper integration delta
# ---------------------------------------------------------------------------


def test_act_on_verdict_blocks_when_b3_no_reuse_destroy_holds_reaper_lock(
    tmp_path: Any,
) -> None:
    """Bug: B1 not blocking on B3 --no-reuse's reaper:<id> would double-destroy
    (race condition; second destroy raises on phantom pod)."""
    import threading
    import time as _time

    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    provider = _FakeProvider(live_ids={"pod-1"})
    ledger.record(
        Instance(
            id="pod-1",
            provider="fake",
            status="ready",
            created_at=0.0,
            cost_rate_usd_per_hr=0.5,
            tags={},
        )
    )

    b1_done = threading.Event()
    b1_result: dict[str, Any] = {}
    b1_started = threading.Event()

    def b1_worker() -> None:
        try:
            b1_started.set()
            res = act_on_verdict(
                store,
                ledger,
                provider,  # type: ignore[arg-type]
                _entry(id_="pod-1"),
                Verdict.IDLE_REAP,
                thresholds=_THR,
                clock=FakeClock(1000.0),
            )
            b1_result["action"] = res.action
        finally:
            b1_done.set()

    # Main thread = "B3 --no-reuse" holding reaper:<id>.
    with store.acquire_lock("reaper/pod-1", ttl_s=30.0):
        t = threading.Thread(target=b1_worker, daemon=True)
        t.start()
        # Wait for B1 to start (and block at the lock acquire).
        b1_started.wait(timeout=5.0)
        _time.sleep(0.3)  # Give B1 time to attempt acquire + block on poll.
        # While we hold the lock: B3 sim-destroys + forgets pod-1.
        provider.destroy_instance("pod-1")
        ledger.forget("pod-1")
        # Confirm B1 has NOT yet completed (it's polling our lock).
        assert not b1_done.is_set(), "B1 ran before --no-reuse released lock"
    # Release: B1 acquires, re-classifies, finds live_ids empty → STALE_LEDGER.
    t.join(timeout=10.0)
    assert b1_done.is_set()

    # No double-destroy: only B3's destroy is recorded.
    assert provider.destroyed == ["pod-1"]
    # B1 re-classified and saw STALE_LEDGER → forgot (no-op since already gone)
    # OR drift skipped. Either way action is non-destructive.
    assert b1_result.get("action") in {"forgot", "skipped"}
