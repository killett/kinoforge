"""Layer X: pure cost-aggregator substrate.

CLI owns ledger read + balance reads + classify call + env-var read +
render. This module folds the inputs into a :class:`CostSnapshot`. Bad
ledger entries (missing ``id`` / malformed ``cost_rate_usd_per_hr``) are
isolated: that entry is skipped silently; the rest of the snapshot is
honest. Same isolation contract as :func:`kinoforge.core.reaper_actor.sweep`.

No I/O. No mutable globals. No imports from providers / engines / sources.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kinoforge.core.balance_endpoints import ProviderBalance
from kinoforge.core.reaper import Verdict

__all__ = [
    "_BURNING_VERDICTS",
    "CostSnapshot",
    "ProviderBreakdown",
    "aggregate",
]

_BURNING_VERDICTS: frozenset[Verdict] = frozenset(
    {
        Verdict.LIVE,
        Verdict.IDLE_REAP,
        Verdict.OVERAGE_REAP,
        Verdict.ORPHAN_REAP,
        Verdict.HEARTBEAT_UNKNOWN,
        Verdict.HEARTBEAT_SUBSTRATE_MISSING,
    }
)


@dataclass(frozen=True)
class ProviderBreakdown:
    """One row in the per-provider table.

    Attributes:
        provider: Provider kind string (e.g. ``"runpod"``).
        burn_rate_usd_per_hr: Sum of ``cost_rate_usd_per_hr`` across
            entries with a verdict in :data:`_BURNING_VERDICTS`.
        spend_usd_total: Sum of ``rate * hours_up`` across the same set.
            ``hours_up = max(0, (now - created_at) / 3600)``.
        pod_counts_by_verdict: All 8 Verdict keys; zeros included for
            verdicts not seen on this provider.
    """

    provider: str
    burn_rate_usd_per_hr: float
    spend_usd_total: float
    pod_counts_by_verdict: Mapping[Verdict, int]


@dataclass(frozen=True)
class CostSnapshot:
    """Authoritative aggregator output.

    The CLI render paths (human / --json / --prom) all derive from this
    snapshot. Field defaults match spec §10's JSON schema; future micro-
    layers add keys but never rename.
    """

    as_of: datetime
    burn_rate_usd_per_hr: float
    per_provider: tuple[ProviderBreakdown, ...]
    balances: Mapping[str, ProviderBalance | None]
    balance_errors: Mapping[str, str]
    heartbeat_partial_truth: tuple[str, ...]
    hosted_spend_pending: bool = True
    throttle_warnings: tuple[str, ...] = field(default_factory=tuple)


def aggregate(
    *,
    entries: Sequence[Mapping[str, Any]],
    verdicts_by_id: Mapping[str, Verdict],
    now: datetime,
    balances: Mapping[str, ProviderBalance | None],
    balance_errors: Mapping[str, str],
    heartbeat_partial_truth: tuple[str, ...],
    throttle_warnings: tuple[str, ...] = (),
) -> CostSnapshot:
    """Fold ledger entries + verdicts into a :class:`CostSnapshot`.

    Pure. ``entries`` order does not affect outputs (per-provider tuple
    is sorted by provider name ascending). ``balances`` /
    ``balance_errors`` / ``heartbeat_partial_truth`` / ``throttle_warnings``
    are pass-through from the CLI; aggregator does no I/O and does not
    look up balances.

    Args:
        entries: Ledger entries (from :meth:`Ledger.entries`). Each must
            carry ``id`` and ``provider``; missing keys are tolerated
            (bad entries skipped silently).
        verdicts_by_id: Pre-computed verdict per entry id, from
            :func:`kinoforge.core.reaper.classify`. Entries whose id is
            absent from this mapping are skipped (defensive — caller
            should pass a verdict for every entry).
        now: Wall-clock used for ``as_of`` and ``spend_usd_total`` math.
        balances: Per-provider balance read; ``None`` is missing-cred /
            no-satisfier / transport failure.
        balance_errors: Per-provider error message; empty when all OK.
        heartbeat_partial_truth: Provider kinds whose verdicts may be
            HEARTBEAT_SUBSTRATE_MISSING because the wire substrate has
            not shipped yet (B5b SkyPilot).
        throttle_warnings: Provider-warning strings (Replicate $5
            throttle gate); empty when none active.

    Returns:
        A fresh frozen :class:`CostSnapshot`.
    """
    by_provider: dict[str, dict[str, Any]] = {}
    for entry in entries:
        instance_id_raw = entry.get("id")
        if instance_id_raw is None:
            continue
        instance_id = str(instance_id_raw)
        verdict = verdicts_by_id.get(instance_id)
        if verdict is None:
            continue
        provider = str(entry.get("provider", "unknown"))
        try:
            rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
        except (TypeError, ValueError):
            continue
        try:
            created_at = float(entry.get("created_at", now.timestamp()))
        except (TypeError, ValueError):
            created_at = now.timestamp()
        slot = by_provider.setdefault(
            provider,
            {"burn": 0.0, "spend": 0.0, "counts": dict.fromkeys(Verdict, 0)},
        )
        slot["counts"][verdict] = slot["counts"][verdict] + 1
        if verdict in _BURNING_VERDICTS:
            slot["burn"] += rate
            hours_up = max(0.0, (now.timestamp() - created_at) / 3600.0)
            slot["spend"] += rate * hours_up

    per_provider = tuple(
        ProviderBreakdown(
            provider=provider,
            burn_rate_usd_per_hr=slot["burn"],
            spend_usd_total=slot["spend"],
            pod_counts_by_verdict=dict(slot["counts"]),
        )
        for provider, slot in sorted(by_provider.items())
    )
    total_burn = sum(p.burn_rate_usd_per_hr for p in per_provider)
    return CostSnapshot(
        as_of=now,
        burn_rate_usd_per_hr=total_burn,
        per_provider=per_provider,
        balances=dict(balances),
        balance_errors=dict(balance_errors),
        heartbeat_partial_truth=heartbeat_partial_truth,
        throttle_warnings=throttle_warnings,
    )
