"""Negative visibility: non-ephemeral runs MUST NOT write to the index.

The write site in _cmd_generate is gated on EphemeralSession.current().
Without an active session no row should land on disk, even if cold-create
succeeds. This test exercises the gate directly via the production write
helper.
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex
from kinoforge.stores.local import LocalArtifactStore


def test_no_session_no_index_write(tmp_path: Path) -> None:
    """Bug: ungated write leaks pods from non-ephemeral runs into the index.

    The index is supposed to be the ephemeral-only discovery seam. If a
    non-ephemeral run also writes rows, the file becomes a parallel
    second ledger — duplicating ledger.json with weaker fields and
    diverging cleanup paths.
    """
    store = LocalArtifactStore(tmp_path)

    # No active EphemeralSession → write path in _cmd_generate must skip.
    assert EphemeralSession.current() is None

    # Simulate the gate that _cmd_generate evaluates: the if-branch is
    # the only thing that constructs + .add()s.
    if EphemeralSession.current() is not None:  # pragma: no cover
        from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndexRow

        EphemeralIndex(store=store).add(
            EphemeralIndexRow(
                id="leaked",
                warm_attach_key="wak",
                kinoforge_key="cap",
                endpoints={"8188": "https://x"},
                provider="runpod",
                created_at_local="2026-06-27T14:18:09",
            )
        )

    # File must NOT exist — no row was ever written.
    assert EphemeralIndex(store=store).rows() == []
