"""Shared ledger reconciliation — forget rows the provider confirms gone.

Used by both ``kinoforge list`` (`_cmd_list`) and the top-of-command
instance overview (`_print_instance_overview`). One implementation, two
callers — a dead pod's ``est_spend`` (age×rate) must not inflate forever.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# compute-seam S5. The tag the orchestrator writes on the pre-launch
# provisional row, duplicated as a literal rather than imported from
# ``kinoforge.core.lifecycle``: this module is imported on every CLI command,
# and the value is already a wire-format constant (it is persisted in the
# ledger JSON, so it can never be renamed without a migration anyway).
_LAUNCH_PHASE_TAG: str = "kf_launch_phase"
_LAUNCH_PHASE_LAUNCHING: str = "launching"

# How long a launch may plausibly still be in flight before a row with no
# matching instance is treated as debris. Deliberately generous — double the
# 900 s ``Lifecycle.boot_timeout_s`` default — because the cost of being wrong
# is asymmetric: reaping too early deletes the handle on a pod that is booting
# and billing, while reaping too late only prolongs a $0.00 ghost row.
_LAUNCHING_GRACE_S: float = 1800.0


class _ReconcileLedger(Protocol):
    """The ledger surface the reconciler REQUIRES: just ``forget``.

    compute-seam S5's adoption path uses three more methods, every one of them
    probed with :func:`getattr` rather than declared here. That is deliberate,
    not laziness: the reconciler is best-effort and is handed forget-only
    ledgers today (``tests/cli/test_reconcile_ledger.py``), so a wider Protocol
    would type-reject callers that the runtime handles perfectly well by
    declining to adopt. Absent any of them, the row is LEFT ALONE — never
    forgotten on a capability the ledger happens not to have.

    * ``record(instance, **kwargs)`` — writes the adopted row. Without it there
      is nothing to adopt onto.
    * ``forget_provisional(provisional_id, *, real_id=None) -> bool`` — the
      phase-scoped delete. Strongly preferred over ``forget`` for adoption; see
      :func:`_adopt_launching_row`.
    * ``read(instance_id) -> dict | None`` — used only to avoid appending a
      second row for an instance the ledger already holds.
    """

    def forget(self, instance_id: str) -> None:  # noqa: D102
        ...


# Providers whose ``get_instance(id)`` is authoritative ACROSS processes — a
# KeyError reliably means "this pod no longer exists". Only these are auto-
# reconciled. ``local`` is excluded: its instance table is in-process, so a
# fresh CLI invocation always KeyErrors on a valid pod.
#
# ``skypilot`` joined 2026-08-15 (F1): SkyPilotProvider.get_instance() calls
# ``sky_client.status()``, which is cross-process authoritative the same way
# RunPod's API is, and KeyErrors reliably for a cluster that never came up
# (e.g. ``sky.launch`` raised ``ResourcesUnavailableError`` after the
# provisional F12 ledger row was already written). Without this, a routine
# launch failure leaves a permanent ghost row whose ``est_spend`` inflates
# forever and that the sweeper later tries (and fails) to destroy — the same
# "$210 phantom pod" failure mode this reconciler was built to close for
# RunPod.
_RECONCILABLE_PROVIDERS: frozenset[str] = frozenset({"runpod", "skypilot"})


def _reconcile_dead_ledger_entries(
    ledger: _ReconcileLedger,
    entries: list[dict[str, Any]],
    *,
    get_provider: Callable[[str], Callable[[], Any]] | None = None,
    now: float | None = None,
    boot_timeout_s: float = _LAUNCHING_GRACE_S,
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

    compute-seam S5 carves out ONE exception to the "KeyError means gone" rule:
    a row tagged ``kf_launch_phase=launching`` never reaches the probe at all.
    That row is written by the orchestrator BEFORE ``create_instance`` and is
    keyed by the CLIENT-side ``run_id``, which equals the provider's id only on
    SkyPilot — on RunPod it is the pod NAME, on Modal the app run id. Probing
    ``get_instance(run_id)`` there asks about an id that cannot exist, so the
    KeyError rule would delete the one durable handle on a pod that may be
    mid-boot and billing: exactly the orphan finding F12 exists to prevent, and
    reachable from a plain concurrent ``kinoforge list``. Such rows go to
    :func:`_adopt_or_age_out` instead.

    Args:
        ledger: Object exposing ``forget(instance_id)`` (see
            :class:`_ReconcileLedger` for the optional adoption methods).
        entries: Ledger entry dicts (each may carry ``id`` + ``provider``).
        get_provider: Injectable provider-factory resolver (test seam); defaults
            to :func:`kinoforge.core.registry.get_provider`.
        now: Current epoch seconds (test seam); defaults to ``time.time()``.
            Only the ``launching`` branch reads it.
        boot_timeout_s: How long a ``launching`` row with no matching instance
            is presumed to be a launch still in flight rather than debris.

    Returns:
        The ids that were confirmed gone and forgotten. An ADOPTED row is
        deliberately absent from this list even though its id was removed: the
        callers use the return value to print "pod gone provider-side" and to
        hide the row, and doing either for a pod that is live and billing is
        the failure this function exists to prevent.
    """
    from kinoforge.core import registry

    resolve = get_provider if get_provider is not None else registry.get_provider
    now_s = time.time() if now is None else now
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
        if _is_launching(entry):
            try:
                aged_out = _adopt_or_age_out(
                    ledger,
                    provider,
                    entry,
                    now=now_s,
                    boot_timeout_s=boot_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 — never fatal, never a delete
                logger.debug("reconcile: launching row %s skipped: %s", pid, exc)
                continue
            if aged_out is not None:
                forgotten.append(aged_out)
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


def _is_launching(entry: dict[str, Any]) -> bool:
    """Return whether *entry* is the orchestrator's pre-launch provisional row.

    Args:
        entry: A ledger entry dict.

    Returns:
        True when ``tags[_LAUNCH_PHASE_TAG]`` is ``"launching"``.
    """
    tags = entry.get("tags")
    if not isinstance(tags, dict):
        return False
    return bool(tags.get(_LAUNCH_PHASE_TAG) == _LAUNCH_PHASE_LAUNCHING)


def _adopt_or_age_out(
    ledger: _ReconcileLedger,
    provider: Any,  # noqa: ANN401 — duck-typed provider; core must stay import-free
    entry: dict[str, Any],
    *,
    now: float,
    boot_timeout_s: float,
) -> str | None:
    """Resolve a pre-launch provisional row by NAME, or age it out.

    compute-seam S5. The row is keyed by the client-side ``run_id``, which is
    the provider's id only on SkyPilot; on RunPod it is the pod name and on
    Modal the app run id. ``get_instance(run_id)`` therefore KeyErrors for a pod
    that exists, and the caller's "KeyError means gone" rule would delete the
    only durable handle on a billing resource — the precise failure F12 exists
    to prevent.

    So the row is resolved against the FULL listing instead, matching on either
    the instance id or ``tags["name"]``. Three outcomes:

    * a match → adopt (see :func:`_adopt_launching_row`);
    * no match, row younger than *boot_timeout_s* → LEAVE IT. A create is
      multi-minute on every cloud, and a concurrent ``kinoforge list`` (which
      this project's own live-smoke polling rule actively encourages) must not
      reconcile away the row protecting a boot that is still in progress;
    * no match, row older than *boot_timeout_s* → forget it, or the launch that
      never produced anything haunts ``list`` forever.

    Anything uncertain — an unreadable provider, a malformed ``created_at``, a
    failing ``forget`` — leaves the row exactly where it is.

    Args:
        ledger: Ledger exposing ``forget``, and ideally the adoption methods
            described on :class:`_ReconcileLedger`.
        provider: The resolved provider.
        entry: The launching row.
        now: Current epoch seconds.
        boot_timeout_s: How long a launch may plausibly still be in flight.

    Returns:
        The row id when it was aged out, else None. Adoption returns None: the
        row's id changed, but nothing about that pod is "gone".
    """
    row_id = str(entry.get("id") or "")
    tags = entry.get("tags")
    run_id = (
        str((tags or {}).get("kf_run_id") or row_id)
        if isinstance(tags, dict)
        else row_id
    )
    try:
        live = provider.list_instances()
    except Exception as exc:  # noqa: BLE001 — uncertain → keep the row
        logger.debug("reconcile: launching row %s uncertain: %s", row_id, exc)
        return None
    for inst in live:
        if inst.id == run_id or dict(inst.tags).get("name") == run_id:
            _adopt_launching_row(ledger, row_id=row_id, instance=inst)
            return None
    try:
        age = now - float(entry.get("created_at") or now)
    except (TypeError, ValueError) as exc:
        # A row whose age cannot be read is not evidence of anything.
        logger.debug("reconcile: launching row %s has no usable age: %s", row_id, exc)
        return None
    if age <= boot_timeout_s:
        return None
    try:
        ledger.forget(row_id)
    except Exception as exc:  # noqa: BLE001 — forget best-effort
        logger.debug("reconcile: forget launching row %s failed: %s", row_id, exc)
        return None
    logger.info(
        "reconcile: forgot launching row %s (%.0fs old, no matching instance)",
        row_id,
        age,
    )
    return row_id


def _adopt_launching_row(
    ledger: _ReconcileLedger,
    *,
    row_id: str,
    instance: Any,  # noqa: ANN401 — kinoforge.core.interfaces.Instance, duck-typed
) -> None:
    """Rewrite a provisional row onto the id the provider actually assigned.

    Two orderings are possible and only one is safe. ``record`` lands FIRST, so
    there is never a window in which zero rows exist for a resource that is
    already created and billing; the provisional row is dropped only after its
    replacement is durable.

    The delete is ``forget_provisional(row_id, real_id=instance.id)`` whenever
    the ledger offers it, NOT ``forget``. ``forget`` matches on id alone, and on
    SkyPilot the cluster name IS the ``run_id`` — so ``forget(row_id)`` after
    the record would delete the row just written, leaving a live cluster
    invisible to every kinoforge command. ``forget_provisional`` is scoped by id
    AND by ``kf_launch_phase == "launching"``, and additionally refuses unless a
    non-provisional row already exists under *real_id*, which is exactly the
    precondition this call has just established. Its precondition and its delete
    share one lock acquisition, so a concurrent writer cannot slip between them.

    Ledgers without ``forget_provisional`` fall back to ``forget`` ONLY when the
    ids differ, where a plain delete cannot take the new row with it. On the
    same-key shape such a ledger simply keeps its ``launching`` row: a stale tag
    is a cosmetic defect, and deleting a live resource's only handle is not.

    Args:
        ledger: The ledger to rewrite.
        row_id: The provisional row's id (the client-side ``run_id``).
        instance: The live instance the row resolved to.
    """
    record = getattr(ledger, "record", None)
    if not callable(record):
        logger.debug(
            "reconcile: ledger cannot record; leaving launching row %s alone", row_id
        )
        return
    collapse = getattr(ledger, "forget_provisional", None)
    if not callable(collapse) and instance.id == row_id:
        # Decided BEFORE the record, not after: recording first and only then
        # discovering the delete is unsafe would leave two rows under one id.
        logger.warning(
            "reconcile: ledger has no phase-scoped delete, so launching row %s "
            "is kept — forgetting it by id would delete the real row that "
            "shares that id",
            row_id,
        )
        return
    try:
        if not _has_real_row(ledger, instance.id):
            record(instance)
        if callable(collapse):
            collapse(row_id, real_id=instance.id)
        else:
            ledger.forget(row_id)
    except Exception as exc:  # noqa: BLE001 — adoption is best-effort
        logger.debug("reconcile: adopting launching row %s failed: %s", row_id, exc)
        return
    logger.info(
        "reconcile: adopted launching row %s onto live instance %s",
        row_id,
        instance.id,
    )


def _has_real_row(ledger: _ReconcileLedger, instance_id: str) -> bool:
    """Return whether the ledger already holds a NON-provisional row for an id.

    Guards the one thing ``record`` cannot undo: it APPENDS, so recording an
    instance the ledger already knows leaves two rows under one id and every
    per-id reader picks whichever it finds first. Only reachable when the
    orchestrator's own collapse failed (it is best-effort too), which is
    precisely when the reconciler is expected to clean up.

    Args:
        ledger: The ledger to interrogate; ``read`` is optional.
        instance_id: The provider-assigned id.

    Returns:
        True only when a row exists under *instance_id* and is not itself a
        ``launching`` row. False on any doubt, including an unreadable ledger —
        a spurious duplicate row is recoverable, a missing row is not.
    """
    read = getattr(ledger, "read", None)
    if not callable(read):
        return False
    try:
        row = read(instance_id)
    except Exception as exc:  # noqa: BLE001 — unreadable ledger → assume absent
        logger.debug("reconcile: ledger read of %s failed: %s", instance_id, exc)
        return False
    if not isinstance(row, dict):
        return False
    return not _is_launching(row)
