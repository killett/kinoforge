"""Tests for kinoforge.core.lifecycle — Task 17 acceptance criteria.

AC #1: effective_deadline math (pure function).
AC #2: Warm reuse — idle < idle_timeout → same instance returned.
AC #3: After idle_timeout → should_reap True → warm_reuse_or_create destroys + creates new.
AC #4: Graceful drain — max_lifetime trips accepting_new_jobs=False; in-flight continues.
AC #5: In-flight liveness — under effective deadline → is_liveness_OK True even past dead-man window.
AC #6: Idle dead-man — no jobs, no heartbeats → is_liveness_OK False; heartbeat revives it.
AC #7: Lifecycle() defaults carry expected timeout values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.interfaces import Instance, InstanceSpec, Lifecycle
from kinoforge.core.lifecycle import (
    LifecycleManager,
    effective_deadline,
    warm_reuse_or_create,
)
from kinoforge.providers.local import LocalProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_setup(
    idle_timeout_s: float = 2 * 3600,
    job_timeout_s: float = 30 * 60,
    time_buffer_s: float = 30 * 60,
    max_lifetime_s: float = 5 * 3600,
    start: float = 0.0,
) -> tuple[FakeClock, LocalProvider, Lifecycle, LifecycleManager]:
    """Create a FakeClock + LocalProvider + Lifecycle + LifecycleManager bundle."""
    clock = FakeClock(start=start)
    provider = LocalProvider(clock=clock)
    lc = Lifecycle(
        idle_timeout_s=idle_timeout_s,
        job_timeout_s=job_timeout_s,
        time_buffer_s=time_buffer_s,
        max_lifetime_s=max_lifetime_s,
    )
    manager = LifecycleManager(
        provider=provider, clock=clock, lifecycle=lc, run_id="r1"
    )
    return clock, provider, lc, manager


_SPEC = InstanceSpec(image="test")

# ---------------------------------------------------------------------------
# AC #1 — effective_deadline pure function
# ---------------------------------------------------------------------------


def test_effective_deadline_math() -> None:
    """AC #1: 4 segments × 30 min + 30 min buffer == 150 min in seconds."""
    result = effective_deadline(
        num_segments=4, job_timeout_s=30 * 60, time_buffer_s=30 * 60
    )
    assert result == 4 * 30 * 60 + 30 * 60


# ---------------------------------------------------------------------------
# AC #2 — Warm reuse: idle < idle_timeout → same instance returned
# ---------------------------------------------------------------------------


def test_warm_reuse_returns_same_id_when_not_stale() -> None:
    """AC #2: 30 min idle with 2 h timeout → should_reap False → same id returned."""
    clock, provider, _lc, manager = _make_setup(idle_timeout_s=2 * 3600)

    # Create instance
    inst = provider.create_instance(_SPEC)
    manager.register(inst.id, inst.created_at)

    # Start then finish a job
    manager.start_job(inst.id, "job-1", num_segments=1)
    manager.finish_job(inst.id, "job-1")

    # Advance 30 minutes — well within 2-hour idle_timeout
    clock.advance(30 * 60)

    assert not manager.should_reap(inst.id)

    # warm_reuse_or_create should return the SAME id with no new create_instance call
    instances_before = len(provider.list_instances())
    returned_id = warm_reuse_or_create(provider, manager, inst.id, _SPEC)

    assert returned_id == inst.id
    assert len(provider.list_instances()) == instances_before  # no new instance


# ---------------------------------------------------------------------------
# AC #3 — Reap after idle_timeout → new instance created
# ---------------------------------------------------------------------------


def test_reap_after_idle_timeout_creates_new_instance() -> None:
    """AC #3: 3 h idle with 2 h timeout → should_reap True → warm_reuse_or_create creates new."""
    clock, provider, _lc, manager = _make_setup(idle_timeout_s=2 * 3600)

    inst = provider.create_instance(_SPEC)
    manager.register(inst.id, inst.created_at)

    manager.start_job(inst.id, "job-1", num_segments=1)
    manager.finish_job(inst.id, "job-1")

    # Advance 3 hours → past idle_timeout of 2 h
    clock.advance(3 * 3600)

    assert manager.should_reap(inst.id)

    new_id = warm_reuse_or_create(provider, manager, inst.id, _SPEC)

    assert new_id != inst.id
    # Old instance should be destroyed
    assert inst.id not in {i.id for i in provider.list_instances()}
    # New instance should exist
    assert new_id in {i.id for i in provider.list_instances()}


