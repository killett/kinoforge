"""Shared ledger reconciliation — forget rows the provider confirms gone.

Used by both ``kinoforge list`` (`_cmd_list`) and the top-of-command
instance overview (`_print_instance_overview`). One implementation, two
callers — a dead pod's ``est_spend`` (age×rate) must not inflate forever.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _ForgetLedger(Protocol):
    """Minimal ledger surface the reconciler needs: just ``forget``."""

    def forget(self, instance_id: str) -> None:  # noqa: D102
        ...


# Providers whose ``get_instance(id)`` is authoritative ACROSS processes — a
# KeyError reliably means "this pod no longer exists". Only these are auto-
# reconciled. ``local`` is excluded: its instance table is in-process, so a
# fresh CLI invocation always KeyErrors on a valid pod.
_RECONCILABLE_PROVIDERS: frozenset[str] = frozenset({"runpod"})


def _reconcile_dead_ledger_entries(
    ledger: _ForgetLedger,
    entries: list[dict[str, Any]],
    *,
    get_provider: Callable[[str], Callable[[], Any]] | None = None,
) -> list[str]:
    """Forget ledger entries whose pod the provider confirms is gone.

    For each entry, resolve its provider and call ``get_instance(id)``. A
    ``KeyError`` means the pod definitively does not exist provider-side, so the
    stale ledger entry is forgotten — otherwise its ``est_spend`` (age×rate) goes
    on inflating forever (2026-07-06: two 7-day-old dead pods showed ~$210 each).
    ANY other outcome (unknown provider, auth/transport error, live pod) is
    treated as uncertain and the entry is left untouched. Best-effort: never
    raises, so it can run inline on ``kinoforge list`` and the overview without a
    creds/network dependency becoming fatal.

    Args:
        ledger: Object exposing ``forget(instance_id)``.
        entries: Ledger entry dicts (each may carry ``id`` + ``provider``).
        get_provider: Injectable provider-factory resolver (test seam); defaults
            to :func:`kinoforge.core.registry.get_provider`.

    Returns:
        The ids that were confirmed gone and forgotten.
    """
    from kinoforge.core import registry

    resolve = get_provider if get_provider is not None else registry.get_provider
    forgotten: list[str] = []
    for entry in entries:
        pid = str(entry.get("id") or "")
        pname = str(entry.get("provider") or "")
        if not pid or pname not in _RECONCILABLE_PROVIDERS:
            continue
        try:
            provider = resolve(pname)()
        except Exception as exc:  # noqa: BLE001 — unknown/unresolvable provider
            logger.debug("reconcile: skip %s (provider %s: %s)", pid, pname, exc)
            continue
        try:
            provider.get_instance(pid)
        except KeyError:
            try:
                ledger.forget(pid)
                forgotten.append(pid)
            except Exception as exc:  # noqa: BLE001 — forget best-effort
                logger.debug("reconcile: forget %s failed: %s", pid, exc)
                continue
        except Exception as exc:  # noqa: BLE001 — auth/transport → uncertain, keep
            logger.debug("reconcile: probe %s uncertain, keeping: %s", pid, exc)
            continue
    return forgotten
