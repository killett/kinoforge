"""Shared ledger reconciliation — forget rows the provider confirms gone.

Used by both ``kinoforge list`` (`_cmd_list`) and the top-of-command
instance overview (`_print_instance_overview`). One implementation, two
callers — a dead pod's ``est_spend`` (age×rate) must not inflate forever.
"""

from __future__ import annotations

import dataclasses
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
#
# This is NOT a boot timeout and the parameter it defaults is not named after
# one: at 1800 s it is twice ``Lifecycle.boot_timeout_s``, and a caller reading
# "boot_timeout_s" off the signature would reasonably pass the 900 s lifecycle
# value and halve a grace window that is deliberately generous.
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
    * ``entries() -> list[dict]``, or ``read(instance_id) -> dict | None`` as a
      fallback — used only to avoid appending a second row for an instance the
      ledger already holds. See :func:`_has_real_row` for why the scan is
      preferred over the per-id read.
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
    launching_grace_s: float = _LAUNCHING_GRACE_S,
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
    :func:`_resolve_launching_row` instead.

    A ``launching`` row is dispatched BEFORE the ``_RECONCILABLE_PROVIDERS``
    gate, and that ordering is the fix for the hole ruling C1 would otherwise
    open. Since C1, a create that raises KEEPS its provisional row — a raise is
    not proof the provider booked nothing. On a provider this module cannot
    reconcile (``modal`` is not in the set, and never can be: its listing
    exposes no name matchable against a ``run_id``) that row would then be
    permanent, visible in every ``kinoforge list`` forever, with no branch
    anywhere able to clear it. The age-out fallback in
    :func:`_resolve_launching_row` is what closes that: after the grace window,
    an unadoptable ``launching`` row is forgotten. NOTHING ELSE about a
    non-reconcilable provider changes — a non-launching ``local`` row is still
    never probed and never forgotten.

    Args:
        ledger: Object exposing ``forget(instance_id)`` (see
            :class:`_ReconcileLedger` for the optional adoption methods).
        entries: Ledger entry dicts (each may carry ``id`` + ``provider``).
        get_provider: Injectable provider-factory resolver (test seam); defaults
            to :func:`kinoforge.core.registry.get_provider`.
        now: Current epoch seconds (test seam); defaults to ``time.time()``.
            Only the ``launching`` branch reads it.
        launching_grace_s: How long a ``launching`` row with no matching
            instance is presumed to be a launch still in flight rather than
            debris. NOT a boot timeout — see :data:`_LAUNCHING_GRACE_S`.

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
        if not pid:
            continue
        if _is_launching(entry):
            # Above the reconcilable gate on purpose — see the note in this
            # function's docstring about the permanent-row hole C1 opens.
            try:
                aged_out = _resolve_launching_row(
                    ledger,
                    entry,
                    provider_name=pname,
                    resolve=resolve,
                    now=now_s,
                    launching_grace_s=launching_grace_s,
                )
            except Exception as exc:  # noqa: BLE001 — never fatal, never a delete
                logger.debug("reconcile: launching row %s skipped: %s", pid, exc)
                continue
            if aged_out is not None:
                forgotten.append(aged_out)
            continue
        if pname not in _RECONCILABLE_PROVIDERS:
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


