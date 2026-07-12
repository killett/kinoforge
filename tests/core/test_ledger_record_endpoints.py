"""``Ledger.record`` must persist ``instance.endpoints`` in the entry dict.

Regression context (2026-07-11 Modal warm-reuse smoke):
    A second cross-CLI ``kinoforge generate`` discovered + attached to the
    warm Modal pod (``warm-reuse: attached to <id>``) but then died with::

        kinoforge.core.errors.ProvisionFailed:
            pod '<id>' has no endpoints — cannot construct ready URL

    Root cause: ``Ledger.record`` serialized only ``id / provider / tags /
    created_at / cost_rate_usd_per_hr`` and DROPPED ``instance.endpoints``.
    The warm-attach reconstructor ``_resolve_warm_instance`` replays
    ``entry.get("endpoints")`` and only falls back to
    ``provider.endpoints(instance)`` (a ports-based deterministic rebuild)
    when the entry carries none. RunPod / SkyPilot can rebuild from
    ``tags["ports"]``; Modal CANNOT (its ``.modal.run`` URL carries a
    non-deterministic ``build-<hash>`` suffix), so with no persisted
    endpoints the replay was empty AND the rebuild was empty →
    ProvisionFailed.

Fix: persist ``dict(instance.endpoints)`` in the record() entry dict so the
existing ``entry.get("endpoints")`` replay path picks it up for any
provider, rebuildable or not.

Would-fail-bug (pre-fix): the ``endpoints`` key is absent from the entry
dict → ``entry["endpoints"]`` KeyError / ``entries()[0]["endpoints"]``
missing.
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.local import LocalArtifactStore

_MODAL_ID = "modal-warm-reuse-endpoints"
_MODAL_URL = "https://x--kinoforge-run-build-27e651.modal.run"
_ENDPOINTS = {"8000": _MODAL_URL}


def _modal_instance() -> Instance:
    return Instance(
        id=_MODAL_ID,
        provider="modal",
        status="ready",
        created_at=1_700_000_000.0,
        endpoints=dict(_ENDPOINTS),
        tags={"mode": "pod"},
        cost_rate_usd_per_hr=1.10,
    )


def test_record_persists_endpoints_in_entries(tmp_path: Path) -> None:
    """``entries()[0]["endpoints"]`` must round-trip the recorded endpoints.

    Would-fail-bug: pre-fix ``record()`` omits the ``endpoints`` key, so the
    entry dict has no ``endpoints`` — a Modal warm-attach then replays an
    empty dict and ProvisionFails.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path))
    ledger.record(_modal_instance())

    entries = ledger.entries()
    assert len(entries) == 1
    assert entries[0]["endpoints"] == _ENDPOINTS, (
        f"record() must persist instance.endpoints; got "
        f"{entries[0].get('endpoints')!r}, expected {_ENDPOINTS!r}"
    )


def test_record_persists_endpoints_in_read(tmp_path: Path) -> None:
    """``read(<id>)["endpoints"]`` must mirror the recorded endpoints.

    Warm-attach reads the entry via ``ledger.read`` (per-id), not just
    ``entries()`` — guard both surfaces.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path))
    ledger.record(_modal_instance())

    entry = ledger.read(_MODAL_ID)
    assert entry is not None
    assert entry["endpoints"] == _ENDPOINTS, (
        f"read() entry must carry instance.endpoints; got "
        f"{entry.get('endpoints')!r}, expected {_ENDPOINTS!r}"
    )


def test_record_copies_endpoints_not_aliases(tmp_path: Path) -> None:
    """Persisted endpoints must be a copy, not an alias of the live dict.

    ``record()`` uses ``dict(instance.tags)`` for tags to defend against
    post-record mutation of the source Instance leaking into the ledger; the
    endpoints copy must give the same guarantee.

    Would-fail-bug: storing ``instance.endpoints`` by reference means a later
    mutation of the source dict silently rewrites the persisted ledger entry.
    """
    inst = _modal_instance()
    ledger = Ledger(store=LocalArtifactStore(tmp_path))
    ledger.record(inst)

    inst.endpoints["8000"] = "https://tampered.modal.run"

    entry = ledger.read(_MODAL_ID)
    assert entry is not None
    assert entry["endpoints"] == _ENDPOINTS, (
        "record() must snapshot endpoints; post-record mutation of the source "
        "Instance leaked into the persisted entry"
    )
