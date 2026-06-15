"""C30 probe helpers for fault-isolation of the RunPod restart loop.

Provides direct-GraphQL pod probes, S3 trap-fire counting, verdict
classification, spend-ledger enforcement, and verify-and-retry destroy.
All public helpers are documented in
``docs/superpowers/specs/2026-06-14-c30-restart-loop-diagnosis-design.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum


class Verdict(Enum):
    """Outcome classes for a 10-minute probe window.

    SURVIVED  — pod stayed up the whole window; no trap fires; uptime
                monotonically increased across all samples.
    RESTARTED — pod cycled >=3 times within the window (trap-fire count
                is the authoritative signal; uptime drops corroborate).
    AMBIGUOUS — evidence cannot distinguish the two; rerun the probe
                or treat as RESTARTED conservatively per spec §3.
    """

    SURVIVED = "survived"
    RESTARTED = "restarted"
    AMBIGUOUS = "ambiguous"


def classify_run(
    poll_trail: Sequence[tuple[float, int | None]],
    fire_count: int,
) -> Verdict:
    """Classify a probe run from its poll trail and S3 trap-fire count.

    Args:
        poll_trail: ``(elapsed_seconds, uptime_in_seconds)`` per sample.
            ``uptime_in_seconds`` may be ``None`` when the GraphQL
            ``pod(podId)`` response lacked a ``runtime`` block (transient).
        fire_count: Number of ``diag-*.txt`` objects under the run's
            S3 prefix.

    Returns:
        Verdict per spec §3 rules.
    """
    if fire_count >= 3:
        return Verdict.RESTARTED
    if fire_count >= 1:
        return Verdict.AMBIGUOUS
    if len(poll_trail) < 2:
        return Verdict.AMBIGUOUS
    raw = [u for _, u in poll_trail]
    if any(u is None for u in raw):
        return Verdict.AMBIGUOUS
    uptimes: list[int] = [u for u in raw if u is not None]
    for prev, curr in zip(uptimes, uptimes[1:], strict=False):
        if curr <= prev:
            return Verdict.AMBIGUOUS
    return Verdict.SURVIVED
