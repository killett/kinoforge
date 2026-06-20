"""Layer U T1: Ledger.touch strict-update mutation under cross-process lock.

Tests are red-first against the missing `Ledger.touch` API. Implementation
sketch lives in docs/superpowers/specs/2026-06-05-layer-u-heartbeat-persistence-design.md §3.2.

AC mapping (Layer U):
- AC1 / AC4 covered by `test_touch_sets_last_heartbeat_*` and `test_touch_acquires_*`.
- AC2 covered by `test_touch_unknown_id_*`.
- AC3 covered by `test_touch_unchanged_value_*`.
- AC4 covered by `test_touch_filters_protected_keys`.
- AC5 covered by `test_touch_visible_across_process_boundary`.
- Forget-race lockdown by `test_touch_after_forget_returns_false_no_resurrect`.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from kinoforge.core.clock import FakeClock
from kinoforge.core.interfaces import Artifact, Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.locks import InMemoryLock
from kinoforge.stores.local import LocalArtifactStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instance(
    instance_id: str = "i-touch",
    *,
    provider: str = "local",
    created_at: float = 0.0,
    cost_rate_usd_per_hr: float = 0.0,
    tags: dict[str, str] | None = None,
) -> Instance:
    """Build a minimal Instance for ledger-touch tests."""
    return Instance(
        id=instance_id,
        provider=provider,
        status="ready",
        created_at=created_at,
        cost_rate_usd_per_hr=cost_rate_usd_per_hr,
        tags=dict(tags) if tags is not None else {},
    )


class _SpyStore(LocalArtifactStore):
    """LocalArtifactStore that records acquire_lock calls and _write_entries hits."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.lock_calls: list[tuple[str, float]] = []
        self.write_count: int = 0
        self._registry: dict[str, dict[str, float | str]] = {}
        self._clock = FakeClock(start=0.0)

    def acquire_lock(self, key: str, *, ttl_s: float) -> InMemoryLock:  # noqa: D102
        self.lock_calls.append((key, ttl_s))
        return InMemoryLock(
            key=key, ttl_s=ttl_s, registry=self._registry, clock=self._clock
        )

    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact:  # type: ignore[type-arg]
        """Tally writes for the skip-unchanged test, then delegate."""
        if name == "ledger.json":
            self.write_count += 1
        return super().put_json(run_id, name, obj)


# ---------------------------------------------------------------------------
# AC1 / AC4 — happy path + lock contract
# ---------------------------------------------------------------------------


def test_touch_sets_last_heartbeat_on_existing_entry_returns_true(
    tmp_path: Path,
) -> None:
    """touch on existing id writes last_heartbeat and returns True.

    Would fail if touch were implemented as upsert with wrong order, or if
    it skipped persisting when the prior value was absent.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_touch_happy")
    inst = _make_instance("i-1", created_at=1.0)
    ledger.record(inst)

    changed = ledger.touch("i-1", last_heartbeat=1234.5)

    assert changed is True
    fresh = Ledger(store=store, run_id="_touch_happy")
    [entry] = fresh.entries()
    assert entry["id"] == "i-1"
    assert entry["last_heartbeat"] == 1234.5


def test_touch_acquires_ledger_lock_with_expected_key_and_ttl(
    tmp_path: Path,
) -> None:
    """touch uses the same lock key + ttl as record/forget.

    Locks down the cross-process safety contract: a future implementation
    that forgets to wrap the RMW would silently regress Layer H.
    """
    store = _SpyStore(tmp_path)
    ledger = Ledger(store=store, run_id="_touch_lock", mutate_ttl_s=17.0)
    ledger.record(_make_instance("i-1"))
    pre_count = len(store.lock_calls)

    ledger.touch("i-1", last_heartbeat=42.0)

    assert len(store.lock_calls) == pre_count + 1
    key, ttl = store.lock_calls[-1]
    assert key == "ledger/_touch_lock"
    assert ttl == 17.0


# ---------------------------------------------------------------------------
# AC2 — unknown id no-op
# ---------------------------------------------------------------------------


def test_touch_unknown_id_returns_false_and_does_not_append(tmp_path: Path) -> None:
    """touch on an unknown id is a no-op (no upsert, no resurrect).

    Would fail an upsert misimplementation that appends a synthetic entry.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_touch_unknown")
    ledger.record(_make_instance("i-1"))

    changed = ledger.touch("nonexistent", last_heartbeat=42.0)

    assert changed is False
    [entry] = ledger.entries()
    assert entry["id"] == "i-1"
    assert "last_heartbeat" not in entry


