"""Layer V: pure decision-tree substrate for the heartbeat-aware reaper.

No I/O. No mutable globals. Every consumer (CLI, future Layer W
sweeper daemon, future Layer Y orchestrator hook) shares the same
``classify`` / ``Policy`` / ``partition`` surface.

The sentinel-gate contract documented in
:meth:`kinoforge.core.lifecycle.Ledger.touch` is realised entirely in
``classify`` — this is the single place that consults
``heartbeat_thread_tick`` for a destructive decision.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from kinoforge.core.heartbeat_endpoints import provider_heartbeat_supported
from kinoforge.core.util_endpoints import provider_util_supported


class Verdict(StrEnum):
    """Possible classification outcomes for a single ledger entry.

    Insertion order is part of the public contract — Layer W daemons
    and Layer Y orchestrator hooks may serialise verdict values.
    """

    LIVE = "LIVE"
    IDLE_REAP = "IDLE_REAP"
    ORPHAN_REAP = "ORPHAN_REAP"
    OVERAGE_REAP = "OVERAGE_REAP"
    STALE_LEDGER = "STALE_LEDGER"
    HEARTBEAT_UNKNOWN = "HEARTBEAT_UNKNOWN"
    HEARTBEAT_SUBSTRATE_MISSING = "HEARTBEAT_SUBSTRATE_MISSING"  # B5a
    UNROUTABLE = "UNROUTABLE"
    STALL_REAP = "STALL_REAP"  # C26
    RESTART_LOOP_REAP = "RESTART_LOOP_REAP"  # C27
    DEGRADED_REAP = "DEGRADED_REAP"  # Layer LoRA — pod self-marked degraded
    # Sweeper-side ephemeral reap (spec 2026-06-28):
    GC_404 = "GC_404"  # Provider 404 — remove EphemeralIndex row, no destroy
    SKIP_NO_PROBE = "SKIP_NO_PROBE"  # Provider lacks probe_runtime substrate
    PROBE_FAILED = "PROBE_FAILED"  # Probe raised transient TransportError
    POD_GONE = "POD_GONE"  # 2026-07-06 — provider confirmed pod absent mid-run


@dataclass(frozen=True)
class Policy:
    """Which verdicts the consumer chooses to act on.

    Dry-run = ``Policy(frozenset())``. CLI ``--apply`` builds
    :data:`DEFAULT_APPLY_POLICY`; opt-ins union additional verdicts in.
    Future Layer W daemon constructs from YAML config.
    """

    act_verdicts: frozenset[Verdict]


DEFAULT_APPLY_POLICY = Policy(
    act_verdicts=frozenset(
        {
            Verdict.IDLE_REAP,
            Verdict.OVERAGE_REAP,
            Verdict.STALE_LEDGER,
            Verdict.STALL_REAP,  # C26
            Verdict.RESTART_LOOP_REAP,  # C27
            Verdict.DEGRADED_REAP,  # Layer LoRA
            Verdict.GC_404,  # Ephemeral row cleanup (sweeper-ephemeral-reap)
        }
    )
)

DEFAULT_STRICT_VERDICTS: frozenset[Verdict] = frozenset(
    {
        Verdict.UNROUTABLE,
        Verdict.HEARTBEAT_UNKNOWN,
        Verdict.HEARTBEAT_SUBSTRATE_MISSING,  # NEW (B5a)
    }
)


def policy_from_cli_flags(
    *,
    apply: bool,
    include_orphans: bool = False,
    force_forget: bool = False,
) -> Policy:
    """Build the Policy a CLI invocation should use.

    Args:
        apply: True iff ``--apply`` was set; False is dry-run.
        include_orphans: True iff ``--include-orphans`` was set.
        force_forget: True iff ``--force-forget`` was set.

    Returns:
        Empty-act-set Policy when ``apply=False`` (dry-run).
        ``DEFAULT_APPLY_POLICY`` plus opt-ins otherwise.
    """
    if not apply:
        return Policy(act_verdicts=frozenset())
    act = set(DEFAULT_APPLY_POLICY.act_verdicts)
    if include_orphans:
        act.add(Verdict.ORPHAN_REAP)
    if force_forget:
        act.add(Verdict.UNROUTABLE)
    return Policy(act_verdicts=frozenset(act))


def _resolve(entry: Mapping[str, Any], field: str, default: float) -> float:
    """Per-entry threshold override with type-safe fallback.

    Mirrors Layer S ``_ledger_field_or_cfg``. Defensive against ledger
    corruption: bad types fall through to the default rather than
    raising, because raising inside ``classify`` would abort the whole
    sweep on one bad entry.

    Args:
        entry: The ledger entry being classified.
        field: Threshold field name (e.g. ``"idle_timeout_s"``).
        default: Cfg-derived fallback when the entry does not override.

    Returns:
        Float threshold value.
    """
    val = entry.get(field)
    if val is None:
        return float(default)
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(default)


def _stall_reap_predicate(
    entry: Mapping[str, Any],
    *,
    now: float,
    sentinel_window: float,
    heartbeat_interval_s: float,
    stall_window_s: float | None,
) -> bool:
    """Return True iff the entry should fire STALL_REAP (C26 row 3').

    Args:
        entry: The ledger entry being classified.
        now: Wall-clock seconds.
        sentinel_window: ``3 * heartbeat_interval_s`` — used as the
            util-tick freshness ceiling too (a stale util tick means we
            cannot trust the counter even if it's high).
        heartbeat_interval_s: Cfg heartbeat cadence; counter × interval
            gives the cumulative low-util duration in seconds.
        stall_window_s: Cfg-level threshold; None = kill-switch (return False).

    Returns:
        True when:
          1. Feature on (effective window is not None), AND
          2. Provider has a util substrate (or provider unknown), AND
          3. Counter and util_tick both present on the entry, AND
          4. util_tick fresh (age <= sentinel_window), AND
          5. counter × heartbeat_interval_s >= effective window.
        Per-entry ``stall_window_s`` override beats the default.
    """
    override = entry.get("stall_window_s")
    if override is not None:
        try:
            effective_window: float | None = float(override)
        except (TypeError, ValueError):
            effective_window = stall_window_s
    else:
        effective_window = stall_window_s
    if effective_window is None:
        return False
    provider_kind = entry.get("provider_kind") or entry.get("provider")
    if provider_kind is not None and not provider_util_supported(str(provider_kind)):
        return False
    counter = entry.get("consecutive_low_util_count")
    util_tick = entry.get("util_thread_tick")
    if counter is None or util_tick is None:
        return False
    try:
        counter_i = int(counter)
        util_age = now - float(util_tick)
    except (TypeError, ValueError):
        return False
    if util_age > sentinel_window:
        return False
    return counter_i * heartbeat_interval_s >= effective_window


def _restart_loop_reap_predicate(
    entry: Mapping[str, Any],
    *,
    now: float,
    sentinel_window: float,
    heartbeat_interval_s: float,
    restart_loop_window_s: float | None,
) -> bool:
    """Return True iff the entry should fire RESTART_LOOP_REAP (C27 row 3'').

    Twin of :func:`_stall_reap_predicate`. Same defensive shape: bad
    types fall through to default rather than raising, because raising
    inside ``classify`` would abort the whole sweep on one bad entry.

    Args:
        entry: The ledger entry being classified.
        now: Wall-clock seconds.
        sentinel_window: ``3 * heartbeat_interval_s`` — util-tick
            freshness ceiling. A stale util tick means we cannot trust
            the counter even if it's high.
        heartbeat_interval_s: Cfg heartbeat cadence; counter × interval
            gives cumulative low-uptime duration in seconds.
        restart_loop_window_s: Cfg-level threshold; None = kill switch.

    Returns:
        True when:
          1. Feature on (effective window is not None), AND
          2. Provider has a util substrate (or provider unknown), AND
          3. ``consecutive_low_uptime_count`` and ``util_thread_tick``
             both present, AND
          4. util_thread_tick fresh (age <= sentinel_window), AND
          5. counter × heartbeat_interval_s >= effective window.
        Per-entry ``restart_loop_window_s`` override beats the default.
    """
    override = entry.get("restart_loop_window_s")
    if override is not None:
        try:
            effective_window: float | None = float(override)
        except (TypeError, ValueError):
            effective_window = restart_loop_window_s
    else:
        effective_window = restart_loop_window_s
    if effective_window is None:
        return False
    provider_kind = entry.get("provider_kind") or entry.get("provider")
    if provider_kind is not None and not provider_util_supported(str(provider_kind)):
        return False
    counter = entry.get("consecutive_low_uptime_count")
    util_tick = entry.get("util_thread_tick")
    if counter is None or util_tick is None:
        return False
    try:
        counter_i = int(counter)
        util_age = now - float(util_tick)
    except (TypeError, ValueError):
        return False
    if util_age > sentinel_window:
        return False
    return counter_i * heartbeat_interval_s >= effective_window


def _num(value: Any) -> float | None:  # noqa: ANN401 — reads untyped entry dicts
    """Coerce a probe reading to float; ``None`` when absent or unusable.

    Args:
        value: A ``gpu_util_pct`` / ``cpu_pct`` reading off a synthesised
            ephemeral entry. Providers report ``None`` when the field is
            simply not available (RunPod returns a null GPU array during
            early boot).

    Returns:
        The float value, or ``None`` when it is missing or not numeric.
        ``None`` means "not observed" — never "zero".
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ephemeral_orphan_reason(entry: Mapping[str, Any], now: float) -> str:
    """Describe the evidence behind an ephemeral ORPHAN_REAP.

    A destroy that records only the verdict is unreviewable — the operator
    whose render vanished cannot tell whether the daemon observed an idle
    GPU or guessed. This string is attached to the
    :class:`~kinoforge.core.reaper_actor.ActionResult` and logged at WARNING
    before the destroy, so the age and the utilisation that justified it
    survive in the record.

    Args:
        entry: The synthesised ephemeral entry that was classified.
        now: Wall-clock seconds at the moment of the decision.

    Returns:
        A one-line reason naming the pod's age in seconds and the GPU/CPU
        readings observed, e.g. ``"ephemeral orphan: age=7200s idle on
        probe gpu_util=0.0% cpu=1.5%"``. Unobserved readings render as
        ``unknown`` rather than ``0``.
    """
    age = now - float(entry.get("created_at", now))
    gpu = _num(entry.get("gpu_util_pct"))
    cpu = _num(entry.get("cpu_pct"))
    gpu_s = "unknown" if gpu is None else f"{gpu:.1f}%"
    cpu_s = "unknown" if cpu is None else f"{cpu:.1f}%"
    return (
        f"ephemeral orphan: age={age:.0f}s idle on probe gpu_util={gpu_s} cpu={cpu_s}"
    )


def _ephemeral_orphan_predicate(
    entry: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    age_s: float,
) -> bool:
    """Return True iff an ephemeral row is old enough AND idle (spec C1).

    Both halves are required, and neither is sufficient:

    * **Age alone must never reap.** A four-hour render is not a leak; a
      pure TTL would destroy the operator's work.
    * **Idleness alone must never reap.** A Wan A14B cold boot spends ~25
      of its ~30 minutes at 0% GPU fetching 70 GB of weights.

    Conservative on ignorance throughout: a missing threshold, a missing
    reading, or a non-numeric reading all return False. "Not observed" is
    never read as "idle".

    Args:
        entry: Synthesised ephemeral entry; must carry ``probe_state``
            ``"ok"`` for any reap to be considered.
        thresholds: Threshold mapping. ``ephemeral_orphan_age_s`` is the
            age gate (``None`` = kill switch, the default); idleness is
            measured against ``stall_gpu_threshold`` /
            ``stall_cpu_threshold``, the same definition of a "low-util
            sample" the STALL_REAP path uses.
        age_s: ``now - created_at``, where ``created_at`` came from the
            index row's ``created_at_local`` — the pod's BIRTH time, so
            this age includes the whole cold boot (spec A2).

    Returns:
        True when the pod is strictly older than the age gate and both the
        GPU and the CPU reading sit below their idle thresholds.
    """
    if entry.get("probe_state") != "ok":
        return False
    age_gate = _num(thresholds.get("ephemeral_orphan_age_s"))
    if age_gate is None or age_gate <= 0.0:
        return False
    if age_s <= age_gate:
        return False
    gpu = _num(entry.get("gpu_util_pct"))
    cpu = _num(entry.get("cpu_pct"))
    if gpu is None or cpu is None:
        return False
    gpu_thresh = float(thresholds.get("stall_gpu_threshold") or 0.0)
    cpu_thresh = float(thresholds.get("stall_cpu_threshold") or 0.0)
    return gpu < gpu_thresh and cpu < cpu_thresh


def _ephemeral_stall_predicate(
    entry: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    *,
    stall_history: Mapping[str, deque[tuple[float, float]]] | None,
) -> bool:
    """Return True iff N consecutive samples sit below both util thresholds.

    Extracted verbatim from the previous inline body of
    :func:`_classify_ephemeral` so the orphan rule can be evaluated after
    it without being stranded behind its early returns.

    Args:
        entry: Synthesised ephemeral entry.
        thresholds: Reads ``stall_window_s`` (``0``/absent = off),
            ``heartbeat_interval_s``, ``stall_gpu_threshold`` and
            ``stall_cpu_threshold``.
        stall_history: Per-pod deque of ``(gpu_util_pct, cpu_pct)`` samples
            owned by ``SweeperLoop``. ``None`` (``kinoforge reap`` one-shot
            mode) disables STALL_REAP entirely.

    Returns:
        True when the history holds at least
        ``ceil(stall_window_s / heartbeat_interval_s)`` samples and every
        one of the most recent that many is below both thresholds.
    """
    if stall_history is None:
        return False
    stall_window_s = float(thresholds.get("stall_window_s") or 0.0)
    interval_s = float(thresholds.get("heartbeat_interval_s") or 30.0)
    if stall_window_s <= 0.0:
        return False
    required = max(1, math.ceil(stall_window_s / interval_s))
    history = stall_history.get(str(entry["id"]))
    if history is None or len(history) < required:
        return False
    gpu_thresh = float(thresholds.get("stall_gpu_threshold") or 0.0)
    cpu_thresh = float(thresholds.get("stall_cpu_threshold") or 0.0)
    recent = list(history)[-required:]
    return all(g < gpu_thresh and c < cpu_thresh for (g, c) in recent)


#: How long a ``not_found`` ephemeral row is presumed to be a launch still in
#: flight rather than a dead pod. Same value and the same asymmetry as
#: ``cli/_reconcile._LAUNCHING_GRACE_S``: acting too early deletes the handle on
#: a pod that is booting AND BILLING, while acting too late only prolongs a
#: $0.00 ghost row that removes no resource when it finally goes.
_EPHEMERAL_GC_404_GRACE_S: float = 1800.0


def _ephemeral_pre_create_grace(entry: Mapping[str, Any], now: float) -> bool:
    """Return True iff a ``not_found`` row is still plausibly a live launch.

    Two conditions, both required:

    * the row carries **no endpoints** — nothing has ever confirmed it names a
      reachable resource, which is the shape
      ``cli/_commands._ephemeral_launch_row_reserve`` writes before
      ``create_instance`` and which ``_ephemeral_index_add`` replaces with a
      row carrying the provider-side id and its URLs;
    * it is younger than :data:`_EPHEMERAL_GC_404_GRACE_S`.

    A row that once named a live pod (endpoints present) is GC'd on the first
    ``not_found`` exactly as before, so a genuinely dead pod's row is not held
    open by this.

    Deliberately NOT a configurable threshold. Every tunable in this module is
    wired from ``Lifecycle`` and reaches ``classify`` as an explicit keyword;
    adding one here for a window whose only job is to outlast a cold boot would
    grow the config surface (and a migration) for a knob no operator has a
    reason to move. The module constant is the same shape, and the same value,
    as ``cli/_reconcile._LAUNCHING_GRACE_S``.

    Args:
        entry: Synthesised ephemeral entry.
        now: Wall-clock now (seconds, float).

    Returns:
        True to defer the GC_404 for this tick.
    """
    if entry.get("endpoints"):
        return False
    created_at = float(entry.get("created_at", 0.0))
    if created_at <= 0.0:
        # An unparseable stamp is no evidence of youth — fall through to GC.
        return False
    return (now - created_at) <= _EPHEMERAL_GC_404_GRACE_S


def _classify_ephemeral(
    entry: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    now: float,
    *,
    stall_history: Mapping[str, deque[tuple[float, float]]] | None,
) -> Verdict:
    """Heartbeat-free classification for entries flagged ``kinoforge_ephemeral=True``.

    Decision tree (spec 2026-06-28 §3.5):
      1. probe_state == "not_found"    → GC_404, unless the row is still
         inside the pre-create grace window (no endpoints ever recorded AND
         younger than :data:`_EPHEMERAL_GC_404_GRACE_S`), in which case → LIVE
      2. probe_state == "no_substrate" → SKIP_NO_PROBE
      3. probe_state == "failed"        → PROBE_FAILED
      4. now - created_at > max_lifetime_s → OVERAGE_REAP
      5. N consecutive samples below gpu+cpu thresholds → STALL_REAP
         (N = ceil(stall_window_s / heartbeat_interval_s); skipped when
         ``stall_history`` is None, i.e. one-shot CLI)
      6. old enough AND idle on this tick's probe → ORPHAN_REAP (spec C1)
      7. else → LIVE

    NEVER reads heartbeat keys (last_heartbeat, heartbeat_thread_tick,
    session_claim, restart_count); enforced by AST invariant test (Task 8).

    Args:
        entry: Synthetic ephemeral entry from ``_synthesize_ephemeral_entry``.
        thresholds: Threshold mapping; only ``max_lifetime_s``,
            ``stall_window_s``, ``stall_gpu_threshold``,
            ``stall_cpu_threshold``, ``heartbeat_interval_s``,
            ``ephemeral_orphan_age_s`` are read.
        now: Wall-clock now (seconds, float).
        stall_history: Per-pod deque of ``(gpu_util_pct, cpu_pct)`` samples,
            owned by ``SweeperLoop``. ``None`` from ``kinoforge reap``
            one-shot mode — skip STALL_REAP entirely.

    Returns:
        One of GC_404, SKIP_NO_PROBE, PROBE_FAILED, OVERAGE_REAP,
        STALL_REAP, ORPHAN_REAP, LIVE.
    """
    probe_state = entry.get("probe_state")
    if probe_state == "not_found":
        if _ephemeral_pre_create_grace(entry, now):
            # A row a launch has not yet superseded is NOT evidence of a dead
            # pod: it is keyed by the resource NAME the controller reserved
            # before `create_instance`, and on RunPod that name is not the pod
            # id the probe asks about, so `not_found` is the ONLY answer
            # possible until the post-create update rewrites the row. GC'ing it
            # deletes the one durable handle on a pod that may already be
            # billing — the exact hole the pre-create row exists to close, and
            # reachable from a concurrent `kinoforge sweeper` tick.
            return Verdict.LIVE
        return Verdict.GC_404
    if probe_state == "no_substrate":
        return Verdict.SKIP_NO_PROBE
    if probe_state == "failed":
        return Verdict.PROBE_FAILED

    created_at = float(entry.get("created_at", 0.0))
    age_s = now - created_at
    max_lifetime_s = float(thresholds["max_lifetime_s"])
    if age_s > max_lifetime_s:
        return Verdict.OVERAGE_REAP

    # STALL_REAP is evaluated first because it carries the stronger evidence
    # (N consecutive low-util samples, not one probe) and it is inside
    # DEFAULT_APPLY_POLICY, so it acts without an operator opt-in.
    if _ephemeral_stall_predicate(entry, thresholds, stall_history=stall_history):
        return Verdict.STALL_REAP

    # Spec C1 — the age+util backstop for ``--ephemeral`` pods, which write no
    # ledger row and therefore have no heartbeat to go stale. Opt-in at the
    # policy level (ORPHAN_REAP is not in DEFAULT_APPLY_POLICY), and reachable
    # in one-shot mode too — unlike STALL_REAP it needs no sample history.
    if _ephemeral_orphan_predicate(entry, thresholds, age_s):
        return Verdict.ORPHAN_REAP

    return Verdict.LIVE


def classify(
    entry: Mapping[str, Any],
    live_pod_ids: frozenset[str] | set[str],
    now: float,
    *,
    idle_timeout_s: float,
    max_lifetime_s: float,
    heartbeat_interval_s: float | None,
    grace_after_session_s: float,
    stall_window_s: float | None = None,
    stall_gpu_threshold: float = 5.0,
    stall_cpu_threshold: float = 20.0,
    restart_loop_window_s: float | None = None,
    restart_loop_uptime_threshold_s: float = 90.0,
    stall_history: Mapping[str, deque[tuple[float, float]]] | None = None,
    ephemeral_orphan_age_s: float | None = None,
) -> Verdict:
    """Classify a single ledger entry against the current world state.

    Pure function. No I/O. See spec §3.3 for the row-by-row decision
    tree this implements (rows 1–7).

    Args:
        entry: A ledger-shaped dict. Must carry ``id``. May carry
            per-entry threshold overrides via ``idle_timeout_s`` /
            ``max_lifetime_s`` / ``grace_after_session_s`` keys.
        live_pod_ids: Set of ids the provider currently reports live.
        now: Wall-clock seconds.
        idle_timeout_s: Default idle threshold (cfg-derived).
        max_lifetime_s: Default hard ceiling (cfg-derived).
        heartbeat_interval_s: Cfg heartbeat cadence; ``None`` means the
            heartbeat feature is disabled in this invocation.
        grace_after_session_s: Default post-session warm-reuse window.
        stall_window_s: C26 cfg threshold for util-aware stall reaping.
            ``None`` (default) = kill switch, no STALL_REAP fires. Per-
            entry ``stall_window_s`` key overrides at row 3'.
        stall_gpu_threshold: C26 cfg GPU-util % below which a tick counts
            as 'low'. Carried for HeartbeatLoop ``_update_counter`` and
            unused inside classify itself.
        stall_cpu_threshold: C26 cfg CPU % below which a tick counts as
            'low'. Sister of ``stall_gpu_threshold``.
        restart_loop_window_s: C27 cfg threshold for util-aware restart-
            loop reaping. ``None`` (default) = kill switch, no
            RESTART_LOOP_REAP fires. Per-entry ``restart_loop_window_s``
            key overrides at row 3''.
        restart_loop_uptime_threshold_s: C27 cfg uptime-seconds strict-<
            threshold for ``_update_uptime_counter``. Carried for
            HeartbeatLoop and unused inside classify itself.
        stall_history: Sweeper-side ephemeral reap (spec 2026-06-28).
            Per-pod deque of ``(gpu_util_pct, cpu_pct)`` samples owned by
            ``SweeperLoop``. Only consulted on the ephemeral dispatch
            branch (``entry["kinoforge_ephemeral"] is True``). ``None``
            (default + ``kinoforge reap`` one-shot mode) skips STALL_REAP
            in the ephemeral branch.
        ephemeral_orphan_age_s: Spec C1 age gate for the ``--ephemeral``
            backstop. Only consulted on the ephemeral branch. ``None``
            (the default) is the kill switch — no ORPHAN_REAP can fire
            from an index row. When set, an ephemeral row is reaped only
            when it is strictly older than this AND idle on the current
            probe (below ``stall_gpu_threshold`` and
            ``stall_cpu_threshold``). Age alone never reaps and idleness
            alone never reaps; see :func:`_ephemeral_orphan_predicate`.

    Returns:
        One of the seven non-UNROUTABLE Verdict values:
        LIVE, IDLE_REAP, ORPHAN_REAP, OVERAGE_REAP, STALE_LEDGER,
        HEARTBEAT_UNKNOWN, or HEARTBEAT_SUBSTRATE_MISSING. UNROUTABLE
        is assigned by :func:`kinoforge.core.reaper_actor.sweep` when
        provider lookup fails, never by classify itself. Callers may
        rely on this exclusion when partitioning.
    """
    # Sweeper-side ephemeral reap (spec 2026-06-28): entries synthesised
    # from the EphemeralIndex carry the sentinel ``kinoforge_ephemeral=True``
    # and dispatch to a heartbeat-free decision tree. Ledger-backed entries
    # never carry this flag — the existing decision tree below runs.
    if entry.get("kinoforge_ephemeral") is True:
        return _classify_ephemeral(
            entry,
            {
                "max_lifetime_s": max_lifetime_s,
                "stall_window_s": stall_window_s,
                "stall_gpu_threshold": stall_gpu_threshold,
                "stall_cpu_threshold": stall_cpu_threshold,
                "heartbeat_interval_s": heartbeat_interval_s,
                "ephemeral_orphan_age_s": ephemeral_orphan_age_s,
            },
            now,
            stall_history=stall_history,
        )

    instance_id = str(entry["id"])
    created_at = float(entry.get("created_at", now))
    pod_age = now - created_at
    pod_up = instance_id in live_pod_ids

    # Row 1
    if not pod_up:
        return Verdict.STALE_LEDGER

    # Layer LoRA: a pod self-marked status="degraded" (e.g. via the
    # /lora/set_stack failure paths in Task 8) is reap-eligible even if
    # its heartbeat is fresh and it has not yet hit max_lifetime — the
    # matcher has already routed traffic elsewhere and the operator
    # only owes it a teardown.
    if entry.get("status") == "degraded":
        return Verdict.DEGRADED_REAP

    idle = _resolve(entry, "idle_timeout_s", idle_timeout_s)
    max_age = _resolve(entry, "max_lifetime_s", max_lifetime_s)
    grace = _resolve(entry, "grace_after_session_s", grace_after_session_s)

    # Row 2
    if pod_age > max_age:
        return Verdict.OVERAGE_REAP

    hb_tick = entry.get("heartbeat_thread_tick")
    hb = entry.get("last_heartbeat")

    # Row 7 — heartbeat data unavailable.
    # B5a: gate on provider substrate support. When the entry's provider
    # has no wire-level HeartbeatEndpoint shipped yet (e.g. SkyPilot
    # pre-B5b), emit HEARTBEAT_SUBSTRATE_MISSING so consumers do not
    # treat the absence as actionable. Layer S Ledger.record writes the
    # provider kind under the key ``"provider"`` (lifecycle.py:504);
    # earlier B5a iterations of this gate read ``"provider_kind"`` which
    # the ledger never writes, making the new verdict unreachable on
    # real production entries. Read both for forward compatibility:
    # the canonical key is ``"provider"`` (matches Ledger schema), but
    # test fixtures that pre-date this fix may still pass
    # ``"provider_kind"``. Legacy ledger entries (pre-Layer-S) lack
    # both keys and fall through to HEARTBEAT_UNKNOWN — operator-
    # opted-in dead-man fallback applies.
    if hb_tick is None or hb is None or heartbeat_interval_s is None:
        provider_kind = entry.get("provider_kind") or entry.get("provider")
        if provider_kind is not None and not provider_heartbeat_supported(
            str(provider_kind)
        ):
            return Verdict.HEARTBEAT_SUBSTRATE_MISSING
        return Verdict.HEARTBEAT_UNKNOWN

    sentinel_window = 3.0 * heartbeat_interval_s
    sent_age = now - float(hb_tick)
    hb_age = now - float(hb)

    # Rows 3 & 4 — sentinel fresh
    if sent_age <= sentinel_window:
        if hb_age <= idle:
            # Row 3' (C26): util-aware stall reap interception.
            if _stall_reap_predicate(
                entry,
                now=now,
                sentinel_window=sentinel_window,
                heartbeat_interval_s=heartbeat_interval_s,
                stall_window_s=stall_window_s,
            ):
                return Verdict.STALL_REAP
            # Row 3'' (C27): util-aware restart-loop reap interception.
            # Checked after row 3' so simultaneous fires return STALL_REAP.
            if _restart_loop_reap_predicate(
                entry,
                now=now,
                sentinel_window=sentinel_window,
                heartbeat_interval_s=heartbeat_interval_s,
                restart_loop_window_s=restart_loop_window_s,
            ):
                return Verdict.RESTART_LOOP_REAP
            return Verdict.LIVE
        return Verdict.IDLE_REAP

    # Rows 5 & 6 — sentinel stale. Measure grace from the last driver detach
    # (``session_end``), not pod creation: ``grace_after_session_s`` is named
    # and documented (interfaces.py:65) as the POST-SESSION warm-reuse window.
    # Between CLI invocations the in-process HeartbeatLoop is dead, so the
    # sentinel necessarily goes stale — that is normal, not orphan. As long
    # as the last detach is within grace the pod is still inside the
    # warm-reuse window and must classify LIVE.
    #
    # Fallbacks:
    #   * ``session_end`` absent → never driven yet (or pre-Layer-B3 legacy
    #     entry); fall back to ``pod_age`` so brand-new pods still get the
    #     full grace window from creation and ancient orphan entries still
    #     get reaped.
    #   * ``max(created_at, session_end)`` guards against an out-of-order
    #     write where ``session_end`` somehow predates the pod itself; we
    #     never measure from a marker older than the pod.
    session_end = entry.get("session_end")
    if session_end is None:
        time_since_drive = pod_age
    else:
        drive_marker = max(created_at, float(session_end))
        time_since_drive = now - drive_marker
    if time_since_drive > grace:
        return Verdict.ORPHAN_REAP
    return Verdict.LIVE


def partition(
    verdicts_by_id: Mapping[str, Verdict],
    policy: Policy,
) -> tuple[dict[str, Verdict], dict[str, Verdict]]:
    """Split a verdict snapshot into ``(to_act, to_skip)`` per the policy.

    Pure. Returns fresh dicts; mutating either result does not affect
    the other or the input.

    Args:
        verdicts_by_id: Snapshot from ``sweep`` — one verdict per id.
        policy: Policy whose ``act_verdicts`` selects the actionable set.

    Returns:
        ``(to_act, to_skip)`` — two dicts whose union is the input.
    """
    to_act = {k: v for k, v in verdicts_by_id.items() if v in policy.act_verdicts}
    to_skip = {k: v for k, v in verdicts_by_id.items() if v not in policy.act_verdicts}
    return to_act, to_skip