def _resolve_launching_row(
    ledger: _ReconcileLedger,
    entry: dict[str, Any],
    *,
    provider_name: str,
    resolve: Callable[[str], Callable[[], Any]],
    now: float,
    launching_grace_s: float,
) -> str | None:
    """Route a ``launching`` row to adoption, or to the provider-agnostic age-out.

    Two paths, split on whether adoption is even expressible for this provider:

    * **Reconcilable** (:data:`_RECONCILABLE_PROVIDERS`). The provider is
      constructed and :func:`_adopt_or_age_out` gets to match the row against
      the live listing, adopting it when the resource turns out to exist. A
      provider that will not construct (missing creds, a transport fault) is
      left ALONE, not aged out: that is transient uncertainty, and the next
      invocation with working creds can still adopt.
    * **Everything else** — ``modal``, ``local``, an unregistered or absent
      provider name. Adoption cannot be expressed here at all: Modal's listing
      carries no name matchable against a ``run_id``, and ``local``'s instance
      table is in-process so a fresh CLI can never see it. The row is aged out
      once the grace window has passed, and NEVER adopted.

    That second path exists because of ruling C1. With the orchestrator no
    longer forgetting the row on every raise, a Modal launch that dies would
    otherwise leave a row that no branch in this module could ever clear.

    Args:
        ledger: The ledger holding the row.
        entry: The launching row. MUTATED IN PLACE on adoption.
        provider_name: The row's ``provider`` field.
        resolve: Provider-factory resolver.
        now: Current epoch seconds.
        launching_grace_s: How long a launch may plausibly still be in flight.

    Returns:
        The row id when it was aged out, else None.
    """
    if provider_name in _RECONCILABLE_PROVIDERS:
        try:
            provider = resolve(provider_name)()
        except Exception as exc:  # noqa: BLE001 — unresolvable → uncertain, keep
            logger.debug(
                "reconcile: launching row %s left alone (provider %s: %s)",
                entry.get("id"),
                provider_name,
                exc,
            )
            return None
        return _adopt_or_age_out(
            ledger,
            provider,
            entry,
            now=now,
            launching_grace_s=launching_grace_s,
        )
    return _age_out_unadoptable_row(
        ledger, entry, provider_name=provider_name, now=now, grace_s=launching_grace_s
    )


def _age_out_unadoptable_row(
    ledger: _ReconcileLedger,
    entry: dict[str, Any],
    *,
    provider_name: str,
    now: float,
    grace_s: float,
) -> str | None:
    """Forget a ``launching`` row this module can never adopt, once it is old.

    The provider-agnostic half of :func:`_resolve_launching_row`. No listing is
    read and no provider is constructed — for these providers there is nothing
    useful to ask — so the ONLY gate is age, and it is the same generous window
    the adoption path uses.

    Deliberately narrow: it fires for ``launching`` rows and nothing else. A
    plain ``local`` row is still never touched, which is what
    ``_RECONCILABLE_PROVIDERS`` has always meant.

    Args:
        ledger: The ledger holding the row.
        entry: The launching row.
        provider_name: The row's ``provider`` field, for the log line.
        now: Current epoch seconds.
        grace_s: How long a launch may plausibly still be in flight.

    Returns:
        The row id when it was forgotten, else None (young, unreadable age, or
        a ledger that refused the delete).
    """
    row_id = str(entry.get("id") or "")
    age = _row_age(entry, now=now, row_id=row_id)
    if age is None or age <= grace_s:
        return None
    try:
        if not _forget_launching_row(ledger, row_id):
            return None
    except Exception as exc:  # noqa: BLE001 — forget best-effort
        logger.debug("reconcile: forget launching row %s failed: %s", row_id, exc)
        return None
    logger.info(
        "reconcile: forgot launching row %s (%.0fs old, provider %s exposes no "
        "adoptable listing)",
        row_id,
        age,
        provider_name,
    )
    return row_id


def _row_age(entry: dict[str, Any], *, now: float, row_id: str) -> float | None:
    """Return a row's age in seconds, or None when it cannot be read.

    Args:
        entry: The ledger row.
        now: Current epoch seconds.
        row_id: The row id, for the log line.

    Returns:
        Age in seconds, or None. A row whose age cannot be read is not
        evidence of anything, so callers must leave it alone.
    """
    try:
        return now - float(entry.get("created_at") or now)
    except (TypeError, ValueError) as exc:
        logger.debug("reconcile: launching row %s has no usable age: %s", row_id, exc)
        return None


def _forget_launching_row(ledger: _ReconcileLedger, row_id: str) -> bool:
    """Delete a ``launching`` row through the PHASE-SCOPED door where possible.

    ``Ledger.forget`` matches on id alone, and on the same-key shape (SkyPilot:
    the cluster name IS the ``run_id``) a real row can share that id — an
    earlier successful launch that reused the run id. Forgetting by id there
    takes the live cluster's only durable handle with the ghost.
    ``forget_provisional(row_id)`` is scoped by id AND by
    ``kf_launch_phase == "launching"``, so it can only ever remove the row this
    function was asked about.

    Falls back to ``forget`` for ledgers that do not offer the scoped delete —
    the same concession :func:`_adopt_launching_row` makes, and for the same
    reason: this module is handed forget-only ledgers, and refusing to age out
    on those would restore the permanent ghost row this whole path exists to
    clear.

    Args:
        ledger: The ledger holding the row.
        row_id: The provisional row's id.

    Returns:
        True when the row was removed. False when a scoped delete declined,
        which must NOT be reported to the caller as "gone".
    """
    collapse = getattr(ledger, "forget_provisional", None)
    if callable(collapse):
        return bool(collapse(row_id))
    ledger.forget(row_id)
    return True


