"""Non-ephemeral runs can still see ephemeral pods via the index.

An ephemeral pod is just a pod. Same WAK = same compatibility,
regardless of which process provisioned it.
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.stores.local import LocalArtifactStore


def test_non_ephemeral_reads_index_rows(tmp_path: Path) -> None:
    """Bug: non-ephemeral process cold-boots while reusable ephemeral pod sits idle."""
    store = LocalArtifactStore(tmp_path)

    # Seed via ephemeral path.
    EphemeralIndex(store=store).add(
        EphemeralIndexRow(
            id="pod-ephemeral",
            warm_attach_key="wak-X",
            kinoforge_key="cap-X",
            endpoint_url="https://pod-ephemeral.example.invalid",
            provider="runpod",
            created_at_local="2026-06-27T14:18:09",
        )
    )

    # Non-ephemeral reader (no EphemeralSession context).
    rows = EphemeralIndex(store=store).rows_by_wak("wak-X")
    assert len(rows) == 1
    assert rows[0].id == "pod-ephemeral"