# ---------------------------------------------------------------------------
# AC #4 — Graceful drain
# ---------------------------------------------------------------------------


def test_graceful_drain_refuses_new_jobs_but_keeps_inflight() -> None:
    """AC #4: max_lifetime trips drain; new job refused; in-flight continues under deadline."""
    clock, provider, _lc, manager = _make_setup(
        idle_timeout_s=2 * 3600,
        job_timeout_s=3600,  # 1 h job timeout
        time_buffer_s=0,
        max_lifetime_s=5 * 3600,
    )

    inst = provider.create_instance(_SPEC)
    manager.register(inst.id, inst.created_at)

    # Start an in-flight job at t=0; effective deadline = 1 h → deadline stamp = 3600
    manager.start_job(inst.id, "job-1", num_segments=1)

    # Advance to t=5h — max_lifetime trip
    clock.advance(5 * 3600)

    # should_drain → True and accepting_new_jobs flips False
    assert manager.should_drain(inst.id)
    assert not manager.accepting_new_jobs(inst.id)

    # Attempting to start a new job raises RuntimeError
    with pytest.raises(RuntimeError, match="draining"):
        manager.start_job(inst.id, "job-new", num_segments=1)

    # In-flight job is still recorded (not killed)
    assert manager.in_flight_job(inst.id) is not None

    # is_liveness_OK: job dispatched at t=0 with 1 h deadline → deadline stamp = 3600
    # At t=5h (18000s), past the deadline → liveness False (deadline expired)
    # But the in-flight job was set at t=0 so deadline stamp = 3600; at t=18000 it's expired
    # The test verifies at t=5h which is 18000s > 3600; liveness via heartbeat check falls back
    # Since no heartbeats: last_signal = max(None→0, created_at=0) = 0
    # dead-man window = 2 * idle_timeout_s = 2 * 2h = 4h = 14400s
    # 18000 > 14400 → liveness False — acceptable; drain already decided
    # The key assertion is the in-flight job still exists (not killed by drain)
    # and should_drain is idempotent
    assert manager.should_drain(inst.id)


def test_inflight_liveness_ok_under_effective_deadline() -> None:
    """AC #4 extension: in-flight under effective_deadline → is_liveness_OK True at t=1h."""
    clock, provider, _lc, manager = _make_setup(
        idle_timeout_s=2 * 3600,
        job_timeout_s=3600,
        time_buffer_s=0,
        max_lifetime_s=10 * 3600,  # don't trigger drain in this test
    )

    inst = provider.create_instance(_SPEC)
    manager.register(inst.id, inst.created_at)

    # Start at t=0; effective_deadline = 1 h → deadline stamp = 3600
    manager.start_job(inst.id, "job-1", num_segments=1)

    # At t=30 min: under deadline → liveness OK
    clock.advance(30 * 60)
    assert manager.is_liveness_OK(inst.id)


# ---------------------------------------------------------------------------
# AC #5 — In-flight liveness: under deadline beats dead-man window
# ---------------------------------------------------------------------------


def test_inflight_liveness_beats_dead_man_window() -> None:
    """AC #5: job at t=0 with 6h deadline; at t=5h (past 4h dead-man) → liveness OK."""
    clock, provider, _lc, manager = _make_setup(
        idle_timeout_s=2 * 3600,  # dead-man window = 4 h
        job_timeout_s=6 * 3600,  # 6 h job timeout → deadline stamp = 21600
        time_buffer_s=0,
        max_lifetime_s=24 * 3600,  # don't trigger drain
    )

    inst = provider.create_instance(_SPEC)
    manager.register(inst.id, inst.created_at)

    # Start job at t=0; effective deadline timestamp = 6h = 21600
    manager.start_job(inst.id, "job-1", num_segments=1)

    # Advance to t=5h (18000s) — past dead-man window (14400s), still under deadline (21600s)
    clock.advance(5 * 3600)

    assert manager.is_liveness_OK(inst.id), (
        "in-flight under deadline must keep liveness OK"
    )