def _adopt_or_age_out(
    ledger: _ReconcileLedger,
    provider: Any,  # noqa: ANN401 — duck-typed provider; core must stay import-free
    entry: dict[str, Any],
    *,
    now: float,
    launching_grace_s: float,
) -> str | None:
    """Resolve a pre-launch provisional row by NAME, or age it out.

    compute-seam S5. The row is keyed by the client-side ``run_id``, which is
    the provider's id only on SkyPilot; on RunPod it is the pod name and on
    Modal the app run id. ``get_instance(run_id)`` therefore KeyErrors for a pod
    that exists, and the caller's "KeyError means gone" rule would delete the
    only durable handle on a billing resource — the precise failure F12 exists
    to prevent.

    **The age gate comes first, and it gates the adoption too.** A row younger
    than *launching_grace_s* is LEFT ALONE unconditionally — matched or not:

    * unmatched, it is a create still in flight, and a concurrent
      ``kinoforge list`` (which this project's own live-smoke polling rule
      actively encourages) must not reconcile away the row protecting that boot;
    * matched, its own orchestrator is still running and will collapse the row
      itself when ``create_instance`` returns. Adopting under it would be
      actively harmful: on SkyPilot ``create_instance`` blocks through all of
      ``sky.launch`` while ``sky.status()`` already lists the cluster in INIT, so
      this function would record a listing-derived row and collapse the
      provisional one; the orchestrator's own ``ledger.record`` then APPENDS a
      SECOND row under that id and its collapse finds nothing to remove. Because
      the reconciler's row landed first and :meth:`Ledger.read` returns the first
      match, warm-attach resolution and ``est_spend`` would then read a mid-INIT
      row with empty ``endpoints``, no ``warm_attach_key`` and no lifecycle
      snapshot, and the overview would double-count the spend.

    Only past the gate is the row resolved against the FULL listing, matching on
    the instance id or ``tags["name"]``: a match is adopted (see
    :func:`_adopt_launching_row`), and no match means the launch produced nothing
    and the row is forgotten before it haunts ``list`` forever. Adoption is
    therefore confined to genuinely ABANDONED launches, which is what it is for.

    Known non-matching shape: under
    :class:`~kinoforge.core.ephemeral.EphemeralSession` with
    ``pod_name_includes_alias=False`` the RunPod pod is named
    ``kinoforge-<hex>`` rather than the ``run_id`` (see
    ``providers/runpod/__init__.py``), so an ephemeral launching row can never be
    matched and is aged out instead. That is the correct outcome for a session
    whose pod is destroyed at exit anyway, but it means "aged out" does not imply
    "no pod existed" on that path.

    Anything uncertain — an unreadable provider, a malformed ``created_at``, a
    failing ``forget`` — leaves the row exactly where it is.

    Args:
        ledger: Ledger exposing ``forget``, and ideally the adoption methods
            described on :class:`_ReconcileLedger`.
        provider: The resolved provider.
        entry: The launching row. MUTATED IN PLACE on adoption, see
            :func:`_rewrite_adopted_entry`.
        now: Current epoch seconds.
        launching_grace_s: How long a launch may plausibly still be in flight.

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
    age = _row_age(entry, now=now, row_id=row_id)
    if age is None:
        # A row whose age cannot be read is not evidence of anything.
        return None
    if age <= launching_grace_s:
        # Young: safe either way, and cheap — no listing call is made at all.
        return None
    try:
        live = provider.list_instances()
    except Exception as exc:  # noqa: BLE001 — uncertain → keep the row
        logger.debug("reconcile: launching row %s uncertain: %s", row_id, exc)
        return None
    for inst in live:
        if inst.id == run_id or dict(inst.tags).get("name") == run_id:
            if _adopt_launching_row(ledger, row_id=row_id, instance=inst, entry=entry):
                _rewrite_adopted_entry(entry, instance=inst)
            return None
    try:
        if not _forget_launching_row(ledger, row_id):
            return None
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
    entry: dict[str, Any],
) -> bool:
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

    **What gets recorded is NOT the listing verbatim.** Both listing converters
    hard-code ``created_at=0.0`` (``providers/skypilot/__init__.py``,
    ``providers/runpod/__init__.py``) because neither list API returns a
    creation time, and SkyPilot's additionally returns ``tags={}``. Recording
    that as the durable row is not a cosmetic loss:

    * ``created_at=0.0`` gives the adopted row an age of ~56 years, so
      ``Lifecycle`` max-age reaping destroys EVERY adopted instance
      unconditionally, and the overview renders a six-figure ``est≤$`` for
      RunPod, whose listing does carry a real ``costPerHr``.
    * SkyPilot losing ``tags`` loses ``tags["ports"]``, which is the only thing
      ``ensure_endpoints`` can rebuild its tunnels from — a warm attach to an
      adopted cluster could then never reach it.

    So the ledger row's own fields are merged back in: its ``created_at`` (the
    launch time, which is what the row was written with), its ``max_age_s``
    lifecycle snapshot, and its tags UNDER the listing's — the provider's
    reading of a key it does share wins, the launch-phase tag is dropped, and
    everything the listing never knew about survives.
    :func:`_rewrite_adopted_entry` does the same for the caller's in-memory
    copy; this is the durable half, and without it that copy is the only place
    the truth exists.

    Args:
        ledger: The ledger to rewrite.
        row_id: The provisional row's id (the client-side ``run_id``).
        instance: The live instance the row resolved to.
        entry: The provisional row itself, read for the create-time fields the
            provider's listing cannot reproduce.

    Returns:
        True when the provisional row was actually removed. False means the
        ledger still holds it — a missing capability, a refused collapse, or a
        failed write — so the caller must NOT present the row as adopted.
    """
    record = getattr(ledger, "record", None)
    if not callable(record):
        logger.debug(
            "reconcile: ledger cannot record; leaving launching row %s alone", row_id
        )
        return False
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
        return False
    try:
        if not _has_real_row(ledger, instance.id):
            record(
                _merge_row_into_instance(entry, instance),
                max_age_s=_row_max_age_s(entry),
            )
        if callable(collapse):
            collapsed = collapse(row_id, real_id=instance.id)
            if not collapsed:
                # The designed refusal: no non-provisional row under real_id.
                # Keeping the row is right, but silence here is how a
                # permanently stale 'launching' row on a live instance reaches
                # production with nothing to grep for — the same reasoning as
                # orchestrator._collapse_provisional_row's warning.
                logger.warning(
                    "reconcile: collapse refused for launching row %s onto %s; "
                    "the row stays provisional and this instance may show up "
                    "twice (or as launching) in kinoforge list",
                    row_id,
                    instance.id,
                )
                return False
        else:
            ledger.forget(row_id)
    except Exception as exc:  # noqa: BLE001 — adoption is best-effort
        logger.debug("reconcile: adopting launching row %s failed: %s", row_id, exc)
        return False
    logger.info(
        "reconcile: adopted launching row %s onto live instance %s",
        row_id,
        instance.id,
    )
    return True


