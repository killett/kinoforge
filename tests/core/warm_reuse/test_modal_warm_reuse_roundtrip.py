"""Offline: a Modal warm-reuse instance round-trips write->discover.

Milestone 5. The index write (cli/_commands.py:625, gated on an active
EphemeralSession) is provider-agnostic; this pins that a provider="modal"
row survives the cross-process boundary carrying its .modal.run endpoint
(which — unlike RunPod's proxy URL — cannot be rebuilt from ports, so the
stored endpoint is the ONLY recovery path), and that the modal provider is
resolvable by name on the attach side.
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.registry import get_provider
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.providers.modal import ModalProvider
from kinoforge.stores.local import LocalArtifactStore

_MODAL_URL = "https://emmykillett--kinoforge-generate-x-build-27e651.modal.run"
_CAP = "cap0mod4l789"


def test_modal_index_row_roundtrips_with_modal_run_endpoint(tmp_path: Path) -> None:
    # Bug caught: a Modal row that loses its .modal.run endpoint across the
    # process boundary -> run 2 cold-boots (URL is not port-rebuildable).
    store = LocalArtifactStore(tmp_path)

    # Process #1: write the Modal discovery row under an EphemeralSession.
    with EphemeralSession(enabled=True):
        EphemeralIndex(store=store).add(
            EphemeralIndexRow(
                id="modal-run-1",
                warm_attach_key="wak-modal",
                kinoforge_key=_CAP,
                endpoints={"8000": _MODAL_URL},
                provider="modal",
                created_at_local="2026-07-12T10:00:00",
            )
        )

    # Process #2: fresh session; disk index survives; discover by cap key.
    with EphemeralSession(enabled=True):
        rows = EphemeralIndex(store=store).rows_by_kinoforge_key(_CAP)

    assert len(rows) == 1, f"expected 1 modal discovery row, got {len(rows)}"
    assert rows[0].id == "modal-run-1"
    assert rows[0].provider == "modal"
    assert rows[0].endpoints == {"8000": _MODAL_URL}


def test_rows_by_kinoforge_key_filters_by_cap(tmp_path: Path) -> None:
    # Bug caught: a broken rows_by_kinoforge_key that ignores its arg and
    # returned every row would still pass the round-trip test (one row only).
    # Two rows under distinct caps expose that: the query must return exactly
    # the matching row, and [] for a cap with no row.
    _CAP_OTHER = "cap0other999"
    _OTHER_URL = "https://emmykillett--kinoforge-generate-y-build-abc123.modal.run"
    store = LocalArtifactStore(tmp_path)

    with EphemeralSession(enabled=True):
        index = EphemeralIndex(store=store)
        index.add(
            EphemeralIndexRow(
                id="modal-run-1",
                warm_attach_key="wak-modal",
                kinoforge_key=_CAP,
                endpoints={"8000": _MODAL_URL},
                provider="modal",
                created_at_local="2026-07-12T10:00:00",
            )
        )
        index.add(
            EphemeralIndexRow(
                id="modal-run-2",
                warm_attach_key="wak-modal-2",
                kinoforge_key=_CAP_OTHER,
                endpoints={"8000": _OTHER_URL},
                provider="modal",
                created_at_local="2026-07-12T10:05:00",
            )
        )

    with EphemeralSession(enabled=True):
        query = EphemeralIndex(store=store)
        matched = query.rows_by_kinoforge_key(_CAP)
        no_match = query.rows_by_kinoforge_key("capnomatch00")

    assert len(matched) == 1, f"expected exactly the {_CAP} row, got {len(matched)}"
    assert matched[0].id == "modal-run-1"
    assert matched[0].kinoforge_key == _CAP
    assert matched[0].endpoints == {"8000": _MODAL_URL}
    assert no_match == [], f"expected [] for a cap with no row, got {no_match}"


def test_modal_provider_resolves_by_name() -> None:
    # Bug caught: the warm-attach path calls registry.get_provider(cfg
    # .compute.provider)(); if "modal" is not registered there it raises and
    # the attach dies. Importing kinoforge.providers.modal self-registers it.
    # get_provider returns the zero-arg factory (real call sites all invoke
    # it, e.g. _resolve_warm_instance: `registry.get_provider(...)()`), so we
    # call it here exactly as the warm-attach path does.
    provider = get_provider("modal")()
    assert isinstance(provider, ModalProvider)