# ---------------------------------------------------------------------------
# AC3 — skip-unchanged guard
# ---------------------------------------------------------------------------


def test_touch_unchanged_value_is_noop_writes_zero_times(tmp_path: Path) -> None:
    """Second touch with the same value writes nothing.

    Pre-mitigates lock thrash for a future sub-second-cadence consumer.
    Failing this would surface as ledger I/O at every poll even when
    nothing has changed — the precise pain we are designing around.
    """
    store = _SpyStore(tmp_path)
    ledger = Ledger(store=store, run_id="_touch_noop")
    ledger.record(_make_instance("i-1"))

    first = ledger.touch("i-1", last_heartbeat=1.0)
    write_count_after_first = store.write_count
    second = ledger.touch("i-1", last_heartbeat=1.0)

    assert first is True
    assert second is False
    assert store.write_count == write_count_after_first


def test_touch_with_all_none_kwargs_is_noop_no_lock_acquired(
    tmp_path: Path,
) -> None:
    """All-None kwargs returns False without acquiring the lock.

    Lets the call site stay unguarded: `ledger.touch(id,
    last_heartbeat=provider.last_heartbeat(id))` is always safe even when
    the provider hasn't observed any heartbeat yet.
    """
    store = _SpyStore(tmp_path)
    ledger = Ledger(store=store, run_id="_touch_allnone")
    ledger.record(_make_instance("i-1"))
    pre_lock_calls = len(store.lock_calls)

    changed = ledger.touch("i-1")

    assert changed is False
    assert len(store.lock_calls) == pre_lock_calls


# ---------------------------------------------------------------------------
# AC4 — protected key filter
# ---------------------------------------------------------------------------


def test_touch_filters_protected_keys(tmp_path: Path) -> None:
    """Protected keys (id/provider/tags/created_at) cannot be overwritten via **extra.

    Defends against accidental clobber by a future Layer V consumer that
    grows touch's payload. Without this guard, a caller could rewrite
    instance.id from inside touch and corrupt the ledger.

    NOTE: cost_rate_usd_per_hr was removed from the protected set so
    ``kinoforge status`` can refresh the ledger rate from the live
    provider value (RunPod's GraphQL ``pod.costPerHr``).  See
    :func:`test_touch_updates_cost_rate_usd_per_hr` for the new contract.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_touch_protected")
    ledger.record(
        _make_instance(
            "i-1", provider="local", created_at=10.0, cost_rate_usd_per_hr=0.25
        )
    )

    # mypy: tags is dict, **extra is float|int|str|None — we pass the
    # protected payload via **kwargs unpack to keep the assertion that
    # touch silently filters even ill-typed values out.
    protected_extra: dict[str, Any] = {
        "provider": "evil",
        "id": "hijacked",
        "created_at": 0.0,
        "tags": {"x": "y"},
    }
    changed = ledger.touch("i-1", last_heartbeat=1.0, **protected_extra)

    assert changed is True
    [entry] = ledger.entries()
    assert entry["id"] == "i-1"
    assert entry["provider"] == "local"
    assert entry["created_at"] == 10.0
    assert entry["cost_rate_usd_per_hr"] == 0.25
    assert entry["tags"] == {}
    assert entry["last_heartbeat"] == 1.0


def test_touch_updates_cost_rate_usd_per_hr(tmp_path: Path) -> None:
    """touch(id, cost_rate_usd_per_hr=X) replaces the recorded rate.

    Catches the bug where the field was frozen in ``_PROTECTED_LEDGER_KEYS``,
    so ``kinoforge status`` could not persist the live RunPod
    ``pod.costPerHr`` (e.g. $0.45/hr) back to the ledger and kept showing
    the catalog rate captured at provision time (e.g. $0.35/hr).  Every
    ``accrued_spend_usd`` figure, the ``cost`` dashboard total, and the
    budget-ceiling guard were biased low by the same factor until the
    refresh path was unlocked.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_touch_cost_rate")
    ledger.record(
        _make_instance(
            "i-rate", provider="runpod", created_at=10.0, cost_rate_usd_per_hr=0.35
        )
    )

    changed = ledger.touch("i-rate", cost_rate_usd_per_hr=0.45)

    assert changed is True
    [entry] = ledger.entries()
    assert entry["cost_rate_usd_per_hr"] == 0.45
    # touch must not corrupt the other record-owned fields when threading
    # the refreshed rate through.
    assert entry["id"] == "i-rate"
    assert entry["provider"] == "runpod"
    assert entry["created_at"] == 10.0


