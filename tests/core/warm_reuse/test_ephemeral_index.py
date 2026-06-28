"""EphemeralIndex — store-backed discovery index for --ephemeral warm-reuse.

Covers schema, locked CRUD, idempotency, read-tolerance, and the
matcher-compatible to_entry_dict shape.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path)


@pytest.fixture
def row() -> EphemeralIndexRow:
    return EphemeralIndexRow(
        id="pod-A",
        warm_attach_key="a" * 64,
        kinoforge_key="cap12345xyzA",
        endpoints={"8188": "https://pod-A.example.invalid"},
        provider="runpod",
        created_at_local="2026-06-27T14:18:09",
    )


def test_row_is_frozen_dataclass(row: EphemeralIndexRow) -> None:
    """Bug: mutable row shape lets a misbehaving caller alter persisted state."""
    with pytest.raises((AttributeError, Exception)):
        row.id = "pod-B"  # type: ignore[misc]


def test_add_then_rows_returns_added(
    store: LocalArtifactStore, row: EphemeralIndexRow
) -> None:
    """Bug: silent write-skip — add() pretends to persist but disk file is empty."""
    idx = EphemeralIndex(store=store)
    idx.add(row)
    rows = idx.rows()
    assert len(rows) == 1
    assert rows[0] == row


def test_add_is_idempotent_replaces_on_id_collision(
    store: LocalArtifactStore, row: EphemeralIndexRow
) -> None:
    """Bug: matcher sees two candidates for the same pod, attaches twice."""
    idx = EphemeralIndex(store=store)
    idx.add(row)
    replacement = EphemeralIndexRow(
        id="pod-A",
        warm_attach_key="b" * 64,
        kinoforge_key="cap12345xyzB",
        endpoints={"8188": "https://pod-A.example.invalid"},
        provider="runpod",
        created_at_local="2026-06-27T15:00:00",
    )
    idx.add(replacement)
    rows = idx.rows()
    assert len(rows) == 1, "duplicate id must replace, not append"
    assert rows[0].warm_attach_key == "b" * 64


def test_remove_existing_pod(store: LocalArtifactStore, row: EphemeralIndexRow) -> None:
    """Bug: cleanup path silently leaves stale row → matcher attaches to ghost."""
    idx = EphemeralIndex(store=store)
    idx.add(row)
    idx.remove("pod-A")
    assert idx.rows() == []


def test_remove_missing_pod_is_noop(store: LocalArtifactStore) -> None:
    """Bug: double-destroy crashes the cleanup path."""
    idx = EphemeralIndex(store=store)
    idx.remove("pod-nonexistent")  # must not raise
    assert idx.rows() == []


def test_rows_by_wak_filters_correctly(
    store: LocalArtifactStore, row: EphemeralIndexRow
) -> None:
    """Bug: matcher receives wrong-WAK candidate, attempts incompatible attach."""
    idx = EphemeralIndex(store=store)
    idx.add(row)
    idx.add(
        EphemeralIndexRow(
            id="pod-B",
            warm_attach_key="b" * 64,
            kinoforge_key="cap-different",
            endpoints={"8188": "https://pod-B.example.invalid"},
            provider="runpod",
            created_at_local="2026-06-27T14:30:00",
        )
    )
    matches = idx.rows_by_wak("a" * 64)
    assert len(matches) == 1
    assert matches[0].id == "pod-A"


def test_rows_by_wak_empty_when_no_match(
    store: LocalArtifactStore, row: EphemeralIndexRow
) -> None:
    """Bug: returning None vs [] forces every consumer to defensively coerce."""
    idx = EphemeralIndex(store=store)
    idx.add(row)
    assert idx.rows_by_wak("z" * 64) == []


def test_rows_by_kinoforge_key_filters_correctly(
    store: LocalArtifactStore, row: EphemeralIndexRow
) -> None:
    """Bug: _scan_warm_candidates can't find ephemeral pods by cap_key."""
    idx = EphemeralIndex(store=store)
    idx.add(row)
    matches = idx.rows_by_kinoforge_key("cap12345xyzA")
    assert len(matches) == 1
    assert matches[0].id == "pod-A"


def test_read_tolerates_missing_file(store: LocalArtifactStore) -> None:
    """Bug: first-run crashes when index file doesn't exist yet."""
    idx = EphemeralIndex(store=store)
    assert idx.rows() == []


def test_read_tolerates_malformed_json(
    store: LocalArtifactStore, tmp_path: Path
) -> None:
    """Bug: corrupted index halts all warm-reuse scans."""
    (tmp_path / "_lifecycle").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_lifecycle" / "ephemeral-index.json").write_text("{not json")
    idx = EphemeralIndex(store=store)
    assert idx.rows() == []


def test_to_entry_dict_shape_matches_ledger_consumers(
    row: EphemeralIndexRow,
) -> None:
    """Bug: matcher KeyError on missing 'tags' or 'warm_attach_key'."""
    d = row.to_entry_dict()
    assert d["id"] == "pod-A"
    assert d["provider"] == "runpod"
    assert d["endpoints"] == {"8188": "https://pod-A.example.invalid"}
    assert d["warm_attach_key"] == "a" * 64
    assert d["tags"]["kinoforge_key"] == "cap12345xyzA"


def test_concurrent_adds_under_lock_no_torn_write(
    store: LocalArtifactStore,
) -> None:
    """Bug: lost-update from RMW race; one row vanishes under contention."""
    idx = EphemeralIndex(store=store)

    def add_one(suffix: str) -> None:
        idx.add(
            EphemeralIndexRow(
                id=f"pod-{suffix}",
                warm_attach_key=suffix * 64,
                kinoforge_key=f"cap{suffix}",
                endpoints={"8188": f"https://pod-{suffix}.example.invalid"},
                provider="runpod",
                created_at_local="2026-06-27T14:18:09",
            )
        )

    threads = [
        threading.Thread(target=add_one, args=(s,)) for s in ("a", "b", "c", "d")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = idx.rows()
    assert len(rows) == 4, f"expected 4 rows after concurrent adds, got {len(rows)}"
