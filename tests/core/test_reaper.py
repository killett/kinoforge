"""Pure decision-tree tests for kinoforge.core.reaper.

Covers spec §3.3 verdict tree row-by-row plus the Policy / partition /
_resolve helpers. No I/O. No mocks. Table-driven where possible.
"""

from __future__ import annotations

from typing import Any

from kinoforge.core.reaper import (
    DEFAULT_APPLY_POLICY,
    DEFAULT_STRICT_VERDICTS,
    Policy,
    Verdict,
    _resolve,
    classify,
    partition,
    policy_from_cli_flags,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    *,
    id_: str = "i-1",
    provider: str = "runpod",
    created_at: float = 0.0,
    last_heartbeat: float | None = None,
    heartbeat_thread_tick: float | None = None,
    **overrides: float,
) -> dict[str, object]:
    """Build a ledger-shaped entry for classify tests."""
    e: dict[str, object] = {
        "id": id_,
        "provider": provider,
        "created_at": created_at,
        "cost_rate_usd_per_hr": 0.5,
        "tags": {},
    }
    if last_heartbeat is not None:
        e["last_heartbeat"] = last_heartbeat
    if heartbeat_thread_tick is not None:
        e["heartbeat_thread_tick"] = heartbeat_thread_tick
    e.update(overrides)
    return e


# Sentinel-window math: 3 * heartbeat_interval_s. We use 30s here so the
# stale boundary is t=now-90. Fresh test pins heartbeat_thread_tick=now-1.
_THR: dict[str, Any] = dict(
    idle_timeout_s=100.0,
    max_lifetime_s=10_000.0,
    heartbeat_interval_s=30.0,
    grace_after_session_s=500.0,
)


# ---------------------------------------------------------------------------
# Enum + constants
# ---------------------------------------------------------------------------


def test_verdict_values_are_stable_strings() -> None:
    """Insertion order is part of the public contract (Layer V D-forward-compat)."""
    assert [v.value for v in Verdict] == [
        "LIVE",
        "IDLE_REAP",
        "ORPHAN_REAP",
        "OVERAGE_REAP",
        "STALE_LEDGER",
        "HEARTBEAT_UNKNOWN",
        "HEARTBEAT_SUBSTRATE_MISSING",  # B5a
        "UNROUTABLE",
        "STALL_REAP",  # C26
        "RESTART_LOOP_REAP",  # C27
    ]


def test_default_apply_policy_contains_high_confidence_verdicts() -> None:
    """ORPHAN_REAP is NOT in the default — requires --include-orphans.

    STALL_REAP added C26 (steady-low-util predicate); RESTART_LOOP_REAP
    added C27 (chronic low-uptime predicate) — both high-confidence
    enough to act on by default.
    """
    assert DEFAULT_APPLY_POLICY.act_verdicts == frozenset(
        {
            Verdict.IDLE_REAP,
            Verdict.OVERAGE_REAP,
            Verdict.STALE_LEDGER,
            Verdict.STALL_REAP,  # C26
            Verdict.RESTART_LOOP_REAP,  # C27
        }
    )


def test_default_strict_verdicts_are_uncertain_only() -> None:
    """--strict trips on verdicts that mean 'I don't know enough to decide'."""
    assert DEFAULT_STRICT_VERDICTS == frozenset(
        {
            Verdict.UNROUTABLE,
            Verdict.HEARTBEAT_UNKNOWN,
            Verdict.HEARTBEAT_SUBSTRATE_MISSING,  # B5a
        }
    )


def test_restart_loop_reap_verdict_exists_after_stall_reap() -> None:
    """C27: RESTART_LOOP_REAP appended after STALL_REAP, honouring insertion order."""
    assert Verdict.RESTART_LOOP_REAP.value == "RESTART_LOOP_REAP"
    members = list(Verdict)
    assert members.index(Verdict.RESTART_LOOP_REAP) > members.index(Verdict.STALL_REAP)


def test_default_apply_policy_contains_restart_loop_reap() -> None:
    """C27: DEFAULT_APPLY_POLICY acts on RESTART_LOOP_REAP."""
    assert Verdict.RESTART_LOOP_REAP in DEFAULT_APPLY_POLICY.act_verdicts


# ---------------------------------------------------------------------------
# policy_from_cli_flags
# ---------------------------------------------------------------------------


