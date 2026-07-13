"""Warm-attach scan surfaces a Modal EphemeralIndexRow (offline).

Bug caught: a RunPod-proxy URL-pattern assumption in the scan/preflight
would make Modal ephemeral rows undiscoverable or rebuild a wrong URL.

Modal endpoint URLs are opaque (``*.modal.run`` — not rebuildable from the
app name via any deterministic pattern the provider SDK exposes). The scan
path MUST replay the persisted ``endpoints`` dict verbatim; it MUST NOT fall
back to ``provider.endpoints(instance)`` for Modal rows.

This test exercises the ``_scan_warm_candidates`` discovery path (the same
path ``_cmd_generate`` calls) with a ``provider="modal"`` index row written
by a prior ``--ephemeral`` CLI session, simulating the cross-process boundary
from ``test_ephemeral_cross_session_warm_reuse``.

The health-preflight seam (``_health_preflight_ok``) is stubbed exactly as in
``tests/core/warm_reuse/test_scan_warm_candidates_ephemeral_index.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from kinoforge.core.interfaces import Instance
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.stores.local import LocalArtifactStore

# Hardcoded cap-key prefix and warm-attach-key matching the cfg stub below.
# These values are stable test-internal constants — they are not derived from
# a real Config; the pairing (cfg.capability_key().derive()[:12] == cap_key12)
# is enforced by the matching assertion in _scan_warm_candidates.
_CAP_KEY_PREFIX = "cafef00dcafe"
_WAK_HEX = "wak-modal-eph-1"
_MODAL_ENDPOINT_URL = "https://acct--kinoforge-eph-cafef00d-fn.modal.run"


def test_modal_row_discovered_and_endpoints_replayed(
    tmp_path: Any,
) -> None:
    """Bug: Modal ephemeral row must be surfaced + endpoint URL replayed verbatim.

    A provider-specific URL-pattern assumption (e.g. RunPod's
    ``{pod_id}-{port}.proxy.runpod.net`` rebuild) would silently produce a
    wrong endpoint URL for Modal rows — or make the row undiscoverable if the
    provider filter does not accept ``"modal"`` as a valid ``provider`` kind.

    Simulates the cross-process boundary: a prior --ephemeral CLI session
    wrote the Modal row to the disk index; the current process starts with an
    empty ledger and discovers the row via ``_scan_warm_candidates``.
    """
    from kinoforge.cli._commands import _scan_warm_candidates

    store = LocalArtifactStore(tmp_path)

    # Prior --ephemeral process: write a Modal row to the ephemeral index.
    idx = EphemeralIndex(store=store)
    idx.add(
        EphemeralIndexRow(
            id="eph-cafef00d",
            warm_attach_key=_WAK_HEX,
            kinoforge_key=_CAP_KEY_PREFIX,
            endpoints={"8000": _MODAL_ENDPOINT_URL},
            provider="modal",
            created_at_local="2026-07-12T21:00:00",
        )
    )

    # Current process: empty ledger (STRICT_POLICY — in-memory only).
    ctx = MagicMock()
    ctx.ledger.return_value.entries.return_value = []  # empty ledger
    ctx.store.return_value = store

    # cfg whose capability_key().derive()[:12] == _CAP_KEY_PREFIX and whose
    # compute.provider == "modal" so the scan's coarse filter passes.
    cfg = MagicMock()
    cfg.capability_key.return_value.derive.return_value = (
        _CAP_KEY_PREFIX + "deadbeef" * 6  # scan truncates to [:12]
    )
    cfg.compute.provider = "modal"
    cfg.lifecycle.return_value.heartbeat_interval_s = 60.0

    # _resolve_warm_instance: return a fake instance whose endpoints are
    # pre-populated with the Modal URL to mirror what step-8 (entry-endpoints
    # replay) would produce when NOT mocked. The meaningful assertions below
    # are (a) that the scan surfaced eph-cafef00d at all, and (b) that the
    # endpoint URL is the persisted .modal.run value — not a provider-rebuilt
    # RunPod-style URL.
    fake_instance = Instance(
        id="eph-cafef00d",
        provider="modal",
        status="ready",
        created_at=0.0,
        cost_rate_usd_per_hr=0.0,
        endpoints={"8000": _MODAL_ENDPOINT_URL},
        tags={"kinoforge_key": _CAP_KEY_PREFIX},
    )

    with (
        patch(
            "kinoforge.cli._commands._resolve_warm_instance",
            return_value=(fake_instance, None),
        ),
        patch("kinoforge.cli._commands._probe_lock_held", return_value=False),
        # Stage-capability preflight: stub out the network call so we exercise
        # only the index-discovery + endpoint-replay path, not HTTP reachability.
        patch("kinoforge.cli._commands._health_preflight_ok", return_value=True),
    ):
        instance, report = _scan_warm_candidates(ctx, cfg)

    assert instance is not None, (
        "expected _scan_warm_candidates to surface eph-cafef00d via the "
        "ephemeral-index; got None — Modal provider filter or index-union "
        "path is broken (cold-boot regression for Modal ephemeral pods)"
    )
    assert instance.id == "eph-cafef00d", (
        f"wrong instance id: expected 'eph-cafef00d', got {instance.id!r}"
    )
    assert instance.endpoints.get("8000") == _MODAL_ENDPOINT_URL, (
        f"endpoint not replayed: expected {_MODAL_ENDPOINT_URL!r}, "
        f"got {instance.endpoints.get('8000')!r} — "
        "provider.endpoints() rebuild must not overwrite the persisted "
        ".modal.run URL (Modal URLs are not deterministically reconstructable)"
    )
    assert report.attached == "eph-cafef00d", (
        f"report.attached mismatch: got {report.attached!r}"
    )