# ---------------------------------------------------------------------------
# AC #6 — Idle dead-man
# ---------------------------------------------------------------------------


def test_idle_dead_man_no_heartbeat() -> None:
    """AC #6a: idle with no heartbeats for > 2*idle_timeout → is_liveness_OK False."""
    clock, provider, _lc, manager = _make_setup(
        idle_timeout_s=2 * 3600,  # dead-man window = 4 h
        max_lifetime_s=24 * 3600,
    )

    inst = provider.create_instance(_SPEC)
    manager.register(inst.id, inst.created_at)

    # No jobs ever; advance 5 h
    clock.advance(5 * 3600)

    assert not manager.is_liveness_OK(inst.id)


def test_idle_dead_man_heartbeat_revives() -> None:
    """AC #6b: heartbeat at t=5h → liveness OK at t=5h; stale at t=9h+1s."""
    clock, provider, _lc, manager = _make_setup(
        idle_timeout_s=2 * 3600,  # dead-man window = 4 h
        max_lifetime_s=24 * 3600,
    )

    inst = provider.create_instance(_SPEC)
    manager.register(inst.id, inst.created_at)

    # Advance 5 h, then heartbeat
    clock.advance(5 * 3600)
    provider.heartbeat(inst.id)

    # At t=5h: heartbeat just sent → liveness OK
    assert manager.is_liveness_OK(inst.id)

    # Advance to t=9h+1s: heartbeat sent at t=5h; dead-man window = 4h → expires at t=9h
    clock.advance(4 * 3600 + 1)

    assert not manager.is_liveness_OK(inst.id)


# ---------------------------------------------------------------------------
# AC #7 — Lifecycle() defaults
# ---------------------------------------------------------------------------


def test_lifecycle_defaults() -> None:
    """AC #7: Lifecycle() defaults have the documented timeout values."""
    lc = Lifecycle()
    assert lc.idle_timeout_s == 2 * 3600
    assert lc.job_timeout_s == 30 * 60
    assert lc.max_lifetime_s == 5 * 3600


# ---------------------------------------------------------------------------
# Layer S — Ledger.record schema extension (idle_timeout_s + max_age_s)
# ---------------------------------------------------------------------------


def _layer_s_make_instance(iid: str = "i-1") -> Instance:
    """Construct a minimal Instance for Layer S Ledger.record tests."""
    return Instance(
        id=iid,
        provider="fake",
        status="ready",
        endpoints={},
        tags={},
        created_at=1717635791.0,
        cost_rate_usd_per_hr=0.35,
    )


def test_record_persists_idle_timeout_s_and_max_age_s(tmp_path: Path) -> None:
    """Ledger.record with both new kwargs writes them into the JSON entry.

    Bug-catch: if the new kwargs were accepted but dropped on the floor,
    `kinoforge status` would silently fall back to `<not in ledger>` even
    on freshly recorded entries.
    """
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.local import LocalArtifactStore

    ledger = Ledger(store=LocalArtifactStore(tmp_path), run_id="_lifecycle")

    ledger.record(_layer_s_make_instance(), idle_timeout_s=900, max_age_s=14400)

    entries = ledger.entries()
    assert len(entries) == 1
    assert entries[0]["idle_timeout_s"] == 900
    assert entries[0]["max_age_s"] == 14400
    assert isinstance(entries[0]["idle_timeout_s"], int)
    assert isinstance(entries[0]["max_age_s"], int)


def test_record_omits_new_keys_when_kwargs_none(tmp_path: Path) -> None:
    """Backwards-compat: record() without kwargs writes the legacy entry shape.

    Bug-catch: if a default-None kwarg accidentally persisted as `null`, legacy
    consumers that switch on `key in entry` would flip behavior.
    """
    import json

    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.local import LocalArtifactStore

    ledger = Ledger(store=LocalArtifactStore(tmp_path), run_id="_lifecycle")

    ledger.record(_layer_s_make_instance())

    # Disk layout: <root>/<run_id>/ledger.json containing {"entries": [...]}.
    on_disk = json.loads((tmp_path / "_lifecycle" / "ledger.json").read_text())
    entry = on_disk["entries"][0]
    assert "idle_timeout_s" not in entry
    assert "max_age_s" not in entry