# ---------------------------------------------------------------------------
# AC5 — cross-process visibility
# ---------------------------------------------------------------------------


def test_touch_visible_across_process_boundary(tmp_path: Path) -> None:
    """Subprocess: process A records + touches; parent process reads via entries().

    This is the test that proves the on-disk format actually survives
    the process boundary. A future serialization regression (forgetting
    json.dumps, dropping the field, encoding as bytes) fails here.
    """
    writer = textwrap.dedent(
        f"""
        from pathlib import Path
        from kinoforge.core.interfaces import Instance
        from kinoforge.core.lifecycle import Ledger
        from kinoforge.stores.local import LocalArtifactStore

        store = LocalArtifactStore(Path({str(tmp_path)!r}))
        ledger = Ledger(store=store, run_id="_xproc_touch")
        inst = Instance(
            id="i-x",
            provider="local",
            status="ready",
            created_at=0.0,
            cost_rate_usd_per_hr=0.0,
        )
        ledger.record(inst)
        assert ledger.touch("i-x", last_heartbeat=99.0) is True
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", writer],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"writer subprocess failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert proc.stdout.strip() == "OK"

    store = LocalArtifactStore(tmp_path)
    reader = Ledger(store=store, run_id="_xproc_touch")
    [entry] = reader.entries()
    assert entry["id"] == "i-x"
    assert entry["last_heartbeat"] == 99.0


# ---------------------------------------------------------------------------
# Forget-race lockdown
# ---------------------------------------------------------------------------


def test_touch_after_forget_returns_false_no_resurrect(tmp_path: Path) -> None:
    """forget+touch race: touch on a forgotten id returns False, no resurrect.

    Locks down strict-update semantics. An upsert implementation would
    re-create the entry with only the touched field, corrupting the
    invariant that record() is the sole insert path.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_touch_forget")
    ledger.record(_make_instance("i-gone", created_at=5.0))
    ledger.forget("i-gone")
    assert ledger.entries() == []

    changed = ledger.touch("i-gone", last_heartbeat=1.0)

    assert changed is False
    assert ledger.entries() == []


# ---------------------------------------------------------------------------
# Sentinel forward-compat seam
# ---------------------------------------------------------------------------


def test_touch_writes_sentinel_extra_alongside_last_heartbeat(tmp_path: Path) -> None:
    """The forward-compat **extra path persists heartbeat_thread_tick.

    Layer U writes the sentinel through this seam; T2's HeartbeatLoop is
    its only producer. The CLI status surface (Layer U T5) reads it for
    the staleness advisory.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store, run_id="_touch_sentinel")
    ledger.record(_make_instance("i-1"))

    changed = ledger.touch("i-1", last_heartbeat=1.0, heartbeat_thread_tick=2.0)

    assert changed is True
    [entry] = ledger.entries()
    assert entry["last_heartbeat"] == 1.0
    assert entry["heartbeat_thread_tick"] == 2.0