def _merge_row_into_instance(
    entry: dict[str, Any],
    instance: Any,  # noqa: ANN401 — kinoforge.core.interfaces.Instance, duck-typed
) -> Any:  # noqa: ANN401
    """Return *instance* with the create-time fields its listing cannot know.

    See :func:`_adopt_launching_row` for why recording the listing verbatim is
    wrong. Two fields, and only two, because these are the two the converters
    provably fabricate:

    * ``created_at`` — the row's launch time. The listings hard-code ``0.0``,
      which reads as 1970 and makes the reaper destroy every adopted instance.
      The instance's own value is preferred when the row has none.
    * ``tags`` — the row's tags UNDER the listing's, minus the launch-phase
      tag. Under, not over: a key the provider actually read (RunPod's
      ``mode``/``name``) is truth, while the row's tags carry what the
      provider never saw — SkyPilot's whole tag map, ``tags["ports"]``
      included.

    Never raises: a malformed ``created_at`` or a non-dict ``tags`` falls back
    to the instance's own value, because failing here would abandon an
    adoption that is otherwise perfectly safe.

    Args:
        entry: The provisional ledger row.
        instance: The live instance the row resolved to.

    Returns:
        A new Instance carrying the merged fields.
    """
    try:
        created_at = float(entry.get("created_at") or instance.created_at)
    except (TypeError, ValueError):
        created_at = instance.created_at
    row_tags = entry.get("tags")
    merged: dict[str, str] = {}
    if isinstance(row_tags, dict):
        merged.update(
            {str(k): str(v) for k, v in row_tags.items() if k != _LAUNCH_PHASE_TAG}
        )
    merged.update(dict(instance.tags))
    return dataclasses.replace(instance, created_at=created_at, tags=merged)


