"""_scan_warm_candidates ∪ EphemeralIndex — production warm-reuse path.

This is the path _cmd_generate uses (not find_warm_attach_candidate).
Verifies that when the ledger is empty (e.g. fresh ephemeral process),
index rows with a matching kinoforge_key get surfaced as candidates.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path)


def test_scan_surfaces_index_row_when_ledger_empty(
    store: LocalArtifactStore, tmp_path: Path
) -> None:
    """Bug: --ephemeral process #2 cold-boots despite live pod from #1.

    Process #1's pod was written only to in-memory ledger (STRICT policy).
    Process #2 starts fresh; ledger.entries() returns []. Without index
    integration the scan returns (None, _).
    """
    from kinoforge.cli._commands import _scan_warm_candidates

    idx = EphemeralIndex(store=store)
    idx.add(
        EphemeralIndexRow(
            id="pod-from-ephemeral-1",
            warm_attach_key="wak-X",
            kinoforge_key="cap123456789",
            endpoints={"8188": "https://pod.example.invalid"},
            provider="runpod",
            created_at_local="2026-06-27T14:18:09",
        )
    )

    ctx = MagicMock()
    ctx.ledger.return_value.entries.return_value = []  # empty ledger
    ctx.store.return_value = store

    cfg = MagicMock()
    cfg.capability_key.return_value.derive.return_value = (
        "cap123456789" + "deadbeef" * 6  # scan truncates to [:12]
    )
    cfg.compute.provider = "runpod"
    cfg.lifecycle.return_value.heartbeat_interval_s = 60.0

    fake_instance = MagicMock(id="pod-from-ephemeral-1")
    with (
        patch(
            "kinoforge.cli._commands._resolve_warm_instance",
            return_value=(fake_instance, None),
        ),
        patch("kinoforge.cli._commands._probe_lock_held", return_value=False),
        # Production scan gained a /health capability preflight; this test
        # covers index-row surfacing, not stage matching — report "covers".
        patch("kinoforge.cli._commands._health_preflight_ok", return_value=True),
    ):
        instance, report = _scan_warm_candidates(ctx, cfg)

    assert instance is not None, (
        "expected scan to find pod-from-ephemeral-1 via the ephemeral-index; "
        "got None (cold-boot regression)"
    )
    assert instance.id == "pod-from-ephemeral-1"
    assert report.attached == "pod-from-ephemeral-1"
