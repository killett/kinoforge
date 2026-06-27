"""B7 T1: Ledger.read per-id lookup.

Read-only mirror of the record/forget/touch per-id surface. Returns the
matching entry dict, or None when absent (including post-forget).
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.ephemeral import EphemeralSession
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


def test_read_returns_recorded_entry_under_ephemeral(tmp_path: Path) -> None:
    """Under EphemeralSession(enabled=True), record() then read() must round-trip.

    The 2026-06-26/2026-06-27 hold_until_first_tick hang + lost-warm-pod bug
    was caused by ``_read_entries`` reading from disk while ``_write_entries``
    (under STRICT_POLICY.ledger_record=False) only wrote to
    ``session.in_memory_ledger``. Result: every ``Ledger.read`` after
    ``Ledger.record`` returned ``None``, every ``Ledger.touch`` no-op'd (its
    inner ``_read_entries`` missed the entry), every heartbeat tick failed to
    land, and ``hold_until_first_tick`` polled the 61-minute timeout window.

    Would-fail-bug: the asymmetric read path returns ``None`` here because the
    on-disk ledger file was never created during ``record()``.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-ephem")

    with EphemeralSession(enabled=True):
        ledger.record(inst)
        entry = ledger.read("i-ephem")

    assert entry is not None, (
        "ephemeral asymmetry regression: record() stashed entry in "
        "session.in_memory_ledger but read() bypassed it and read empty disk"
    )
    assert entry["id"] == "i-ephem"
    assert entry["provider"] == "local"


def test_touch_lands_under_ephemeral(tmp_path: Path) -> None:
    """Under EphemeralSession(enabled=True), touch() must mutate the recorded entry.

    Direct guard for the heartbeat tick path: ``HeartbeatLoop._tick_once`` calls
    ``Ledger.touch(instance_id, last_heartbeat=..., heartbeat_thread_tick=...)``
    on a 30s cadence. ``touch`` is a strict update — when ``_read_entries``
    bypasses the in-memory ledger under ephemeral, the entry is "missing", touch
    silently returns False, and ``heartbeat_thread_tick`` never lands anywhere
    reachable by ``hold_until_first_tick``'s polling loop.

    Would-fail-bug: ``touch`` returns False under ephemeral and the subsequent
    ``read`` shows no ``heartbeat_thread_tick`` field.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-touch-ephem")

    with EphemeralSession(enabled=True):
        ledger.record(inst)
        wrote = ledger.touch(
            "i-touch-ephem",
            last_heartbeat=123.456,
            heartbeat_thread_tick=789.012,
        )
        entry = ledger.read("i-touch-ephem")

    assert wrote is True, (
        "ephemeral asymmetry regression: touch() returned False because its "
        "internal _read_entries missed the in-memory entry"
    )
    assert entry is not None
    assert entry["heartbeat_thread_tick"] == 789.012
    assert entry["last_heartbeat"] == 123.456


def test_entries_lists_recorded_under_ephemeral(tmp_path: Path) -> None:
    """``Ledger.entries()`` must surface in-memory entries under ephemeral.

    Used by ``_print_instance_overview`` + ``_scan_warm_candidates`` for
    warm-attach. The 2026-06-26 bug surfaced as
    ``[instance overview] No running instances.`` despite a freshly-recorded
    pod, because the empty disk read shadowed the in-memory ledger.

    Would-fail-bug: ``entries()`` returns [] under ephemeral despite a prior
    ``record`` call, so ``_scan_warm_candidates`` cold-boots a new pod.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst_a = _make_instance("i-list-a")
    inst_b = _make_instance("i-list-b")

    with EphemeralSession(enabled=True):
        ledger.record(inst_a)
        ledger.record(inst_b)
        entries = ledger.entries()

    ids = sorted(e["id"] for e in entries)
    assert ids == ["i-list-a", "i-list-b"], (
        f"expected both recorded ids visible under ephemeral; got {ids!r}"
    )


def test_ephemeral_bootstraps_from_existing_disk_state(tmp_path: Path) -> None:
    """A pre-existing on-disk ledger entry must remain visible inside an
    EphemeralSession opened AFTER the entry was written.

    Warm-attach matchers (``_scan_warm_candidates`` / ``find_warm_attach_candidate``)
    rely on reading entries recorded by a prior non-ephemeral run. A strict
    "in-memory only" read inside an ephemeral session would shadow those
    entries to empty, regressing
    ``tests/integration/test_warm_reuse_lora_ephemeral.py::test_ephemeral_run_registers_refs_and_skips_on_disk_ledger``.

    Would-fail-bug: a fix that returns ``[]`` whenever the in-memory mirror is
    empty (rather than bootstrapping from disk on first read) breaks every
    ephemeral attach to a previously-provisioned pod.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-prior-disk")
    ledger.record(inst)

    with EphemeralSession(enabled=True):
        entry = ledger.read("i-prior-disk")
        all_entries = ledger.entries()

    assert entry is not None
    assert entry["id"] == "i-prior-disk"
    assert [e["id"] for e in all_entries] == ["i-prior-disk"]


def test_ephemeral_writes_do_not_leak_to_disk_after_bootstrap(tmp_path: Path) -> None:
    """Bootstrap-then-write must keep the on-disk ledger frozen at pre-session state.

    The bootstrap copies disk into in-memory; subsequent writes
    (``record`` / ``touch`` / ``forget``) must land in-memory only, leaving
    the disk file at its pre-session contents. Without this, the
    ``policy.ledger_record=False`` confidentiality gate is silently bypassed.

    Would-fail-bug: a bootstrap that wrote back to disk after each modification
    (or one that returned a disk-backed read object) would leak the
    ephemeral-only entries to disk.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    ledger.record(_make_instance("i-disk-only"))
    disk_before = (tmp_path / "_lifecycle" / "ledger.json").read_text()

    with EphemeralSession(enabled=True):
        ledger.record(_make_instance("i-ephem-only"))
        ledger.touch("i-ephem-only", custom_field="ephem-value")
        inside = ledger.entries()

    disk_after = (tmp_path / "_lifecycle" / "ledger.json").read_text()
    assert disk_before == disk_after, (
        "ephemeral writes leaked to disk; on-disk ledger.json changed"
    )
    inside_ids = sorted(e["id"] for e in inside)
    assert inside_ids == ["i-disk-only", "i-ephem-only"]


def test_default_policy_still_uses_disk_under_ephemeral_session(tmp_path: Path) -> None:
    """Lockdown: an EphemeralSession with enabled=False must NOT divert to in-memory.

    The CLI wraps every invocation in ``EphemeralSession(enabled=args.ephemeral)``
    — even when the operator did not pass ``--ephemeral`` — and the disabled
    session binds DEFAULT_POLICY which has ``ledger_record=True``. The fix must
    only divert when ``policy.ledger_record is False``, otherwise non-ephemeral
    runs would silently lose the on-disk ledger.

    Would-fail-bug: a fix that diverted on session-presence alone (rather than
    policy.ledger_record) would break every non-ephemeral invocation by writing
    to in-memory only.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    inst = _make_instance("i-default")

    with EphemeralSession(enabled=False):
        ledger.record(inst)

    on_disk = (tmp_path / "_lifecycle" / "ledger.json").read_text()
    assert "i-default" in on_disk, (
        "non-ephemeral session must still persist to disk; in-memory diversion "
        "fired on session-presence instead of policy.ledger_record"
    )