def test_policy_from_flags_dry_run_is_empty_set() -> None:
    """apply=False → empty act-set even with all opt-ins."""
    p = policy_from_cli_flags(apply=False, include_orphans=True, force_forget=True)
    assert p.act_verdicts == frozenset()


def test_policy_from_flags_apply_defaults() -> None:
    """apply=True with no opt-ins → DEFAULT_APPLY_POLICY exactly."""
    p = policy_from_cli_flags(apply=True)
    assert p.act_verdicts == DEFAULT_APPLY_POLICY.act_verdicts


def test_policy_from_flags_apply_include_orphans_adds_orphan_reap() -> None:
    p = policy_from_cli_flags(apply=True, include_orphans=True)
    assert p.act_verdicts == frozenset(
        {
            Verdict.IDLE_REAP,
            Verdict.OVERAGE_REAP,
            Verdict.STALE_LEDGER,
            Verdict.STALL_REAP,  # C26
            Verdict.RESTART_LOOP_REAP,  # C27
            Verdict.ORPHAN_REAP,
        }
    )


def test_policy_from_flags_apply_force_forget_adds_unroutable() -> None:
    p = policy_from_cli_flags(apply=True, force_forget=True)
    assert p.act_verdicts == frozenset(
        {
            Verdict.IDLE_REAP,
            Verdict.OVERAGE_REAP,
            Verdict.STALE_LEDGER,
            Verdict.STALL_REAP,  # C26
            Verdict.RESTART_LOOP_REAP,  # C27
            Verdict.UNROUTABLE,
        }
    )


def test_policy_from_flags_apply_all_flags_returns_seven_element_set() -> None:
    """`kinoforge reap --apply --include-orphans --force-forget` policy contract.

    Combined-flag invocation must produce DEFAULT_APPLY_POLICY ∪
    {ORPHAN_REAP, UNROUTABLE} — seven verdicts after C26 added STALL_REAP
    and C27 added RESTART_LOOP_REAP to the default. Regression guard: a
    future merge that accidentally drops a base verdict (e.g. STALE_LEDGER)
    under an opt-in would be invisible to single-flag tests; this catches it.
    """
    p = policy_from_cli_flags(apply=True, include_orphans=True, force_forget=True)
    assert p.act_verdicts == frozenset(
        {
            Verdict.IDLE_REAP,
            Verdict.OVERAGE_REAP,
            Verdict.STALE_LEDGER,
            Verdict.STALL_REAP,  # C26
            Verdict.RESTART_LOOP_REAP,  # C27
            Verdict.ORPHAN_REAP,
            Verdict.UNROUTABLE,
        }
    )


# ---------------------------------------------------------------------------
# _resolve helper
# ---------------------------------------------------------------------------


def test_resolve_returns_entry_value_when_present() -> None:
    assert _resolve({"idle_timeout_s": 42}, "idle_timeout_s", 9999.0) == 42.0


def test_resolve_falls_back_to_default_on_missing() -> None:
    assert _resolve({}, "idle_timeout_s", 7200.0) == 7200.0


def test_resolve_falls_back_on_none_value() -> None:
    assert _resolve({"idle_timeout_s": None}, "idle_timeout_s", 7200.0) == 7200.0


def test_resolve_falls_back_on_non_numeric() -> None:
    """Bad-type fallback is defensive — ledger corruption must not crash classify."""
    assert _resolve({"idle_timeout_s": "bogus"}, "idle_timeout_s", 7200.0) == 7200.0


# ---------------------------------------------------------------------------
# classify — row 1: STALE_LEDGER
# ---------------------------------------------------------------------------


def test_classify_returns_stale_ledger_when_pod_not_in_live_ids() -> None:
    """Row 1: provider is authoritative on existence."""
    e = _entry(id_="i-gone")
    assert classify(e, set(), now=100.0, **_THR) == Verdict.STALE_LEDGER


def test_classify_stale_ledger_takes_precedence_over_heartbeat() -> None:
    """Row 1 fires before heartbeat reasoning — pod_up=False is decisive."""
    e = _entry(id_="i-1", last_heartbeat=99.0, heartbeat_thread_tick=99.0)
    assert classify(e, set(), now=100.0, **_THR) == Verdict.STALE_LEDGER


# ---------------------------------------------------------------------------
# classify — row 2: OVERAGE_REAP
# ---------------------------------------------------------------------------


