"""Layer V impure substrate: lock-protected verdict dispatch + provider routing.

The only side-effecting consumer of :mod:`kinoforge.core.reaper`.
Every destructive decision flows through ``act_on_verdict`` so the
re-classify-before-act and Layer 18 per-instance lock contracts are
applied once at the substrate level.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kinoforge.core.clock import Clock
from kinoforge.core.errors import TeardownError
from kinoforge.core.lifecycle import Ledger, destroy_confirmed
from kinoforge.core.reaper import Policy, Verdict, classify, partition

if TYPE_CHECKING:
    from kinoforge.core.interfaces import ComputeProvider
    from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger(__name__)

_LOCK_TTL_S: float = 30.0

# B5a: dedup keys for HEARTBEAT_SUBSTRATE_MISSING warnings. Per
# (provider_kind, instance_id) — operators get one line per pod, not
# one per tick. Module-level state is acceptable because the dedup is
# best-effort (a process restart resets it; the alternative would be
# leaking dedup state into the ledger).
_WARNED_SUBSTRATE_MISSING: set[tuple[str, str]] = set()


def reset_warning_dedup() -> None:
    """Clear the substrate-missing WARN dedup set.

    Test helper. Production code does not call this — the dedup persists
    for the life of the process per the documented best-effort contract.
    """
    _WARNED_SUBSTRATE_MISSING.clear()


@dataclass(frozen=True)
class ActionResult:
    """Outcome of a single ``act_on_verdict`` call.

    Attributes:
        instance_id: The id acted on.
        snapshot_verdict: What ``sweep`` classified the entry as.
        applied_verdict: What the act-time re-classify returned (may
            differ from ``snapshot_verdict`` under drift). For
            ``"deferred-session-claim"`` the re-classify never ran, so
            this is set equal to ``snapshot_verdict``.
        action: One of:

            * ``"destroyed_and_forgot"`` — IDLE_REAP / OVERAGE_REAP /
              ORPHAN_REAP destroyed + ledger entry forgotten.
            * ``"forgot"`` — STALE_LEDGER: entry forgotten, no destroy.
            * ``"forgot_unroutable"`` — sweep-level: provider routing
              failed; the entry was forgotten without contacting any
              provider.
            * ``"skipped"`` — drift between snapshot and act-time verdict.
            * ``"failed"`` — TeardownError during destroy.
            * ``"no_op"`` — LIVE / HEARTBEAT_UNKNOWN /
              HEARTBEAT_SUBSTRATE_MISSING: no action required.
            * ``"deferred-session-claim"`` — B7: orchestrator holds
              ``provision:<id>`` (mid-session-claim). Reaper logs INFO
              and skips this entry on this sweep pass; the next sweep
              re-evaluates.
        reason: Free-text explanation for skipped / failed / deferred-
            session-claim actions. For ``deferred-session-claim`` the
            reason contains the holder PID when readable from the lock
            sidecar.
    """

    instance_id: str
    snapshot_verdict: Verdict
    applied_verdict: Verdict
    action: str
    reason: str | None = None


@dataclass(frozen=True)
class SweepReport:
    """Output of :func:`sweep` — verdict snapshot + per-action results."""

    snapshot: Mapping[str, tuple[Mapping[str, Any], Verdict]]
    actions: list[ActionResult]


def provider_for(
    entry: Mapping[str, Any],
    registry_get_provider: Callable[[str], Callable[[], ComputeProvider]],
    cache: dict[str, ComputeProvider | None],
) -> ComputeProvider | None:
    """Resolve a provider for an entry; ``None`` when unroutable.

    Caches by provider name within a sweep so N entries with the same
    provider produce one factory call. Caches ``None`` on failure so a
    misconfigured provider is reported once per sweep, not N times.

    Args:
        entry: Ledger entry.
        registry_get_provider: Usually ``kinoforge.core.registry.get_provider``.
        cache: Per-sweep cache; mutated.

    Returns:
        Resolved ``ComputeProvider`` or ``None`` if construction failed.
    """
    name = str(entry.get("provider", "local"))
    if name in cache:
        return cache[name]
    try:
        provider = registry_get_provider(name)()
    except Exception as exc:  # noqa: BLE001 — any vendor failure → unroutable
        _log.warning("provider %r unroutable: %s", name, exc)
        cache[name] = None
        return None
    cache[name] = provider
    return provider


def _probe_session_claim_holder(store: ArtifactStore, instance_id: str) -> int | None:
    """Non-blocking probe of ``provision:<instance_id>``.

    B7 reaper-side hook. When the orchestrator's
    :func:`kinoforge.core.session_claim.hold_until_first_tick` holds the
    key, this probe returns the holder PID so the reaper can defer with
    a helpful diagnostic.

    Args:
        store: Artifact store providing the lock.
        instance_id: The instance whose claim lock to probe.

    Returns:
        The orchestrator's PID when the lock is held, ``-1`` when the
        lock is held but the holder PID is not readable (cloud-store or
        unreadable sidecar), or ``None`` when the lock is free (probe-
        success — caller proceeds with destroy). Probe acquires-then-
        releases immediately on success; the caller does NOT hold
        ``provision:<id>`` during the destroy flow.

    Implementation note: the probe uses ``ttl_s=0.0`` because we are
    not claiming the lock for any duration. When acquire succeeds the
    sidecar is briefly rewritten with an immediately-expired TTL and
    then released; no other process is in a "wait for TTL to expire"
    path because orchestrators always use blocking acquire.
    """
    probe = store.acquire_lock(f"provision:{instance_id}", ttl_s=0.0)
    token = probe.acquire(blocking=False)
    if token is not None:
        probe.release(token)
        return None
    # Lock held — read holder PID from sidecar if we can.
    holder_pid: int | None = None
    try:
        from kinoforge.core.locks import _sanitize_key
        from kinoforge.stores.local import LocalArtifactStore

        if isinstance(store, LocalArtifactStore):
            import json

            sanitized = _sanitize_key(f"provision:{instance_id}")
            sidecar = store.root / "_locks" / f"{sanitized}.lock"
            if sidecar.exists():
                data = json.loads(sidecar.read_text())
                holder_pid = int(data.get("holder_pid", 0)) or None
    except (OSError, ValueError, KeyError, TypeError):
        holder_pid = None
    # Sentinel: unknown holder still returns a sentinel int so the
    # caller's None-check distinguishes "lock free" from "lock held by
    # unknown PID".
    return holder_pid if holder_pid is not None else -1


def act_on_verdict(
    store: ArtifactStore,
    ledger: Ledger,
    provider: ComputeProvider,
    entry: Mapping[str, Any],
    snapshot_verdict: Verdict,
    *,
    thresholds: Mapping[str, Any],
    clock: Clock,
) -> ActionResult:
    """Lock + re-classify + dispatch. The single side-effecting surface.

    Layer V D9 + D10: holds ``reaper/<id>`` for the whole compute round
    trip so concurrent reapers/daemon serialise at instance granularity.
    Re-classifies inside the lock so the human-in-the-loop window
    between dry-run snapshot and ``--apply`` is closed.

    Args:
        store: Artifact store providing the cross-process lock.
        ledger: Ledger to mutate on ``forgot`` actions.
        provider: Provider to query / destroy through.
        entry: Ledger entry being acted on.
        snapshot_verdict: The verdict ``sweep`` recorded for this entry.
        thresholds: Threshold kwargs forwarded to ``classify``.
        clock: Wall-clock source for the re-classify timestamp.

    Returns:
        :class:`ActionResult` describing what happened. Never raises;
        ``TeardownError`` becomes ``action="failed"``.
    """
    instance_id = str(entry["id"])
    with store.acquire_lock(f"reaper/{instance_id}", ttl_s=_LOCK_TTL_S):
        # B7: non-blocking probe of provision:<id>. If an orchestrator
        # process holds it, this entry is mid-session-claim — skip this
        # sweep, log INFO with holder_pid, retry next pass.
        deferred = _probe_session_claim_holder(store, instance_id)
        if deferred is not None:
            _log.info(
                "instance %s mid-session-claim (held by pid %s); deferring to next sweep",
                instance_id,
                deferred,
            )
            return ActionResult(
                instance_id=instance_id,
                snapshot_verdict=snapshot_verdict,
                applied_verdict=snapshot_verdict,
                action="deferred-session-claim",
                reason=f"held by pid {deferred}; orchestrator mid-session-claim",
            )
        live_ids = {i.id for i in provider.list_instances()}
        v2 = classify(entry, live_ids, clock.now(), **thresholds)
        if v2 != snapshot_verdict:
            return ActionResult(
                instance_id=instance_id,
                snapshot_verdict=snapshot_verdict,
                applied_verdict=v2,
                action="skipped",
                reason=f"verdict drift {snapshot_verdict.value} -> {v2.value}",
            )
        try:
            if v2 == Verdict.HEARTBEAT_SUBSTRATE_MISSING:
                # Conservative-on-ignorance. The substrate hasn't shipped
                # for this provider yet (e.g. SkyPilot pre-B5b). Operator
                # cannot fix it by destroying the pod. Skip + WARN-once.
                provider_kind = str(entry.get("provider_kind", ""))
                dedup_key = (provider_kind, instance_id)
                if dedup_key not in _WARNED_SUBSTRATE_MISSING:
                    _WARNED_SUBSTRATE_MISSING.add(dedup_key)
                    _log.warning(
                        "provider %r has no heartbeat substrate; "
                        "skipping reap decision for %s (B5b pending)",
                        provider_kind,
                        instance_id,
                    )
                action = "no_op"
            elif v2 in {
                Verdict.IDLE_REAP,
                Verdict.OVERAGE_REAP,
                Verdict.ORPHAN_REAP,
                Verdict.STALL_REAP,  # C26
            }:
                destroy_confirmed(provider, instance_id, sleep=lambda _: None)
                ledger.forget(instance_id)
                action = "destroyed_and_forgot"
            elif v2 == Verdict.STALE_LEDGER:
                ledger.forget(instance_id)
                action = "forgot"
            else:
                # LIVE / HEARTBEAT_UNKNOWN → no_op.
                # UNROUTABLE is unreachable here: classify never returns it and
                # sweep skips UNROUTABLE entries (no provider to invoke). The
                # `forgot_unroutable` path lives in sweep() — see Layer V T5.
                action = "no_op"
        except TeardownError as exc:
            return ActionResult(
                instance_id=instance_id,
                snapshot_verdict=snapshot_verdict,
                applied_verdict=v2,
                action="failed",
                reason=str(exc),
            )
        return ActionResult(
            instance_id=instance_id,
            snapshot_verdict=snapshot_verdict,
            applied_verdict=v2,
            action=action,
        )


def sweep(
    store: ArtifactStore,
    ledger: Ledger,
    registry_get_provider: Callable[[str], Callable[[], ComputeProvider]],
    thresholds: Mapping[str, Any],
    clock: Clock,
    *,
    policy: Policy | None = None,
) -> SweepReport:
    """Classify all ledger entries; optionally act.

    Caches resolved providers and ``list_instances()`` results per
    provider name within the call so N entries with the same provider
    produce one factory call and one ``list_instances`` round-trip.

    Failure isolation: a single ``list_instances`` exception demotes
    that provider's entries to ``UNROUTABLE`` for the rest of the
    sweep but does not abort the sweep. A single ``TeardownError`` is
    captured by ``act_on_verdict`` as ``action="failed"`` and does
    not propagate.

    Force-forget UNROUTABLE: when ``policy.act_verdicts`` contains
    :attr:`Verdict.UNROUTABLE`, a separate post-pass acquires the
    same ``reaper/<id>`` lock and forgets each UNROUTABLE entry.
    Architectural note: ``act_on_verdict`` cannot reach this path
    because UNROUTABLE entries lack a provider — sweep is the
    correct home.

    Args:
        store: Artifact store for the cross-process lock used by
            ``act_on_verdict`` and the UNROUTABLE force-forget path.
        ledger: Ledger to enumerate and mutate.
        registry_get_provider: Usually ``kinoforge.core.registry.get_provider``.
        thresholds: Threshold kwargs forwarded to ``classify``.
        clock: Wall-clock source.
        policy: When ``None``, sweep is read-only (no actions returned).
            Otherwise, snapshot entries whose verdict is in
            ``policy.act_verdicts`` are dispatched to
            ``act_on_verdict`` (or the UNROUTABLE force-forget loop).

    Returns:
        :class:`SweepReport` with the verdict snapshot and (optional)
        action results.
    """
    now = clock.now()
    provider_cache: dict[str, ComputeProvider | None] = {}
    live_pod_ids_cache: dict[str, set[str]] = {}

    entries = list(ledger.entries())
    snapshot: dict[str, tuple[Mapping[str, Any], Verdict]] = {}

    for entry in entries:
        eid = str(entry["id"])
        # Layer W: synthetic daemon-liveness entry written by SweeperLoop;
        # not a reapable pod. Reserved prefix at substrate level — joins
        # `_lifecycle` (run_id) and `_cost_cache` as the third reserved
        # kinoforge namespace. See B1 spec §4.4.
        if eid.startswith("sweeper:"):
            continue
        provider = provider_for(entry, registry_get_provider, provider_cache)
        if provider is None:
            snapshot[eid] = (entry, Verdict.UNROUTABLE)
            continue
        name = str(entry.get("provider", "local"))
        if name not in live_pod_ids_cache:
            try:
                live_pod_ids_cache[name] = {i.id for i in provider.list_instances()}
            except Exception as exc:  # noqa: BLE001
                _log.warning("list_instances failed on %s: %s", name, exc)
                live_pod_ids_cache[name] = set()
                provider_cache[name] = None
                snapshot[eid] = (entry, Verdict.UNROUTABLE)
                continue
        verdict = classify(entry, live_pod_ids_cache[name], now, **thresholds)
        snapshot[eid] = (entry, verdict)

    if policy is None:
        return SweepReport(snapshot=snapshot, actions=[])

    to_act, _to_skip = partition({eid: v for eid, (_, v) in snapshot.items()}, policy)
    actions: list[ActionResult] = []
    for eid, verdict in to_act.items():
        act_entry: Mapping[str, Any] = snapshot[eid][0]
        if verdict == Verdict.UNROUTABLE:
            # UNROUTABLE entries have no provider — act_on_verdict cannot
            # handle them. When policy opts in (--force-forget), sweep
            # acquires the same reaper/<id> lock and forgets directly.
            with store.acquire_lock(f"reaper/{eid}", ttl_s=_LOCK_TTL_S):
                ledger.forget(eid)
            actions.append(
                ActionResult(
                    instance_id=eid,
                    snapshot_verdict=Verdict.UNROUTABLE,
                    applied_verdict=Verdict.UNROUTABLE,
                    action="forgot_unroutable",
                )
            )
            continue
        name = str(act_entry.get("provider", "local"))
        provider = provider_cache.get(name)
        if provider is None:
            # Defensive: any non-UNROUTABLE verdict was set by classify,
            # which only runs when the provider resolved successfully.
            continue
        result = act_on_verdict(
            store,
            ledger,
            provider,
            act_entry,
            verdict,
            thresholds=thresholds,
            clock=clock,
        )
        actions.append(result)
    return SweepReport(snapshot=snapshot, actions=actions)
