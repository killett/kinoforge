"""Warm-attach scan surfaces a Modal EphemeralIndexRow (offline).

Bug caught: a RunPod-proxy URL-pattern assumption in the scan/preflight
would make Modal ephemeral rows undiscoverable or rebuild a wrong URL.

Modal endpoint URLs are opaque (``*.modal.run`` — not rebuildable from the
app name via any deterministic pattern; M5 lesson, commit ``1cb4299``). The
scan path MUST replay the persisted ``endpoints`` dict verbatim; it MUST NOT
fall back to ``provider.endpoints(instance)`` for Modal rows.

This test exercises the ``_scan_warm_candidates`` discovery path (the same
path ``_cmd_generate`` calls) with a ``provider="modal"`` index row written
by a prior ``--ephemeral`` CLI session, simulating the cross-process boundary
from ``test_ephemeral_cross_session_warm_reuse``. ``_resolve_warm_instance``
runs FOR REAL — the provider seam is faked at ``registry.get_provider`` with
a sparse ``get_instance`` (``endpoints={}``) and a deliberately WRONG
RunPod-style ``endpoints()`` rebuild, so the ``.modal.run`` URL on the
returned instance can only come from the entry-endpoints replay branch
(``_resolve_warm_instance`` step 8). Disabling that branch turns the endpoint
assertion red (mutation-verified).

The health-preflight seam (``_health_preflight_ok``) is stubbed exactly as in
``tests/core/warm_reuse/test_scan_warm_candidates_ephemeral_index.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from kinoforge.core.interfaces import Instance, Lifecycle
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.stores.local import LocalArtifactStore

# Hardcoded cap-key prefix and warm-attach-key matching the cfg stub below.
# The pairing (cfg.capability_key().derive()[:12] == kinoforge_key) is what
# the scan's coarse filter enforces.
_CAP_KEY_PREFIX = "cafef00dcafe"
_WAK_HEX = "wak-modal-eph-1"
_POD_ID = "eph-cafef00d"
_MODAL_ENDPOINT_URL = "https://acct--kinoforge-eph-cafef00d-fn.modal.run"


class _FakeModalProvider:
    """Modal-shaped provider double for the classify + get_instance legs.

    ``get_instance`` returns a SPARSE Instance (``endpoints={}``) exactly as
    real providers do — list/status APIs strip create-time fields (the same
    Instance-impoverishment surface documented in
    ``tests/cli/test_resolve_warm_instance_endpoints.py``). The only way the
    scan's returned instance can carry the ``.modal.run`` URL is the
    entry-endpoints replay branch in ``_resolve_warm_instance``.

    ``endpoints()`` deliberately rebuilds a WRONG RunPod-proxy-style URL: if
    the replay branch is skipped and the provider rebuild used instead, the
    endpoint assertion goes red with the rebuilt garbage in the message.
    """

    def list_instances(self) -> list[Instance]:
        """Report the ephemeral pod live.

        classify's liveness gate checks the entry id against the provider's
        list_instances result; an absent id yields STALE_LEDGER, which is
        never force-bypassable and would mask the replay path under test.
        """
        return [self.get_instance(_POD_ID)]

    def get_instance(self, iid: str) -> Instance:
        """Return a sparse Instance — no endpoints, as real providers do."""
        return Instance(
            id=iid,
            provider="modal",
            status="ready",
            created_at=0.0,
            cost_rate_usd_per_hr=0.0,
            endpoints={},  # sparse — real get_instance loses endpoints
            tags={},
        )

    def endpoints(self, instance: Instance) -> dict[str, str]:
        """Rebuild a deliberately WRONG RunPod-pattern URL.

        Modal URLs are opaque; any deterministic rebuild produces garbage.
        This must never reach the returned instance.
        """
        return {"8000": f"https://{instance.id}-8000.proxy.runpod.net"}


def test_modal_row_discovered_and_endpoints_replayed(tmp_path: Any) -> None:
    """Bug: Modal ephemeral row must be surfaced + endpoint URL replayed verbatim.

    A provider-specific URL-pattern assumption (e.g. RunPod's
    ``{pod_id}-{port}.proxy.runpod.net`` rebuild) would silently produce a
    wrong endpoint URL for Modal rows — or make the row undiscoverable if a
    scan/classify gate does not accept ``"modal"`` as a valid provider kind.

    Simulates the cross-process boundary: a prior --ephemeral CLI session
    wrote the Modal row to the disk index; the current process starts with an
    empty ledger and discovers the row via ``_scan_warm_candidates`` with
    ``_resolve_warm_instance`` running for real.
    """
    from kinoforge.cli._commands import _scan_warm_candidates

    store = LocalArtifactStore(tmp_path)

    # Prior --ephemeral process: write a Modal row to the ephemeral index.
    idx = EphemeralIndex(store=store)
    idx.add(
        EphemeralIndexRow(
            id=_POD_ID,
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
    # compute.provider == "modal" so the scan's coarse filter passes. A real
    # Lifecycle so classify's float threshold arithmetic runs unmocked.
    cfg = MagicMock()
    cfg.capability_key.return_value.derive.return_value = (
        _CAP_KEY_PREFIX + "deadbeef" * 6  # scan truncates to [:12]
    )
    cfg.compute.provider = "modal"
    cfg.lifecycle.return_value = Lifecycle(heartbeat_interval_s=60.0)

    with (
        # Provider seam: _resolve_warm_instance step 4 constructs the
        # provider via registry.get_provider("modal")(). NOT patching
        # _resolve_warm_instance itself — its classify gate and step-8
        # endpoint replay must run for real.
        patch(
            "kinoforge.core.registry.get_provider",
            return_value=_FakeModalProvider,
        ),
        patch("kinoforge.cli._commands._probe_lock_held", return_value=False),
        # Stage-capability preflight: stub out the network call so we exercise
        # only the index-discovery + endpoint-replay path, not HTTP reachability.
        patch("kinoforge.cli._commands._health_preflight_ok", return_value=True),
    ):
        instance, report = _scan_warm_candidates(ctx, cfg)

    assert instance is not None, (
        "expected _scan_warm_candidates to surface eph-cafef00d via the "
        "ephemeral-index; got None — Modal provider filter, classify gate, or "
        "index-union path is broken (cold-boot regression for Modal ephemeral "
        f"pods); report.skipped={report.skipped!r}"
    )
    assert instance.id == _POD_ID, (
        f"wrong instance id: expected {_POD_ID!r}, got {instance.id!r}"
    )
    assert instance.endpoints.get("8000") == _MODAL_ENDPOINT_URL, (
        f"endpoint not replayed: expected {_MODAL_ENDPOINT_URL!r}, "
        f"got {instance.endpoints.get('8000')!r} — the persisted .modal.run "
        "URL must be replayed verbatim from the index row; a provider "
        "endpoints() rebuild produces garbage for Modal (opaque URLs)"
    )
    assert report.attached == _POD_ID, (
        f"report.attached mismatch: got {report.attached!r}"
    )