def test_classify_overage_reap_when_pod_age_exceeds_max_lifetime() -> None:
    """Row 2: hard ceiling fires regardless of heartbeat freshness."""
    e = _entry(created_at=0.0, last_heartbeat=20_000.0, heartbeat_thread_tick=20_000.0)
    # max_lifetime_s=10_000; now=20_000 → pod_age=20_000 > 10_000
    assert classify(e, {"i-1"}, now=20_000.0, **_THR) == Verdict.OVERAGE_REAP


# ---------------------------------------------------------------------------
# classify — row 3: LIVE (sentinel-fresh + hb fresh)
# ---------------------------------------------------------------------------


def test_classify_live_when_sentinel_fresh_and_hb_fresh() -> None:
    """Row 3: sentinel-fresh + hb_age <= idle_timeout_s → LIVE."""
    e = _entry(created_at=0.0, last_heartbeat=95.0, heartbeat_thread_tick=99.0)
    # hb_age = 5, sent_age = 1, idle=100, sentinel_window=90 → LIVE
    assert classify(e, {"i-1"}, now=100.0, **_THR) == Verdict.LIVE


# ---------------------------------------------------------------------------
# classify — row 4: IDLE_REAP (sentinel-fresh + hb stale)
# ---------------------------------------------------------------------------


def test_classify_idle_reap_when_sentinel_fresh_but_hb_stale() -> None:
    """Row 4: sentinel-fresh + hb_age > idle_timeout_s → IDLE_REAP."""
    e = _entry(created_at=0.0, last_heartbeat=900.0, heartbeat_thread_tick=1_499.0)
    # now=1_500; hb_age=600 > idle=100; sent_age=1 < 90 → IDLE_REAP
    assert classify(e, {"i-1"}, now=1_500.0, **_THR) == Verdict.IDLE_REAP


# ---------------------------------------------------------------------------
# classify — row 5: ORPHAN_REAP (sentinel-stale + past grace)
# ---------------------------------------------------------------------------


def test_classify_orphan_reap_when_sentinel_stale_and_past_grace() -> None:
    """Row 5: sentinel-stale + pod_age > grace_after_session_s → ORPHAN_REAP."""
    e = _entry(created_at=0.0, last_heartbeat=400.0, heartbeat_thread_tick=400.0)
    # now=1_000; sent_age=600 > 90; pod_age=1_000 > grace=500 → ORPHAN_REAP
    assert classify(e, {"i-1"}, now=1_000.0, **_THR) == Verdict.ORPHAN_REAP


# ---------------------------------------------------------------------------
# classify — row 6: LIVE (sentinel-stale but within grace)
# ---------------------------------------------------------------------------


def test_classify_live_when_sentinel_stale_but_within_grace() -> None:
    """Row 6: brand-new pod, first tick hasn't fired or just lost session.

    pod_age <= grace_after_session_s → grace honored, LIVE.
    """
    e = _entry(created_at=200.0, last_heartbeat=210.0, heartbeat_thread_tick=210.0)
    # now=500; sent_age=290 > 90 (stale); pod_age=300 <= grace=500 → LIVE
    assert classify(e, {"i-1"}, now=500.0, **_THR) == Verdict.LIVE


# ---------------------------------------------------------------------------
# classify — row 7: HEARTBEAT_UNKNOWN
# ---------------------------------------------------------------------------


def test_classify_heartbeat_unknown_when_sentinel_field_absent() -> None:
    """Row 7: legacy entry with no Layer U fields → HEARTBEAT_UNKNOWN."""
    e = _entry(created_at=0.0)  # no hb / hb_tick
    assert classify(e, {"i-1"}, now=500.0, **_THR) == Verdict.HEARTBEAT_UNKNOWN


def test_classify_heartbeat_unknown_when_hb_present_but_tick_absent() -> None:
    """Layer U writes both fields atomically; missing tick = older writer."""
    e = _entry(created_at=0.0, last_heartbeat=400.0)
    assert classify(e, {"i-1"}, now=500.0, **_THR) == Verdict.HEARTBEAT_UNKNOWN


def test_classify_heartbeat_unknown_when_cfg_interval_is_none() -> None:
    """Heartbeat disabled in cfg → cannot reason about freshness."""
    e = _entry(created_at=0.0, last_heartbeat=400.0, heartbeat_thread_tick=499.0)
    thresholds = dict(_THR, heartbeat_interval_s=None)
    assert classify(e, {"i-1"}, now=500.0, **thresholds) == Verdict.HEARTBEAT_UNKNOWN