def test_entries_reads_legacy_entry_without_new_keys(tmp_path: Path) -> None:
    """A ledger.json written before this layer must read cleanly.

    Bug-catch: if the new fields became required, this would KeyError on
    every read after upgrade.
    """
    import json

    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.local import LocalArtifactStore

    target = tmp_path / "_lifecycle" / "ledger.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "legacy-1",
                        "provider": "runpod",
                        "tags": {},
                        "created_at": 1700000000.0,
                        "cost_rate_usd_per_hr": 0.35,
                    }
                ]
            }
        )
    )

    ledger = Ledger(store=LocalArtifactStore(tmp_path), run_id="_lifecycle")
    entries = ledger.entries()

    assert len(entries) == 1
    assert entries[0]["id"] == "legacy-1"
    assert "idle_timeout_s" not in entries[0]
    assert "max_age_s" not in entries[0]


# ---------------------------------------------------------------------------
# Phase 34 T1 — _compute_uri delegates to store.uri_for (no isinstance switch)
# ---------------------------------------------------------------------------


def test_ledger_compute_uri_delegates_to_store_uri_for() -> None:
    """Bug-catch: prevents reintroduction of the isinstance switch.

    A future edit that re-adds isinstance(LocalArtifactStore) checking
    breaks the universal ABC contract. This test calls _compute_uri on a
    NON-Local store and asserts it returns the result of store.uri_for —
    not raises TypeError.
    """
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.s3 import S3ArtifactStore
    from tests.stores.conftest import FakeS3Client

    fake = FakeS3Client()
    store = S3ArtifactStore(bucket="b", prefix="p", client=fake)

    ledger = Ledger(store=store, run_id="_lifecycle")

    assert ledger._compute_uri() == store.uri_for("_lifecycle", "ledger.json")


def test_ledger_round_trip_against_fake_s3() -> None:
    """Record + entries() round-trips through fake S3 store."""
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.s3 import S3ArtifactStore
    from tests.stores.conftest import FakeS3Client

    fake = FakeS3Client()
    store = S3ArtifactStore(bucket="b", prefix="p", client=fake)
    ledger = Ledger(store=store, run_id="_lifecycle")

    inst = Instance(
        id="i-1",
        provider="local",
        status="ready",
        tags={"kinoforge_key": "abc"},
        created_at=1000.0,
        cost_rate_usd_per_hr=0.5,
    )
    ledger.record(inst)

    entries = ledger.entries()
    assert len(entries) == 1
    assert entries[0]["id"] == "i-1"
    assert entries[0]["provider"] == "local"


def test_ledger_round_trip_against_fake_gcs() -> None:
    """Same contract against fake GCS — proves both clouds work."""
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.gcs import GCSArtifactStore
    from tests.stores.conftest import FakeGCSClient

    fake = FakeGCSClient()
    store = GCSArtifactStore(
        bucket="b",
        prefix="p",
        client=fake,
        not_found_exc=fake.NotFound,
    )
    ledger = Ledger(store=store, run_id="_lifecycle")

    inst = Instance(
        id="i-2",
        provider="local",
        status="ready",
        tags={},
        created_at=2000.0,
        cost_rate_usd_per_hr=0.0,
    )
    ledger.record(inst)
    assert [e["id"] for e in ledger.entries()] == ["i-2"]


# ---------------------------------------------------------------------------
# Layer V — grace_after_session_s field on Lifecycle dataclass
# ---------------------------------------------------------------------------


def test_lifecycle_grace_after_session_s_default_is_300() -> None:
    """Layer V: default 5-minute post-session warm-reuse grace window."""
    from kinoforge.core.interfaces import Lifecycle

    assert Lifecycle().grace_after_session_s == 300.0


def test_lifecycle_grace_after_session_s_round_trips() -> None:
    """Constructor accepts an explicit override."""
    from kinoforge.core.interfaces import Lifecycle

    assert Lifecycle(grace_after_session_s=42.0).grace_after_session_s == 42.0
