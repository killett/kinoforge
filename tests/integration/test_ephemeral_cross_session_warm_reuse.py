"""Two --ephemeral CLI invocations share a pod via ephemeral-index.

Reproduces the 2026-06-27 bug: process #2 cold-boots despite process
#1's pod surviving. With the discovery channel wired, process #2 finds
the pod and attaches.

Simulates the process boundary by tearing down the EphemeralSession
between invocations — the in-memory ledger dies but the disk index
survives.
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.stores.local import LocalArtifactStore


def test_two_ephemeral_sessions_share_pod(tmp_path: Path) -> None:
    """Bug: today process #2 cold-boots a second pod (no discovery channel)."""
    store = LocalArtifactStore(tmp_path)

    # Process #1: provision under --ephemeral, write index, exit.
    with EphemeralSession(enabled=True):
        idx1 = EphemeralIndex(store=store)
        idx1.add(
            EphemeralIndexRow(
                id="pod-shared",
                warm_attach_key="wak-X",
                kinoforge_key="cap123456789",
                endpoints={"8188": "https://pod-shared.example.invalid"},
                provider="runpod",
                created_at_local="2026-06-27T14:18:09",
            )
        )

    # Session #1's in-memory ledger is gone. Disk index survives.

    # Process #2: starts fresh; reads disk index; finds pod-shared.
    with EphemeralSession(enabled=True):
        idx2 = EphemeralIndex(store=store)
        rows = idx2.rows_by_kinoforge_key("cap123456789")

    assert len(rows) == 1, (
        "expected exactly one discovery row from session #1; got "
        f"{len(rows)} (cold-boot regression — discovery channel broken)"
    )
    assert rows[0].id == "pod-shared"
    assert rows[0].endpoints == {"8188": "https://pod-shared.example.invalid"}