# ---------------------------------------------------------------------------
# classify — per-entry threshold overrides (_resolve usage)
# ---------------------------------------------------------------------------


def test_classify_per_entry_idle_timeout_override_beats_cfg_default() -> None:
    """Layer S precedent: per-entry threshold beats cfg.

    cfg idle=100; entry idle=1_000 → hb_age=600 < 1_000 → LIVE not IDLE_REAP.
    """
    e = _entry(
        created_at=0.0,
        last_heartbeat=900.0,
        heartbeat_thread_tick=1_499.0,
        idle_timeout_s=1_000.0,
    )
    assert classify(e, {"i-1"}, now=1_500.0, **_THR) == Verdict.LIVE


def test_classify_per_entry_grace_override_beats_cfg_default() -> None:
    """Entry-specified grace_after_session_s overrides cfg."""
    e = _entry(
        created_at=0.0,
        last_heartbeat=400.0,
        heartbeat_thread_tick=400.0,
        grace_after_session_s=2_000.0,
    )
    # pod_age=1_000, grace=2_000 → within grace → LIVE
    assert classify(e, {"i-1"}, now=1_000.0, **_THR) == Verdict.LIVE


# ---------------------------------------------------------------------------
# classify — boundary tests
# ---------------------------------------------------------------------------


def test_classify_sentinel_at_exact_window_boundary_is_fresh() -> None:
    """sent_age == sentinel_window is fresh (`<=` not `<` per spec §3.3)."""
    # sentinel_window = 3*30 = 90; sent_age must be EXACTLY 90.
    e = _entry(created_at=0.0, last_heartbeat=10.0, heartbeat_thread_tick=10.0)
    # now=100; sent_age=90 (boundary); hb_age=90 > idle=100? hb_age=90 <= 100 → LIVE
    assert classify(e, {"i-1"}, now=100.0, **_THR) == Verdict.LIVE


def test_classify_pod_age_at_exact_max_lifetime_is_not_overage() -> None:
    """OVERAGE rule is `>`, not `>=` (spec §3.3 row 2)."""
    e = _entry(created_at=0.0, last_heartbeat=10_000.0, heartbeat_thread_tick=10_000.0)
    # pod_age == max_lifetime → NOT overage; hb_age=0 → LIVE
    assert classify(e, {"i-1"}, now=10_000.0, **_THR) == Verdict.LIVE


def test_classify_grace_at_exact_boundary_is_within_grace() -> None:
    """Grace rule is `<=`, not `<` (spec §3.3 row 6 vs row 5)."""
    e = _entry(created_at=0.0, last_heartbeat=10.0, heartbeat_thread_tick=10.0)
    # now=500; sent_age=490 > 90 (stale); pod_age=500 <= grace=500 → LIVE
    assert classify(e, {"i-1"}, now=500.0, **_THR) == Verdict.LIVE


# ---------------------------------------------------------------------------
# partition
# ---------------------------------------------------------------------------


def test_partition_splits_by_policy_act_verdicts() -> None:
    verdicts = {"a": Verdict.IDLE_REAP, "b": Verdict.LIVE, "c": Verdict.STALE_LEDGER}
    policy = Policy(act_verdicts=frozenset({Verdict.IDLE_REAP}))
    to_act, to_skip = partition(verdicts, policy)
    assert to_act == {"a": Verdict.IDLE_REAP}
    assert to_skip == {"b": Verdict.LIVE, "c": Verdict.STALE_LEDGER}


def test_partition_empty_policy_skips_everything() -> None:
    """Dry-run = empty policy = nothing acted on."""
    verdicts = {"a": Verdict.IDLE_REAP, "b": Verdict.OVERAGE_REAP}
    to_act, to_skip = partition(verdicts, Policy(act_verdicts=frozenset()))
    assert to_act == {}
    assert to_skip == verdicts


def test_partition_returns_independent_dicts() -> None:
    """Mutating either result must not affect the other."""
    verdicts = {"a": Verdict.IDLE_REAP, "b": Verdict.LIVE}
    to_act, to_skip = partition(
        verdicts, Policy(act_verdicts=frozenset({Verdict.IDLE_REAP}))
    )
    to_act["a"] = Verdict.OVERAGE_REAP
    assert to_skip == {"b": Verdict.LIVE}