def _row_max_age_s(entry: dict[str, Any]) -> int | None:
    """Return the row's recorded ``max_age_s``, or None when unusable.

    The lifecycle snapshot the orchestrator wrote before the launch. Carrying
    it onto the adopted row is what lets the reaper age the instance out on the
    ceiling its own config asked for rather than on the ledger default.

    Args:
        entry: The provisional ledger row.

    Returns:
        The ceiling in seconds, or None when absent or unparseable.
    """
    raw = entry.get("max_age_s")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _rewrite_adopted_entry(
    entry: dict[str, Any],
    *,
    instance: Any,  # noqa: ANN401 — kinoforge.core.interfaces.Instance, duck-typed
) -> None:
    """Point the CALLER's copy of an adopted row at the real id.

    The reconciler's callers hold the entry dicts they read before it ran and
    print them afterwards. Leaving an adopted row keyed by the ``run_id`` would
    make ``kinoforge list`` display an id the ledger no longer holds, and
    ``kinoforge destroy --id <that id>`` — copied from what was printed — would
    fail against a pod that is live and billing.

    Deliberately narrow: the id and the launch-phase tag, nothing else. The row's
    ``created_at`` stays the LAUNCH time rather than the listing's (RunPod's
    listing reports ``0.0``, which would render an astronomical ``est_spend``),
    and the ledger's own row is the authority for everything else from the next
    invocation on. That durable row is not the listing either — see
    :func:`_merge_row_into_instance`, which keeps the same ``created_at`` for
    exactly the same reason.

    Args:
        entry: The caller's row dict, mutated in place.
        instance: The live instance the row was adopted onto.
    """
    entry["id"] = instance.id
    tags = entry.get("tags")
    if isinstance(tags, dict):
        entry["tags"] = {k: v for k, v in tags.items() if k != _LAUNCH_PHASE_TAG}


def _has_real_row(ledger: _ReconcileLedger, instance_id: str) -> bool:
    """Return whether the ledger already holds a NON-provisional row for an id.

    Guards the one thing ``record`` cannot undo: it APPENDS, so recording an
    instance the ledger already knows leaves two rows under one id and every
    per-id reader picks whichever it finds first. Only reachable when the
    orchestrator's own collapse failed (it is best-effort too), which is
    precisely when the reconciler is expected to clean up.

    Prefers ``entries()`` over ``read()`` because ``read`` returns the FIRST row
    for an id and the same-key shape (SkyPilot) can hold TWO — the provisional
    one, written first, in front of the real one. Asking ``read`` there answers
    "provisional", i.e. "no real row", and the caller appends a third. Scanning
    every row is the only question that has a correct answer on that shape;
    ``read`` remains the fallback for ledgers without ``entries``.

    Args:
        ledger: The ledger to interrogate; ``entries`` and ``read`` are both
            optional.
        instance_id: The provider-assigned id.

    Returns:
        True only when a row exists under *instance_id* and is not itself a
        ``launching`` row. False on any doubt, including an unreadable ledger —
        a spurious duplicate row is recoverable, a missing row is not.
    """
    all_rows = getattr(ledger, "entries", None)
    if callable(all_rows):
        try:
            rows = all_rows()
        except Exception as exc:  # noqa: BLE001 — unreadable ledger → try read
            logger.debug("reconcile: ledger entries() failed: %s", exc)
        else:
            return any(
                isinstance(row, dict)
                and row.get("id") == instance_id
                and not _is_launching(row)
                for row in rows
            )
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
