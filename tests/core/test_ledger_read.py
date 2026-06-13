"""B7 T1: Ledger.read per-id lookup.

Read-only mirror of the record/forget/touch per-id surface. Returns the
matching entry dict, or None when absent (including post-forget).
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.local import LocalArtifactStore


def _make_instance(instance_id: str = "i-read", *, provider: str = "local") -> Instance:
    return Instance(
        id=instance_id,
        provider=provider,
        status="ready",
        created_at=0.0,
        cost_rate_usd_per_hr=0.0,
        tags={},
    )


def test_read_returns_entry_for_recorded_id(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-known")
    ledger.record(inst)

    entry = ledger.read("i-known")

    assert entry is not None
    assert entry["id"] == "i-known"
    assert entry["provider"] == "local"


def test_read_returns_none_for_unknown_id(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)

    assert ledger.read("never-recorded") is None


def test_read_returns_none_after_forget(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-forget")
    ledger.record(inst)
    assert ledger.read("i-forget") is not None

    ledger.forget("i-forget")

    assert ledger.read("i-forget") is None


def test_read_does_not_acquire_mutate_lock(tmp_path: Path) -> None:
    """read() is read-only — must NOT acquire ledger/<run_id> mutate lock.

    Holding the mutate lock during read would contend with concurrent touch()
    calls from HeartbeatLoop. Verify by recording inside a held mutate lock
    and confirming read() still returns the entry without blocking.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-nolock")
    ledger.record(inst)

    with store.acquire_lock("ledger/_lifecycle", ttl_s=30.0):
        entry = ledger.read("i-nolock")

    assert entry is not None
    assert entry["id"] == "i-nolock"