# ---------------------------------------------------------------------------
# B5a Task d — HEARTBEAT_SUBSTRATE_MISSING verdict + classify gate
# ---------------------------------------------------------------------------


def test_classify_emits_substrate_missing_on_unsupported_provider() -> None:
    """SkyPilot pre-B5b: provider_kind='skypilot', last_heartbeat=None.
    Must NOT emit HEARTBEAT_UNKNOWN — that would let a future B1 sweeper
    reap a live working SkyPilot pod once HEARTBEAT_UNKNOWN is added to
    the apply policy. Emit the dedicated verdict instead."""
    entry = {
        "id": "cluster-x",
        "provider_kind": "skypilot",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    v = classify(
        entry,
        live_pod_ids={"cluster-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_classify_emits_heartbeat_unknown_on_supported_provider_with_no_data() -> None:
    """RunPod with compute.heartbeat_mode='none' (operator opted out):
    provider_kind='runpod', last_heartbeat=None. Operator made the
    choice — sweeper's dead-man fallback (IDLE_REAP after dead-man
    window) is the next layer of defence. Keep HEARTBEAT_UNKNOWN."""
    entry = {
        "id": "pod-x",
        "provider_kind": "runpod",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    v = classify(
        entry,
        live_pod_ids={"pod-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.HEARTBEAT_UNKNOWN


def test_classify_treats_missing_provider_kind_as_unknown() -> None:
    """Legacy ledger entries pre-Layer-S persistence may lack provider_kind.
    Defensive: do NOT emit HEARTBEAT_SUBSTRATE_MISSING on legacy entries —
    that would block operator-driven reaps of orphaned legacy pods."""
    entry = {
        "id": "legacy-x",
        # provider_kind AND provider both absent — fully legacy entry
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    v = classify(
        entry,
        live_pod_ids={"legacy-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.HEARTBEAT_UNKNOWN


def test_classify_reads_provider_key_when_provider_kind_absent() -> None:
    """Production-shaped ledger entries written by Layer S Ledger.record
    carry the provider kind under the key ``"provider"`` (NOT
    ``"provider_kind"`` — see lifecycle.py:504).  Classify must read the
    same key the ledger writes, otherwise HEARTBEAT_SUBSTRATE_MISSING is
    unreachable on every real-world ledger entry and the new verdict
    only fires for hand-crafted dict literals in tests.

    Bug catch: an earlier B5a iteration read only ``"provider_kind"`` and
    silently fell through to HEARTBEAT_UNKNOWN on every production entry.
    Caught by the final cross-task review (2026-06-12)."""
    entry = {
        "id": "cluster-x",
        "provider": "skypilot",  # the actual Ledger.record schema key
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    v = classify(
        entry,
        live_pod_ids={"cluster-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_classify_provider_kind_takes_precedence_over_provider() -> None:
    """When both keys are present (transitional ledger state),
    provider_kind wins.  This is paranoia — Ledger.record writes only
    ``"provider"`` today — but the fallback expression
    ``entry.get("provider_kind") or entry.get("provider")`` evaluates
    provider_kind first, which means a future Ledger schema migration
    that adds ``"provider_kind"`` alongside ``"provider"`` does not
    silently change verdicts when the two disagree."""
    entry = {
        "id": "pod-x",
        "provider_kind": "skypilot",  # would emit SUBSTRATE_MISSING
        "provider": "runpod",  # would emit UNKNOWN
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    v = classify(
        entry,
        live_pod_ids={"pod-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_classify_emits_live_with_fresh_heartbeat_on_runpod() -> None:
    """Smoke: substrate working end-to-end. provider_kind='runpod',
    fresh sentinel + fresh heartbeat → LIVE (the path the operator
    actually wants)."""
    entry = {
        "id": "pod-x",
        "provider_kind": "runpod",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": 1_990.0,  # 10s old → fresh under 90s window
        "last_heartbeat": 1_990.0,
    }
    v = classify(
        entry,
        live_pod_ids={"pod-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.LIVE


def test_default_policy_does_not_act_on_substrate_missing() -> None:
    """B1 sweeper inherits this Policy. Sweeper must NEVER reap on
    HEARTBEAT_SUBSTRATE_MISSING — operator cannot fix the substrate
    by destroying the pod."""
    assert Verdict.HEARTBEAT_SUBSTRATE_MISSING not in DEFAULT_APPLY_POLICY.act_verdicts
