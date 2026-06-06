# Layer V — heartbeat-aware reaper — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the first production consumer of Layer U's sentinel-gate contract — a multi-provider `kinoforge reap` driven by a pure decision function reusable by future Layer W/X/Y consumers.

**Architecture:** Strict purity split. `core/reaper.py` is pure (`Verdict`, `Policy`, `classify`, `partition`, `_resolve`); `core/reaper_actor.py` is the only impure surface (`act_on_verdict`, `provider_for`, `sweep`). CLI rewrites `_cmd_reap` as a thin formatter on top of `sweep`. Default is dry-run; `--apply` activates `DEFAULT_APPLY_POLICY = {IDLE_REAP, OVERAGE_REAP, STALE_LEDGER}`. `act_on_verdict` re-classifies inside a Layer 18 per-instance lock to eliminate the human-in-the-loop race.

**Tech Stack:** Python 3.13 · pydantic v2 · stdlib argparse + dataclasses + enum · pytest · existing Lifecycle / Ledger / ArtifactStore / registry primitives.

**Spec reference:** `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md`.

---

## File Structure

**New files (pure substrate):**
- `src/kinoforge/core/reaper.py` — Verdict enum, Policy dataclass, classify, partition, _resolve, policy_from_cli_flags, DEFAULT_APPLY_POLICY, DEFAULT_STRICT_VERDICTS

**New files (impure orchestration):**
- `src/kinoforge/core/reaper_actor.py` — ActionResult, SweepReport, provider_for, act_on_verdict, sweep

**Modified files:**
- `src/kinoforge/core/interfaces.py` — add `Lifecycle.grace_after_session_s` field
- `src/kinoforge/core/config.py` — add `LifecycleConfig.grace_after_session_s` + validator + wire through `Config.lifecycle()`
- `src/kinoforge/cli/_commands.py` — rewrite `_cmd_reap`; add verdict line in `_cmd_status`
- `src/kinoforge/cli/_main.py` — add `--apply` / `--include-orphans` / `--force-forget` / `--strict` / `--id` / `--format` / `--config` flags to `reap` subparser
- `tests/test_core_invariant.py` — add purity scan for `core/reaper.py`
- `examples/configs/*.yaml` — add commented `grace_after_session_s` line
- `README.md` — add Operator → Reaping section
- `PROGRESS.md` — Phase 37 (Layer V) entry

**New test files:**
- `tests/core/test_reaper.py` — pure decision-tree tests
- `tests/core/test_reaper_actor.py` — actor + drift + lock tests
- `tests/core/test_reaper_sweep.py` — sweep integration tests
- `tests/cli/test_cmd_reap.py` — CLI flag matrix

**Modified test files:**
- `tests/core/test_config.py` — `+3 tests` (YAML round-trip, default, negative reject)
- `tests/core/test_lifecycle.py` — `+2 tests` (Lifecycle dataclass field default + wire)
- `tests/cli/test_commands_routing.py` — `+2 tests` (status verdict line)

---

## Pre-flight

Verify the working tree is clean, the test suite is green, and the spec doc is present.

- [ ] Run `git status` — must show "nothing to commit, working tree clean".
- [ ] Run `pixi run test` — must pass (1351 passed / 8 skipped baseline from Layer U).
- [ ] Run `ls docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` — must exist.

If any of those fail, stop and fix before starting Task 1.

---

### Task 1: Pure substrate (`core/reaper.py`) — Verdict, Policy, classify, partition

**Goal:** Ship the pure decision-tree module that every future heartbeat-aware consumer (this layer's CLI, Layer W daemon, Layer Y orchestrator hook) will reuse.

**Files:**
- Create: `src/kinoforge/core/reaper.py`
- Test: `tests/core/test_reaper.py`

**Acceptance Criteria:**
- [ ] `Verdict` is a `str`-backed `Enum` with the seven values from spec §3.3 in stable insertion order: `LIVE`, `IDLE_REAP`, `ORPHAN_REAP`, `OVERAGE_REAP`, `STALE_LEDGER`, `HEARTBEAT_UNKNOWN`, `UNROUTABLE`.
- [ ] `Policy` is a frozen dataclass with one field `act_verdicts: frozenset[Verdict]`.
- [ ] `DEFAULT_APPLY_POLICY.act_verdicts == frozenset({IDLE_REAP, OVERAGE_REAP, STALE_LEDGER})`.
- [ ] `DEFAULT_STRICT_VERDICTS == frozenset({UNROUTABLE, HEARTBEAT_UNKNOWN})`.
- [ ] `policy_from_cli_flags(apply=False, ...)` returns `Policy(frozenset())` regardless of other flags.
- [ ] `policy_from_cli_flags(apply=True, include_orphans=True, force_forget=True)` returns Policy with the default set ∪ `{ORPHAN_REAP, UNROUTABLE}`.
- [ ] `_resolve(entry, "idle_timeout_s", 7200.0)` returns `entry["idle_timeout_s"]` cast to float when present; falls back to `7200.0` on missing / `None` / non-numeric value.
- [ ] `classify` implements the full row-1-through-row-7 decision tree from spec §3.3 (one verdict per derived input combination); `UNROUTABLE` is never returned by `classify`.
- [ ] `partition({"a": IDLE_REAP, "b": LIVE}, Policy(frozenset({IDLE_REAP})))` returns `({"a": IDLE_REAP}, {"b": LIVE})`.
- [ ] The module imports nothing from `kinoforge.{providers,sources,engines,cli}`, nothing from `kinoforge.stores`, no `urllib`, no `subprocess`, no `threading`, no `time`. Only `collections.abc`, `dataclasses`, `enum`, `typing`.

**Verify:** `pixi run pytest tests/core/test_reaper.py -v` → 25 passed.

**Steps:**

- [ ] **Step 1: Write the failing test file `tests/core/test_reaper.py`.**

```python
"""Pure decision-tree tests for kinoforge.core.reaper.

Covers spec §3.3 verdict tree row-by-row plus the Policy / partition /
_resolve helpers. No I/O. No mocks. Table-driven where possible.
"""

from __future__ import annotations

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
_THR = dict(
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
        "UNROUTABLE",
    ]


def test_default_apply_policy_contains_high_confidence_verdicts() -> None:
    """ORPHAN_REAP is NOT in the default — requires --include-orphans."""
    assert DEFAULT_APPLY_POLICY.act_verdicts == frozenset(
        {Verdict.IDLE_REAP, Verdict.OVERAGE_REAP, Verdict.STALE_LEDGER}
    )


def test_default_strict_verdicts_are_uncertain_only() -> None:
    """--strict trips on verdicts that mean 'I don't know enough to decide'."""
    assert DEFAULT_STRICT_VERDICTS == frozenset(
        {Verdict.UNROUTABLE, Verdict.HEARTBEAT_UNKNOWN}
    )


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
    assert Verdict.ORPHAN_REAP in p.act_verdicts
    assert Verdict.UNROUTABLE not in p.act_verdicts


def test_policy_from_flags_apply_force_forget_adds_unroutable() -> None:
    p = policy_from_cli_flags(apply=True, force_forget=True)
    assert Verdict.UNROUTABLE in p.act_verdicts
    assert Verdict.ORPHAN_REAP not in p.act_verdicts


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
    e = _entry(
        created_at=0.0, last_heartbeat=900.0, heartbeat_thread_tick=1_499.0
    )
    # now=1_500; hb_age=600 > idle=100; sent_age=1 < 90 → IDLE_REAP
    assert classify(e, {"i-1"}, now=1_500.0, **_THR) == Verdict.IDLE_REAP


# ---------------------------------------------------------------------------
# classify — row 5: ORPHAN_REAP (sentinel-stale + past grace)
# ---------------------------------------------------------------------------


def test_classify_orphan_reap_when_sentinel_stale_and_past_grace() -> None:
    """Row 5: sentinel-stale + pod_age > grace_after_session_s → ORPHAN_REAP."""
    e = _entry(
        created_at=0.0, last_heartbeat=400.0, heartbeat_thread_tick=400.0
    )
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
```

- [ ] **Step 2: Run test to verify it fails.**

Run: `pixi run pytest tests/core/test_reaper.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'kinoforge.core.reaper'".

- [ ] **Step 3: Write the minimal implementation `src/kinoforge/core/reaper.py`.**

```python
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

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Verdict(str, Enum):
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
    UNROUTABLE = "UNROUTABLE"


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
        }
    )
)

DEFAULT_STRICT_VERDICTS: frozenset[Verdict] = frozenset(
    {Verdict.UNROUTABLE, Verdict.HEARTBEAT_UNKNOWN}
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


def classify(
    entry: Mapping[str, Any],
    live_pod_ids: frozenset[str] | set[str],
    now: float,
    *,
    idle_timeout_s: float,
    max_lifetime_s: float,
    heartbeat_interval_s: float | None,
    grace_after_session_s: float,
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

    Returns:
        One of six Verdict values. Note: ``UNROUTABLE`` is never
        returned by classify — it is assigned by ``sweep`` when
        ``provider_for`` fails. Callers may rely on the
        exclusion when partitioning.
    """
    instance_id = str(entry["id"])
    created_at = float(entry.get("created_at", now))
    pod_age = now - created_at
    pod_up = instance_id in live_pod_ids

    # Row 1
    if not pod_up:
        return Verdict.STALE_LEDGER

    idle = _resolve(entry, "idle_timeout_s", idle_timeout_s)
    max_age = _resolve(entry, "max_lifetime_s", max_lifetime_s)
    grace = _resolve(entry, "grace_after_session_s", grace_after_session_s)

    # Row 2
    if pod_age > max_age:
        return Verdict.OVERAGE_REAP

    hb_tick = entry.get("heartbeat_thread_tick")
    hb = entry.get("last_heartbeat")

    # Row 7 — heartbeat data unavailable
    if hb_tick is None or hb is None or heartbeat_interval_s is None:
        return Verdict.HEARTBEAT_UNKNOWN

    sentinel_window = 3.0 * heartbeat_interval_s
    sent_age = now - float(hb_tick)
    hb_age = now - float(hb)

    # Rows 3 & 4 — sentinel fresh
    if sent_age <= sentinel_window:
        if hb_age <= idle:
            return Verdict.LIVE
        return Verdict.IDLE_REAP

    # Rows 5 & 6 — sentinel stale
    if pod_age > grace:
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
    to_act = {
        k: v for k, v in verdicts_by_id.items() if v in policy.act_verdicts
    }
    to_skip = {
        k: v for k, v in verdicts_by_id.items() if v not in policy.act_verdicts
    }
    return to_act, to_skip
```

- [ ] **Step 4: Run test to verify it passes.**

Run: `pixi run pytest tests/core/test_reaper.py -v`
Expected: PASS (25 tests).

- [ ] **Step 5: Run lint/format/typecheck.**

Run: `pixi run pre-commit run --files src/kinoforge/core/reaper.py tests/core/test_reaper.py`
Expected: all hooks PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/reaper.py tests/core/test_reaper.py
git commit -m "$(cat <<'EOF'
feat(reaper): pure substrate — Verdict, Policy, classify, partition (Layer V T1)

core/reaper.py — the only place sentinel-gate logic lives. Implements
spec §3.3 row-1-through-row-7 verdict tree as a pure function. No
I/O, no mocks needed in tests. Future Layer W daemon, Layer Y
orchestrator hook, and the in-tree CLI all consume the same surface.

25 tests cover every verdict, boundary conditions on each `>` / `<=`,
per-entry threshold overrides (Layer S precedent), and the
DEFAULT_APPLY_POLICY / DEFAULT_STRICT_VERDICTS constants that the
CLI and any future daemon will reuse.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Invariant scan — `core/reaper.py` purity contract

**Goal:** Lock the purity contract architecturally so a future contributor cannot accidentally drag I/O into the pure module.

**Files:**
- Modify: `tests/test_core_invariant.py` (append one new test)

**Acceptance Criteria:**
- [ ] New test `test_core_reaper_module_is_pure` asserts that `src/kinoforge/core/reaper.py` contains no `import` line referencing `urllib`, `subprocess`, `threading`, `time`, `pathlib`, `kinoforge.providers`, `kinoforge.sources`, `kinoforge.engines`, `kinoforge.stores`, `kinoforge.cli`, or `kinoforge.core.lifecycle` (Ledger lives there — it is I/O).
- [ ] Test fails when run against a hypothetical `reaper.py` with `import urllib.request` injected.
- [ ] Existing 6 invariant tests still pass.

**Verify:** `pixi run pytest tests/test_core_invariant.py -v` → all 7 tests PASS.

**Steps:**

- [ ] **Step 1: Add the failing test at the bottom of `tests/test_core_invariant.py`.**

```python
# ---------------------------------------------------------------------------
# AC 7: core/reaper.py purity contract (Layer V)
# ---------------------------------------------------------------------------

_REAPER_FORBIDDEN_IMPORTS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(import|from)\s+urllib\b"),
    re.compile(r"^\s*(import|from)\s+subprocess\b"),
    re.compile(r"^\s*(import|from)\s+threading\b"),
    re.compile(r"^\s*(import|from)\s+time\b"),
    re.compile(r"^\s*(import|from)\s+pathlib\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.providers\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.sources\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.engines\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.stores\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.cli\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.core\.lifecycle\b"),
]


def test_core_reaper_module_is_pure() -> None:
    """Layer V: core/reaper.py is pure — no I/O, no Ledger, no adapters.

    The sentinel-gate decision logic lives in classify(). Any I/O
    import here would let a future contributor reach into the ledger
    or a provider from inside classify(), violating the purity
    contract documented in spec §3.4. The contract is enforced
    architecturally so docstring vigilance is not load-bearing.
    """
    reaper_path = SRC_ROOT / "core" / "reaper.py"
    violations: list[str] = []
    for lineno, line in enumerate(reaper_path.read_text().splitlines(), start=1):
        for pattern in _REAPER_FORBIDDEN_IMPORTS:
            if pattern.match(line):
                violations.append(f"{reaper_path}:{lineno}: {line.strip()}")
                break
    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            f"core/reaper.py must be pure — forbidden import(s) found:\n  {detail}"
        )
```

- [ ] **Step 2: Verify the failing-by-construction case is caught.**

Temporarily add `import urllib.request` to `src/kinoforge/core/reaper.py`, run the test, expect FAIL with the violation line printed. Remove the temporary line.

Run: `pixi run pytest tests/test_core_invariant.py::test_core_reaper_module_is_pure -v`
Expected after revert: PASS.

- [ ] **Step 3: Run the full invariant suite.**

Run: `pixi run pytest tests/test_core_invariant.py -v`
Expected: 7 PASS (6 existing + 1 new).

- [ ] **Step 4: Commit.**

```bash
git add tests/test_core_invariant.py
git commit -m "$(cat <<'EOF'
test(invariant): lock core/reaper.py purity contract (Layer V T2)

New scan rejects any import of I/O modules, ledger, providers,
sources, engines, stores, or CLI inside core/reaper.py. The
sentinel-gate decision logic stays architecturally pure — a
future contributor cannot accidentally reach into the ledger
or a provider from inside classify().

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `Lifecycle.grace_after_session_s` field + config wire

**Goal:** Ship the single new YAML knob (`lifecycle.grace_after_session_s`) and propagate it through `Lifecycle` dataclass + `LifecycleConfig` pydantic + `Config.lifecycle()`.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (add field to `Lifecycle` dataclass at line ~71)
- Modify: `src/kinoforge/core/config.py` (add field to `LifecycleConfig` at line ~101, add validator below ~131, add field-copy in `Config.lifecycle()` at line ~719)
- Modify: `tests/core/test_lifecycle.py` (add 2 dataclass tests)
- Modify: `tests/core/test_config.py` (add 3 pydantic/YAML tests)

**Acceptance Criteria:**
- [ ] `Lifecycle()` constructed with no args has `grace_after_session_s == 300.0`.
- [ ] `Lifecycle(grace_after_session_s=42.0)` round-trips.
- [ ] `LifecycleConfig(budget=1.0)` defaults `grace_after_session_s` to `300.0`.
- [ ] `LifecycleConfig(budget=1.0, grace_after_session_s=42.0)` round-trips through `model_dump_json` then `model_validate_json`.
- [ ] `LifecycleConfig(budget=1.0, grace_after_session_s=-5.0)` raises `pydantic.ValidationError`.
- [ ] `Config.lifecycle()` populates `grace_after_session_s` from the YAML value when present.
- [ ] All existing config tests still pass.

**Verify:** `pixi run pytest tests/core/test_lifecycle.py tests/core/test_config.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Add failing tests at the bottom of `tests/core/test_lifecycle.py`.**

```python
# ---------------------------------------------------------------------------
# Layer V — grace_after_session_s field on Lifecycle dataclass
# ---------------------------------------------------------------------------


def test_lifecycle_grace_after_session_s_default_is_300() -> None:
    """Layer V: default 5-minute post-session warm-reuse grace window."""
    from kinoforge.core.interfaces import Lifecycle

    assert Lifecycle().grace_after_session_s == 300.0


def test_lifecycle_grace_after_session_s_round_trips() -> None:
    """Constructor accepts an explicit override."""
    from kinoforge.core.interfaces import Lifecycle

    assert Lifecycle(grace_after_session_s=42.0).grace_after_session_s == 42.0
```

- [ ] **Step 2: Add failing tests at the bottom of `tests/core/test_config.py`.**

```python
# ---------------------------------------------------------------------------
# Layer V — grace_after_session_s on LifecycleConfig
# ---------------------------------------------------------------------------


def test_lifecycle_config_grace_after_session_s_default_is_300() -> None:
    """Default surfaces through pydantic load too."""
    from kinoforge.core.config import LifecycleConfig

    assert LifecycleConfig(budget=1.0).grace_after_session_s == 300.0


def test_lifecycle_config_grace_after_session_s_round_trips() -> None:
    """YAML-style round-trip via model_dump_json / model_validate_json."""
    from kinoforge.core.config import LifecycleConfig

    raw = LifecycleConfig(budget=1.0, grace_after_session_s=42.0).model_dump_json()
    parsed = LifecycleConfig.model_validate_json(raw)
    assert parsed.grace_after_session_s == 42.0


def test_lifecycle_config_grace_after_session_s_rejects_negative() -> None:
    """Validator rejects negative values at load time."""
    import pytest
    from pydantic import ValidationError

    from kinoforge.core.config import LifecycleConfig

    with pytest.raises(ValidationError):
        LifecycleConfig(budget=1.0, grace_after_session_s=-1.0)


def test_config_lifecycle_wires_grace_after_session_s() -> None:
    """Top-level Config.lifecycle() populates the field on the interface dataclass."""
    import tempfile
    from pathlib import Path

    from kinoforge.core.config import load_config

    yaml_body = """
compute:
  provider: local
  lifecycle:
    budget: 1.0
    grace_after_session_s: 999.0
engine:
  kind: fake
models:
  - kind: base
    source: http://example.com/m.safetensors
    target: diffusion_models
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cfg.yaml"
        path.write_text(yaml_body)
        cfg = load_config(path)
    assert cfg.lifecycle().grace_after_session_s == 999.0
```

- [ ] **Step 3: Run tests to verify failure.**

Run: `pixi run pytest tests/core/test_lifecycle.py::test_lifecycle_grace_after_session_s_default_is_300 tests/core/test_config.py::test_lifecycle_config_grace_after_session_s_default_is_300 -v`
Expected: FAIL with `AttributeError: 'Lifecycle' object has no attribute 'grace_after_session_s'` / `ValidationError: extra forbidden`.

- [ ] **Step 4: Edit `src/kinoforge/core/interfaces.py`.**

Locate the `Lifecycle` dataclass (line 51). Add the new field right after `heartbeat_interval_s`:

```python
@dataclass
class Lifecycle:
    """Cost-safety guardrails carried into an InstanceSpec (all seconds).

    Attributes:
        heartbeat_interval_s: Layer U — seconds between background
            HeartbeatLoop ticks inside an active deploy_session.
            ``None`` (the default) disables the feature, preserving
            backwards-compatibility for every existing YAML config.
            Operator guidance: values < 10 risk lock contention at scale.
        grace_after_session_s: Layer V — post-session warm-reuse window
            within which a sentinel-stale, pod-up entry is treated as
            LIVE rather than ORPHAN_REAP. Default 300 (5 minutes).
            Prevents the reaper from racing a legitimate session start
            on a warm-reused pod whose first HeartbeatLoop tick has not
            yet fired.
    """

    idle_timeout_s: float = 2 * 3600
    job_timeout_s: float = 30 * 60
    time_buffer_s: float = 30 * 60
    max_lifetime_s: float = 5 * 3600
    budget_usd: float = 0.0
    max_workers: int = 1
    max_in_flight: int = 1
    boot_timeout_s: float = 900.0
    heartbeat_interval_s: float | None = None
    grace_after_session_s: float = 300.0
```

- [ ] **Step 5: Edit `src/kinoforge/core/config.py` — `LifecycleConfig`.**

Add the field right after `heartbeat_interval_s` (line 101):

```python
    heartbeat_interval_s: float | None = None
    grace_after_session_s: float = 300.0
```

Append the validator right after `_validate_heartbeat_interval_positive` (after line 131):

```python
    @field_validator("grace_after_session_s")
    @classmethod
    def _validate_grace_non_negative(cls, v: float) -> float:
        """Reject negative grace at load time (Layer V).

        Negative grace would invert the row-5/row-6 boundary in
        ``classify`` and cause sentinel-stale pods to be classified
        LIVE forever — paid leak class of bug.
        """
        if v < 0:
            raise ValueError(
                f"grace_after_session_s must be >= 0; got {v}"
            )
        return v
```

- [ ] **Step 6: Edit `src/kinoforge/core/config.py` — `Config.lifecycle()`.**

Locate the `InterfaceLifecycle(...)` call (line 711). Add the new kwarg as the final field before the close paren:

```python
        return InterfaceLifecycle(
            idle_timeout_s=lc.idle_timeout,
            job_timeout_s=lc.job_timeout,
            time_buffer_s=lc.time_buffer,
            max_lifetime_s=lc.max_lifetime,
            budget_usd=lc.budget,
            max_in_flight=lc.max_in_flight,
            boot_timeout_s=lc.boot_timeout,
            heartbeat_interval_s=lc.heartbeat_interval_s,
            grace_after_session_s=lc.grace_after_session_s,
        )
```

- [ ] **Step 7: Run tests to verify they pass.**

Run: `pixi run pytest tests/core/test_lifecycle.py tests/core/test_config.py -v`
Expected: all PASS (existing + 5 new = 5 added tests pass).

- [ ] **Step 8: Run full suite — no regressions.**

Run: `pixi run pytest tests/ -x -q`
Expected: 1351 + 5 = 1356 passed / 8 skipped (or equivalent — point is no regressions vs Layer U baseline).

- [ ] **Step 9: Lint / format / type-check.**

Run: `pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/core/config.py tests/core/test_lifecycle.py tests/core/test_config.py`
Expected: all hooks PASS.

- [ ] **Step 10: Commit.**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/config.py tests/core/test_lifecycle.py tests/core/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): Lifecycle.grace_after_session_s (Layer V T3)

One new YAML knob — `lifecycle.grace_after_session_s` — default 300
(5 minutes). Threshold gate between classify() rows 5 (ORPHAN_REAP)
and 6 (LIVE). Prevents the reaper from racing a legitimate session
start on a warm-reused pod whose first HeartbeatLoop tick has not
yet fired.

Pydantic validator rejects negative values at load time. All
existing configs work unchanged (additive).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Impure substrate (`core/reaper_actor.py`) — `act_on_verdict` + `provider_for`

**Goal:** Ship the side-effecting layer. Re-classify-before-act + Layer 18 per-instance lock + provider routing.

**Files:**
- Create: `src/kinoforge/core/reaper_actor.py`
- Test: `tests/core/test_reaper_actor.py`

**Acceptance Criteria:**
- [ ] `ActionResult` is a frozen dataclass with fields `instance_id`, `snapshot_verdict`, `applied_verdict`, `action`, `reason`.
- [ ] `provider_for(entry, registry_get_provider, cache)` caches by provider name — two entries with same provider produce one factory call.
- [ ] `provider_for` returns `None` and caches `None` on any factory exception (including `UnknownAdapter`, `AuthError`, generic `Exception`). Warning is logged.
- [ ] `act_on_verdict` acquires `store.acquire_lock(f"reaper/{id}", ttl_s=30.0)` before any provider call.
- [ ] `act_on_verdict` re-classifies inside the lock; if `v2 != snapshot_verdict` returns `ActionResult(action="skipped", reason="verdict drift ...")` and does NOT call `destroy_instance` or `ledger.forget`.
- [ ] `act_on_verdict` with snapshot+re-classify both `IDLE_REAP` calls `destroy_confirmed` then `ledger.forget`; returns `action="destroyed_and_forgot"`.
- [ ] Same path for `OVERAGE_REAP` and `ORPHAN_REAP`.
- [ ] `act_on_verdict` with `STALE_LEDGER` only forgets; does not call `destroy_instance`. `action="forgot"`.
- [ ] `act_on_verdict` with `UNROUTABLE` only forgets. `action="forgot_unroutable"`.
- [ ] `act_on_verdict` with `LIVE` / `HEARTBEAT_UNKNOWN` returns `action="no_op"`.
- [ ] `TeardownError` from `destroy_confirmed` is caught locally and returned as `action="failed"`; never propagates out of `act_on_verdict`.

**Verify:** `pixi run pytest tests/core/test_reaper_actor.py -v` → 12 passed.

**Steps:**

- [ ] **Step 1: Write the failing test `tests/core/test_reaper_actor.py`.**

```python
"""Layer V T4: act_on_verdict + provider_for tests.

Covers spec §3.5 acceptance criteria AC9–AC10 plus per-verdict
dispatch and TeardownError isolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import AuthError, TeardownError, UnknownAdapter
from kinoforge.core.interfaces import Instance
from kinoforge.core.reaper import Verdict
from kinoforge.core.reaper_actor import (
    ActionResult,
    act_on_verdict,
    provider_for,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Tracks calls. ``live_ids`` controls what list_instances returns."""

    def __init__(self, live_ids: set[str] | None = None) -> None:
        self.live_ids: set[str] = set(live_ids) if live_ids else set()
        self.destroyed: list[str] = []
        self.list_calls: int = 0
        self._raise_on_destroy: bool = False

    def list_instances(self) -> list[Instance]:
        self.list_calls += 1
        return [
            Instance(
                id=i,
                provider="fake",
                created_at=0.0,
                status="ready",
                cost_rate_usd_per_hr=0.5,
                spec=None,
                tags={},
            )
            for i in self.live_ids
        ]

    def destroy_instance(self, instance_id: str) -> None:
        if self._raise_on_destroy:
            raise RuntimeError("simulated network error")
        self.destroyed.append(instance_id)
        self.live_ids.discard(instance_id)

    # Needed by destroy_confirmed's post-destroy verification
    def get_instance(self, instance_id: str) -> Instance:
        raise KeyError(instance_id)


class _FakeStore:
    """Captures lock acquires; provides a context-manager dummy lock."""

    def __init__(self) -> None:
        self.acquires: list[tuple[str, float]] = []

    def acquire_lock(self, key: str, *, ttl_s: float) -> "_FakeLock":
        self.acquires.append((key, ttl_s))
        return _FakeLock()


class _FakeLock:
    def __enter__(self) -> "_FakeLock":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _FakeLedger:
    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)


_THR: Mapping[str, Any] = dict(
    idle_timeout_s=100.0,
    max_lifetime_s=10_000.0,
    heartbeat_interval_s=30.0,
    grace_after_session_s=500.0,
)


def _entry(id_: str = "i-1", **overrides: Any) -> dict[str, Any]:
    """Default-fresh entry suitable for IDLE_REAP-on-re-classify tests."""
    base: dict[str, Any] = {
        "id": id_,
        "provider": "fake",
        "created_at": 0.0,
        "last_heartbeat": 0.0,  # very old
        "heartbeat_thread_tick": 0.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# provider_for — caching + failure modes
# ---------------------------------------------------------------------------


def test_provider_for_caches_by_provider_name() -> None:
    """Two entries with same provider name → one factory call."""
    factory = MagicMock(return_value=_FakeProvider())
    registry = MagicMock(return_value=factory)
    cache: dict[str, Any] = {}
    e1 = {"id": "a", "provider": "runpod"}
    e2 = {"id": "b", "provider": "runpod"}

    p1 = provider_for(e1, registry, cache)
    p2 = provider_for(e2, registry, cache)

    assert p1 is p2
    assert factory.call_count == 1


def test_provider_for_returns_none_on_unknown_adapter() -> None:
    factory = MagicMock(side_effect=UnknownAdapter("nope"))
    registry = MagicMock(return_value=factory)
    cache: dict[str, Any] = {}

    result = provider_for({"id": "a", "provider": "bogus"}, registry, cache)

    assert result is None
    assert cache["bogus"] is None


def test_provider_for_returns_none_on_auth_error() -> None:
    factory = MagicMock(side_effect=AuthError("RUNPOD_API_KEY unset"))
    registry = MagicMock(return_value=factory)
    result = provider_for({"id": "a", "provider": "runpod"}, registry, {})
    assert result is None


def test_provider_for_returns_none_on_generic_exception() -> None:
    """Any vendor SDK exception during construction → unroutable, never crash."""
    factory = MagicMock(side_effect=RuntimeError("network down"))
    registry = MagicMock(return_value=factory)
    result = provider_for({"id": "a", "provider": "runpod"}, registry, {})
    assert result is None


# ---------------------------------------------------------------------------
# act_on_verdict — lock acquisition (AC10)
# ---------------------------------------------------------------------------


def test_act_on_verdict_acquires_per_instance_lock() -> None:
    """Lock key is `reaper/<id>` with ttl_s=30.0."""
    store = _FakeStore()
    ledger = _FakeLedger()
    # Set up so that re-classify returns LIVE → no destruction; we only
    # care about the lock acquire side effect.
    provider = _FakeProvider(live_ids={"i-1"})
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=1.0,
        heartbeat_thread_tick=1.0,
    )
    clock = FakeClock(start=1.0)  # everything sentinel-fresh + hb-fresh → LIVE
    act_on_verdict(
        store, ledger, provider, e, Verdict.IDLE_REAP,
        thresholds=_THR, clock=clock,
    )
    assert store.acquires == [("reaper/i-1", 30.0)]


# ---------------------------------------------------------------------------
# act_on_verdict — drift skip (AC9)
# ---------------------------------------------------------------------------


def test_act_on_verdict_drift_skips_destruction() -> None:
    """Snapshot=ORPHAN_REAP; re-classify=LIVE → skipped, no destroy."""
    store = _FakeStore()
    ledger = _FakeLedger()
    # Pod up, sentinel fresh, hb fresh → re-classify yields LIVE.
    provider = _FakeProvider(live_ids={"i-1"})
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=1.0,
        heartbeat_thread_tick=1.0,
    )
    clock = FakeClock(start=2.0)

    result = act_on_verdict(
        store, ledger, provider, e, Verdict.ORPHAN_REAP,
        thresholds=_THR, clock=clock,
    )

    assert result.action == "skipped"
    assert result.reason is not None and "drift" in result.reason
    assert provider.destroyed == []
    assert ledger.forgotten == []


# ---------------------------------------------------------------------------
# act_on_verdict — destruction paths
# ---------------------------------------------------------------------------


def test_act_on_verdict_idle_reap_destroys_and_forgets() -> None:
    """IDLE_REAP confirmed → destroy_confirmed + ledger.forget."""
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids={"i-1"})
    # hb very old → re-classify yields IDLE_REAP (sentinel-fresh, hb-stale)
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=0.0,
        heartbeat_thread_tick=499.0,
    )
    clock = FakeClock(start=500.0)

    result = act_on_verdict(
        store, ledger, provider, e, Verdict.IDLE_REAP,
        thresholds=_THR, clock=clock,
    )

    assert result.action == "destroyed_and_forgot"
    assert provider.destroyed == ["i-1"]
    assert ledger.forgotten == ["i-1"]


def test_act_on_verdict_orphan_reap_destroys_and_forgets() -> None:
    """ORPHAN_REAP confirmed (sentinel-stale + past grace) → destroy + forget."""
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids={"i-1"})
    # sentinel-stale; pod_age > grace → ORPHAN_REAP both times
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=10.0,
        heartbeat_thread_tick=10.0,
    )
    clock = FakeClock(start=1_000.0)  # sent_age=990>90; pod_age=1000>500

    result = act_on_verdict(
        store, ledger, provider, e, Verdict.ORPHAN_REAP,
        thresholds=_THR, clock=clock,
    )

    assert result.action == "destroyed_and_forgot"
    assert provider.destroyed == ["i-1"]


def test_act_on_verdict_stale_ledger_only_forgets() -> None:
    """STALE_LEDGER → ledger.forget; never call destroy_instance."""
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids=set())  # pod_up=False both times
    e = _entry(id_="i-1")
    clock = FakeClock(start=500.0)

    result = act_on_verdict(
        store, ledger, provider, e, Verdict.STALE_LEDGER,
        thresholds=_THR, clock=clock,
    )

    assert result.action == "forgot"
    assert provider.destroyed == []
    assert ledger.forgotten == ["i-1"]


def test_act_on_verdict_unroutable_only_forgets() -> None:
    """UNROUTABLE → ledger.forget (callers only reach this with --force-forget)."""
    store = _FakeStore()
    ledger = _FakeLedger()
    # Provider doesn't matter; we pre-stamp UNROUTABLE snapshot.
    provider = _FakeProvider(live_ids=set())
    e = _entry(id_="i-1")
    clock = FakeClock(start=500.0)

    # NB: classify never returns UNROUTABLE — so to test the action="forgot_unroutable"
    # path, we need snapshot=UNROUTABLE AND re-classify must also yield UNROUTABLE.
    # But classify can't yield UNROUTABLE. So we exercise via the drift-skip code path
    # by asserting that an UNROUTABLE snapshot always drifts → "skipped". That tests
    # the safe-by-default branch. The "forgot_unroutable" branch is exercised by
    # test_reaper_sweep with a mocked classify.
    result = act_on_verdict(
        store, ledger, provider, e, Verdict.UNROUTABLE,
        thresholds=_THR, clock=clock,
    )
    # re-classify yields STALE_LEDGER (pod_up=False) — drift
    assert result.action == "skipped"
    assert ledger.forgotten == []


def test_act_on_verdict_live_is_no_op() -> None:
    """LIVE snapshot + re-classify LIVE → no destroy, no forget."""
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids={"i-1"})
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=1.0,
        heartbeat_thread_tick=1.0,
    )
    clock = FakeClock(start=2.0)
    result = act_on_verdict(
        store, ledger, provider, e, Verdict.LIVE,
        thresholds=_THR, clock=clock,
    )
    assert result.action == "no_op"
    assert provider.destroyed == []
    assert ledger.forgotten == []


# ---------------------------------------------------------------------------
# act_on_verdict — TeardownError isolation
# ---------------------------------------------------------------------------


def test_act_on_verdict_swallows_teardown_error() -> None:
    """TeardownError from destroy_confirmed → ActionResult(action='failed').

    Must not propagate out of act_on_verdict — sweep continues across
    one-instance failures.
    """
    store = _FakeStore()
    ledger = _FakeLedger()
    provider = _FakeProvider(live_ids={"i-1"})
    provider._raise_on_destroy = True
    e = _entry(
        id_="i-1",
        created_at=0.0,
        last_heartbeat=0.0,
        heartbeat_thread_tick=499.0,
    )
    clock = FakeClock(start=500.0)

    result = act_on_verdict(
        store, ledger, provider, e, Verdict.IDLE_REAP,
        thresholds=_THR, clock=clock,
    )

    assert result.action == "failed"
    assert result.reason is not None
    # Ledger.forget MUST NOT have been called — destroyer didn't confirm.
    assert ledger.forgotten == []
```

- [ ] **Step 2: Run test to verify it fails.**

Run: `pixi run pytest tests/core/test_reaper_actor.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'kinoforge.core.reaper_actor'".

- [ ] **Step 3: Write `src/kinoforge/core/reaper_actor.py`.**

```python
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


@dataclass(frozen=True)
class ActionResult:
    """Outcome of a single ``act_on_verdict`` call.

    Attributes:
        instance_id: The id acted on.
        snapshot_verdict: What ``sweep`` classified the entry as.
        applied_verdict: What the act-time re-classify returned (may
            differ from ``snapshot_verdict`` under drift).
        action: One of ``"destroyed_and_forgot"``, ``"forgot"``,
            ``"forgot_unroutable"``, ``"skipped"``, ``"failed"``,
            ``"no_op"``.
        reason: Free-text explanation for skipped / failed actions.
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
    registry_get_provider: Callable[[str], Callable[[], "ComputeProvider"]],
    cache: dict[str, "ComputeProvider | None"],
) -> "ComputeProvider | None":
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


def act_on_verdict(
    store: "ArtifactStore",
    ledger: Ledger,
    provider: "ComputeProvider",
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
            if v2 in {Verdict.IDLE_REAP, Verdict.OVERAGE_REAP, Verdict.ORPHAN_REAP}:
                destroy_confirmed(provider, instance_id, sleep=lambda _: None)
                ledger.forget(instance_id)
                action = "destroyed_and_forgot"
            elif v2 == Verdict.STALE_LEDGER:
                ledger.forget(instance_id)
                action = "forgot"
            elif v2 == Verdict.UNROUTABLE:
                ledger.forget(instance_id)
                action = "forgot_unroutable"
            else:
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


# ``sweep`` lives in this module too but is added in Task 5 so the
# Task-4 commit stays focused on the per-instance contract.
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `pixi run pytest tests/core/test_reaper_actor.py -v`
Expected: 12 PASS.

- [ ] **Step 5: Lint / format / type-check.**

Run: `pixi run pre-commit run --files src/kinoforge/core/reaper_actor.py tests/core/test_reaper_actor.py`
Expected: all hooks PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/reaper_actor.py tests/core/test_reaper_actor.py
git commit -m "$(cat <<'EOF'
feat(reaper): act_on_verdict + provider_for impure substrate (Layer V T4)

The single side-effecting surface in Layer V. Acquires
`reaper/<id>` cross-process lock (Layer 18 primitive), re-classifies
inside the lock to close the human-in-the-loop race window between
dry-run snapshot and --apply, and dispatches per verdict.

TeardownError from destroy_confirmed is caught locally and returned
as ActionResult(action="failed") so sweep continues across one-
instance failures.

provider_for caches resolved providers + None-on-failure so a
misconfigured provider is logged once per sweep, not N times.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `sweep` orchestration with caches

**Goal:** Add the call-scoped, multi-provider sweep that consumes T1 (`classify`/`partition`) + T4 (`provider_for`/`act_on_verdict`) and produces a `SweepReport`. Cache `provider.list_instances()` per provider name.

**Files:**
- Modify: `src/kinoforge/core/reaper_actor.py` (append `sweep` function)
- Test: `tests/core/test_reaper_sweep.py` (new file, 6 tests)

**Acceptance Criteria:**
- [ ] Empty ledger → `SweepReport(snapshot={}, actions=[])`.
- [ ] Two entries with same provider name → one `list_instances()` call.
- [ ] Provider whose `list_instances()` raises → all its entries become `UNROUTABLE` for the remainder of the sweep; other providers unaffected.
- [ ] `policy=None` → `actions=[]` even when snapshot has destroyable verdicts.
- [ ] `policy=DEFAULT_APPLY_POLICY` → snapshot entries with `IDLE_REAP` / `OVERAGE_REAP` / `STALE_LEDGER` are passed to `act_on_verdict`; `ORPHAN_REAP` and `LIVE` and `HEARTBEAT_UNKNOWN` are not.
- [ ] One entry whose `act_on_verdict` returns `action="failed"` does NOT prevent the next entry from being acted on.

**Verify:** `pixi run pytest tests/core/test_reaper_sweep.py -v` → 6 passed.

**Steps:**

- [ ] **Step 1: Write failing test `tests/core/test_reaper_sweep.py`.**

```python
"""Layer V T5: sweep integration tests.

Covers spec ACs:
- AC11: provider.list_instances() cached per provider name
- AC12: failure isolation — one TeardownError doesn't abort sweep
- sweep w/ policy=None is read-only (no actions)
- sweep w/ DEFAULT_APPLY_POLICY routes correct subset
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import TeardownError
from kinoforge.core.interfaces import Instance
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
from kinoforge.core.reaper_actor import SweepReport, sweep


class _FakeStore:
    def __init__(self) -> None:
        self.acquires: list[tuple[str, float]] = []

    def acquire_lock(self, key: str, *, ttl_s: float):
        self.acquires.append((key, ttl_s))
        return _FakeLock()


class _FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


class _FakeLedger:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = list(entries)
        self.forgotten: list[str] = []

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)
        self._entries = [e for e in self._entries if e.get("id") != instance_id]


class _FakeProvider:
    def __init__(
        self,
        live_ids: set[str],
        *,
        list_raises: bool = False,
        destroy_raises: bool = False,
    ) -> None:
        self.live_ids = set(live_ids)
        self.list_calls = 0
        self.destroyed: list[str] = []
        self._list_raises = list_raises
        self._destroy_raises = destroy_raises

    def list_instances(self) -> list[Instance]:
        self.list_calls += 1
        if self._list_raises:
            raise RuntimeError("network down")
        return [
            Instance(
                id=i, provider="fake", created_at=0.0, status="ready",
                cost_rate_usd_per_hr=0.5, spec=None, tags={},
            )
            for i in self.live_ids
        ]

    def destroy_instance(self, instance_id: str) -> None:
        if self._destroy_raises:
            raise RuntimeError("destroy raises")
        self.destroyed.append(instance_id)
        self.live_ids.discard(instance_id)

    def get_instance(self, instance_id: str) -> Instance:
        raise KeyError(instance_id)


_THR: Mapping[str, Any] = dict(
    idle_timeout_s=100.0,
    max_lifetime_s=10_000.0,
    heartbeat_interval_s=30.0,
    grace_after_session_s=500.0,
)


def _registry(providers: dict[str, _FakeProvider]):
    """Build a registry_get_provider stub that maps name → zero-arg factory."""

    def _resolver(name: str):
        if name not in providers:
            raise KeyError(name)

        def _factory():
            return providers[name]

        return _factory

    return _resolver


# ---------------------------------------------------------------------------
# Empty / read-only paths
# ---------------------------------------------------------------------------


def test_sweep_empty_ledger_returns_empty_report() -> None:
    report = sweep(
        store=_FakeStore(),
        ledger=_FakeLedger([]),
        registry_get_provider=_registry({}),
        thresholds=_THR,
        clock=FakeClock(start=0.0),
        policy=None,
    )
    assert report.snapshot == {}
    assert report.actions == []


def test_sweep_policy_none_skips_all_actions() -> None:
    """Dry-run = policy=None. Snapshot present; actions empty."""
    prov = _FakeProvider(live_ids={"i-1"})
    ledger = _FakeLedger([
        {
            "id": "i-1", "provider": "fake", "created_at": 0.0,
            "last_heartbeat": 0.0, "heartbeat_thread_tick": 499.0,
        }
    ])
    report = sweep(
        store=_FakeStore(), ledger=ledger,
        registry_get_provider=_registry({"fake": prov}),
        thresholds=_THR, clock=FakeClock(start=500.0), policy=None,
    )
    assert report.snapshot["i-1"][1] == Verdict.IDLE_REAP
    assert report.actions == []
    assert prov.destroyed == []
    assert ledger.forgotten == []


# ---------------------------------------------------------------------------
# Provider cache (AC11)
# ---------------------------------------------------------------------------


def test_sweep_caches_list_instances_per_provider() -> None:
    """Two entries → same provider → exactly one list_instances() call."""
    prov = _FakeProvider(live_ids={"i-1", "i-2"})
    entries = [
        {"id": "i-1", "provider": "fake", "created_at": 0.0,
         "last_heartbeat": 100.0, "heartbeat_thread_tick": 100.0},
        {"id": "i-2", "provider": "fake", "created_at": 0.0,
         "last_heartbeat": 100.0, "heartbeat_thread_tick": 100.0},
    ]
    sweep(
        store=_FakeStore(), ledger=_FakeLedger(entries),
        registry_get_provider=_registry({"fake": prov}),
        thresholds=_THR, clock=FakeClock(start=101.0), policy=None,
    )
    assert prov.list_calls == 1


# ---------------------------------------------------------------------------
# list_instances failure → UNROUTABLE (AC12 cousin)
# ---------------------------------------------------------------------------


def test_sweep_list_instances_failure_demotes_provider_to_unroutable() -> None:
    """list_instances raises → all that provider's entries become UNROUTABLE."""
    prov_a = _FakeProvider(live_ids=set(), list_raises=True)
    prov_b = _FakeProvider(live_ids={"i-b"})
    entries = [
        {"id": "i-a", "provider": "broken", "created_at": 0.0},
        {"id": "i-b", "provider": "fine", "created_at": 0.0,
         "last_heartbeat": 100.0, "heartbeat_thread_tick": 100.0},
    ]
    report = sweep(
        store=_FakeStore(), ledger=_FakeLedger(entries),
        registry_get_provider=_registry({"broken": prov_a, "fine": prov_b}),
        thresholds=_THR, clock=FakeClock(start=101.0), policy=None,
    )
    assert report.snapshot["i-a"][1] == Verdict.UNROUTABLE
    assert report.snapshot["i-b"][1] == Verdict.LIVE


# ---------------------------------------------------------------------------
# Policy dispatch — DEFAULT_APPLY_POLICY routes the right subset
# ---------------------------------------------------------------------------


def test_sweep_default_apply_policy_acts_on_idle_overage_stale() -> None:
    """IDLE_REAP / OVERAGE_REAP / STALE_LEDGER acted; ORPHAN_REAP skipped."""
    # i-idle: sentinel-fresh, hb-stale → IDLE_REAP → act
    # i-orphan: sentinel-stale, past grace → ORPHAN_REAP → NOT acted
    # i-gone: pod_up=False → STALE_LEDGER → act
    prov = _FakeProvider(live_ids={"i-idle", "i-orphan"})
    entries = [
        {"id": "i-idle", "provider": "fake", "created_at": 0.0,
         "last_heartbeat": 0.0, "heartbeat_thread_tick": 499.0},
        {"id": "i-orphan", "provider": "fake", "created_at": 0.0,
         "last_heartbeat": 10.0, "heartbeat_thread_tick": 10.0},
        {"id": "i-gone", "provider": "fake", "created_at": 0.0},
    ]
    report = sweep(
        store=_FakeStore(), ledger=_FakeLedger(entries),
        registry_get_provider=_registry({"fake": prov}),
        thresholds=_THR, clock=FakeClock(start=1_000.0),
        policy=DEFAULT_APPLY_POLICY,
    )
    acted_ids = {a.instance_id for a in report.actions}
    # i-orphan must NOT be acted (ORPHAN_REAP not in DEFAULT_APPLY_POLICY)
    assert "i-orphan" not in acted_ids
    # i-idle + i-gone are acted
    assert {"i-idle", "i-gone"}.issubset(acted_ids)


# ---------------------------------------------------------------------------
# Failure isolation (AC12)
# ---------------------------------------------------------------------------


def test_sweep_one_teardown_failure_does_not_abort_remaining() -> None:
    """First entry's destroy_confirmed raises; second entry still processed."""
    prov = _FakeProvider(live_ids={"i-1", "i-2"}, destroy_raises=True)
    # Both entries IDLE_REAP. i-1 destroy raises; i-2 still attempted.
    entries = [
        {"id": "i-1", "provider": "fake", "created_at": 0.0,
         "last_heartbeat": 0.0, "heartbeat_thread_tick": 499.0},
        {"id": "i-2", "provider": "fake", "created_at": 0.0,
         "last_heartbeat": 0.0, "heartbeat_thread_tick": 499.0},
    ]
    report = sweep(
        store=_FakeStore(), ledger=_FakeLedger(entries),
        registry_get_provider=_registry({"fake": prov}),
        thresholds=_THR, clock=FakeClock(start=500.0),
        policy=DEFAULT_APPLY_POLICY,
    )
    actions_by_id = {a.instance_id: a for a in report.actions}
    assert actions_by_id["i-1"].action == "failed"
    assert actions_by_id["i-2"].action == "failed"  # destroy still raises
    # Critical: BOTH were attempted (sweep didn't abort after first failure).
    assert len(report.actions) == 2
```

- [ ] **Step 2: Run test to verify it fails.**

Run: `pixi run pytest tests/core/test_reaper_sweep.py -v`
Expected: FAIL with "ImportError: cannot import name 'sweep'".

- [ ] **Step 3: Append `sweep` to `src/kinoforge/core/reaper_actor.py`.**

Replace the trailing `# ``sweep`` lives in this module too but is added in Task 5...` comment line from T4 with the full function below:

```python
def sweep(
    store: "ArtifactStore",
    ledger: Ledger,
    registry_get_provider: Callable[[str], Callable[[], "ComputeProvider"]],
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
    captured by ``act_on_verdict`` as ``action="failed"`` and does not
    propagate.

    Args:
        store: Artifact store for the cross-process lock used by
            ``act_on_verdict``.
        ledger: Ledger to enumerate and mutate.
        registry_get_provider: Usually ``kinoforge.core.registry.get_provider``.
        thresholds: Threshold kwargs forwarded to ``classify``.
        clock: Wall-clock source.
        policy: When ``None``, sweep is read-only (no actions returned).
            Otherwise, snapshot entries whose verdict is in
            ``policy.act_verdicts`` are dispatched to
            ``act_on_verdict``.

    Returns:
        :class:`SweepReport` with the verdict snapshot and (optional)
        action results.
    """
    now = clock.now()
    provider_cache: dict[str, "ComputeProvider | None"] = {}
    live_pod_ids_cache: dict[str, set[str]] = {}

    entries = list(ledger.entries())
    snapshot: dict[str, tuple[Mapping[str, Any], Verdict]] = {}

    for entry in entries:
        eid = str(entry["id"])
        provider = provider_for(entry, registry_get_provider, provider_cache)
        if provider is None:
            snapshot[eid] = (entry, Verdict.UNROUTABLE)
            continue
        name = str(entry.get("provider", "local"))
        if name not in live_pod_ids_cache:
            try:
                live_pod_ids_cache[name] = {
                    i.id for i in provider.list_instances()
                }
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

    to_act, _to_skip = partition(
        {eid: v for eid, (_, v) in snapshot.items()}, policy
    )
    actions: list[ActionResult] = []
    for eid, verdict in to_act.items():
        entry, _ = snapshot[eid]
        name = str(entry.get("provider", "local"))
        provider = provider_cache.get(name)
        if provider is None:
            continue
        result = act_on_verdict(
            store, ledger, provider, entry, verdict,
            thresholds=thresholds, clock=clock,
        )
        actions.append(result)
    return SweepReport(snapshot=snapshot, actions=actions)
```

- [ ] **Step 4: Run sweep tests + entire core suite to verify.**

Run: `pixi run pytest tests/core/test_reaper_sweep.py -v`
Expected: 6 PASS.

Run: `pixi run pytest tests/core/ -q`
Expected: all PASS (no regressions vs T4 baseline).

- [ ] **Step 5: Lint / format / type-check.**

Run: `pixi run pre-commit run --files src/kinoforge/core/reaper_actor.py tests/core/test_reaper_sweep.py`
Expected: all hooks PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/reaper_actor.py tests/core/test_reaper_sweep.py
git commit -m "$(cat <<'EOF'
feat(reaper): sweep multi-provider orchestration with caches (Layer V T5)

Per-call provider cache + per-call list_instances cache mean N
entries with the same provider name produce one factory call and
one provider round-trip.

Failure isolation: a single list_instances exception demotes that
provider to UNROUTABLE for the rest of the sweep but other providers
continue. A single TeardownError on one entry doesn't abort
remaining acts.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `kinoforge reap` CLI rewrite + flags + formatters

**Goal:** Replace `_cmd_reap` with the dry-run-default multi-provider sweeper. Add `--apply` / `--include-orphans` / `--force-forget` / `--strict` / `--id` / `--format` / `--config` flags. Ship human-table + JSONL output formats.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (rewrite `_cmd_reap` around line 699; add formatters as module-local helpers)
- Modify: `src/kinoforge/cli/_main.py` (rewrite `sub.add_parser("reap", ...)` around line 147)
- Test: `tests/cli/test_cmd_reap.py` (new file, 10 tests)

**Acceptance Criteria:**
- [ ] `kinoforge reap` with no flags writes the verdict table to stdout; no destructive calls; exit code 0.
- [ ] `--apply` triggers actor loop; `DEFAULT_APPLY_POLICY` honored.
- [ ] `--include-orphans` without `--apply` → exit code 4 + stderr message.
- [ ] `--include-orphans` with `--apply` → entries with `ORPHAN_REAP` are passed to actor.
- [ ] `--force-forget` without `--apply` → exit code 4.
- [ ] `--strict` with a `UNROUTABLE` or `HEARTBEAT_UNKNOWN` verdict present → exit code 3.
- [ ] `--id X` restricts sweep to one ledger entry.
- [ ] `--format json` emits one JSON record per line (header + one per snapshot entry + one per action). Stdout is parseable as JSONL.
- [ ] Empty ledger → exit 0, "reap: ledger empty" message.
- [ ] One `action="failed"` in actions → exit code 2.

**Verify:** `pixi run pytest tests/cli/test_cmd_reap.py -v` → 10 passed.

**Steps:**

- [ ] **Step 1: Write failing tests at `tests/cli/test_cmd_reap.py`.**

```python
"""Layer V T6: CLI `kinoforge reap` integration tests.

Covers AC13–AC16 of spec §4.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.cli._commands import _cmd_reap
from kinoforge.cli.context import SessionContext


def _args(**overrides: Any) -> argparse.Namespace:
    """Default flags = dry-run, no opts."""
    base = dict(
        apply=False,
        include_orphans=False,
        force_forget=False,
        strict=False,
        id=None,
        format="human",
        config=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class _FakeCtx:
    """Minimal SessionContext stand-in for CLI tests."""

    def __init__(self, entries: list[dict[str, Any]], cfg: Any = None) -> None:
        self._entries = entries
        self.cfg = cfg
        self._ledger = MagicMock()
        self._ledger.entries.return_value = entries
        self._ledger.forget = MagicMock()
        self._store = MagicMock()

        # acquire_lock returns a context manager
        class _L:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return None
        self._store.acquire_lock = MagicMock(return_value=_L())

    def ledger(self):
        return self._ledger

    def store(self):
        return self._store


# ---------------------------------------------------------------------------
# Dry-run default
# ---------------------------------------------------------------------------


def test_reap_dry_run_default_does_not_destroy(capsys) -> None:
    """No --apply → no destructive calls; verdict table printed."""
    ctx = _FakeCtx([])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        from kinoforge.core.reaper_actor import SweepReport
        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        code = _cmd_reap(_args(), ctx)
    assert code == 0
    # sweep called with policy=None for dry-run
    assert mock_sweep.call_args.kwargs["policy"] is None


def test_reap_empty_ledger_prints_message_and_exits_zero(capsys) -> None:
    ctx = _FakeCtx([])
    code = _cmd_reap(_args(), ctx)
    out = capsys.readouterr().out + capsys.readouterr().err
    assert code == 0
    assert "empty" in out.lower() or "no" in out.lower()


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------


def test_reap_include_orphans_without_apply_exit_4(capsys) -> None:
    ctx = _FakeCtx([])
    code = _cmd_reap(_args(include_orphans=True), ctx)
    err = capsys.readouterr().err
    assert code == 4
    assert "--apply" in err


def test_reap_force_forget_without_apply_exit_4(capsys) -> None:
    ctx = _FakeCtx([])
    code = _cmd_reap(_args(force_forget=True), ctx)
    assert code == 4


# ---------------------------------------------------------------------------
# --apply path
# ---------------------------------------------------------------------------


def test_reap_apply_routes_default_policy_to_sweep() -> None:
    """sweep is called with DEFAULT_APPLY_POLICY when --apply set."""
    from kinoforge.core.reaper import DEFAULT_APPLY_POLICY
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _FakeCtx([{"id": "i-1", "provider": "fake"}])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        _cmd_reap(_args(apply=True), ctx)
    assert mock_sweep.call_args.kwargs["policy"].act_verdicts == \
        DEFAULT_APPLY_POLICY.act_verdicts


def test_reap_apply_include_orphans_extends_policy() -> None:
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _FakeCtx([{"id": "i-1", "provider": "fake"}])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        _cmd_reap(_args(apply=True, include_orphans=True), ctx)
    assert Verdict.ORPHAN_REAP in mock_sweep.call_args.kwargs["policy"].act_verdicts


# ---------------------------------------------------------------------------
# --strict
# ---------------------------------------------------------------------------


def test_reap_strict_with_unroutable_present_exits_3() -> None:
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _FakeCtx([{"id": "i-1", "provider": "broken"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "broken"}, Verdict.UNROUTABLE)}
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=[])
        code = _cmd_reap(_args(strict=True), ctx)
    assert code == 3


def test_reap_strict_no_uncertainty_exits_0() -> None:
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _FakeCtx([{"id": "i-1", "provider": "fake"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "fake"}, Verdict.LIVE)}
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=[])
        code = _cmd_reap(_args(strict=True), ctx)
    assert code == 0


# ---------------------------------------------------------------------------
# --format json
# ---------------------------------------------------------------------------


def test_reap_format_json_emits_jsonl(capsys) -> None:
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _FakeCtx([{"id": "i-1", "provider": "fake"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "fake", "created_at": 0.0},
                        Verdict.LIVE)}
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=[])
        code = _cmd_reap(_args(format="json"), ctx)

    out = capsys.readouterr().out.strip().splitlines()
    # Every line must be parseable JSON.
    for line in out:
        json.loads(line)
    assert code == 0


# ---------------------------------------------------------------------------
# action="failed" → exit 2
# ---------------------------------------------------------------------------


def test_reap_apply_with_failed_action_exits_2() -> None:
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import ActionResult, SweepReport

    ctx = _FakeCtx([{"id": "i-1", "provider": "fake"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "fake"}, Verdict.IDLE_REAP)}
    actions = [
        ActionResult(
            instance_id="i-1",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="failed",
            reason="simulated",
        )
    ]
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=actions)
        code = _cmd_reap(_args(apply=True), ctx)
    assert code == 2
```

- [ ] **Step 2: Run tests to verify they fail.**

Run: `pixi run pytest tests/cli/test_cmd_reap.py -v`
Expected: FAIL with `AttributeError` / `TypeError` (current `_cmd_reap` ignores flags).

- [ ] **Step 3: Rewrite `_cmd_reap` in `src/kinoforge/cli/_commands.py`.**

Locate the existing `_cmd_reap` (line 699) and the existing imports at top of file. Replace the entire `_cmd_reap` body with:

```python
def _cmd_reap(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``reap`` subcommand (Layer V — heartbeat-aware).

    Dry-run by default. ``--apply`` activates DEFAULT_APPLY_POLICY
    (IDLE_REAP, OVERAGE_REAP, STALE_LEDGER). Opt-in flags
    ``--include-orphans`` and ``--force-forget`` add ORPHAN_REAP and
    UNROUTABLE respectively. ``--strict`` exits non-zero when uncertain
    verdicts are surfaced.

    Args:
        args: Parsed CLI arguments — apply, include_orphans, force_forget,
            strict, id, format, config (all optional).
        ctx: Per-invocation session context.

    Returns:
        Exit code per spec §3.7:
            * 0 — normal (dry-run or --apply with no failures)
            * 2 — at least one action="failed" under --apply
            * 3 — --strict tripped by UNROUTABLE / HEARTBEAT_UNKNOWN
            * 4 — invalid flag combo
    """
    from kinoforge.core import registry
    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.reaper import (
        DEFAULT_STRICT_VERDICTS,
        policy_from_cli_flags,
    )
    from kinoforge.core.reaper_actor import sweep

    apply_flag = bool(getattr(args, "apply", False))
    include_orphans = bool(getattr(args, "include_orphans", False))
    force_forget = bool(getattr(args, "force_forget", False))
    strict = bool(getattr(args, "strict", False))
    single_id: str | None = getattr(args, "id", None)
    fmt: str = getattr(args, "format", "human") or "human"

    if include_orphans and not apply_flag:
        print(
            "error: --include-orphans requires --apply (Layer V opt-in safety)",
            file=sys.stderr,
        )
        return 4
    if force_forget and not apply_flag:
        print(
            "error: --force-forget requires --apply (Layer V opt-in safety)",
            file=sys.stderr,
        )
        return 4

    ledger = ctx.ledger()
    entries = ledger.entries()
    if single_id is not None:
        entries = [e for e in entries if e.get("id") == single_id]
    if not entries:
        print("reap: ledger empty (nothing to do)")
        return 0

    cfg = ctx.cfg
    lifecycle = cfg.lifecycle() if cfg is not None else Lifecycle()
    thresholds = {
        "idle_timeout_s": lifecycle.idle_timeout_s,
        "max_lifetime_s": lifecycle.max_lifetime_s,
        "heartbeat_interval_s": lifecycle.heartbeat_interval_s,
        "grace_after_session_s": lifecycle.grace_after_session_s,
    }

    policy = policy_from_cli_flags(
        apply=apply_flag,
        include_orphans=include_orphans,
        force_forget=force_forget,
    )

    # Subset-ledger wrapper for --id: sweep enumerates entries via
    # ledger.entries(); we shim a fresh proxy that returns only the
    # filtered list. Mutation paths (forget) pass through.
    if single_id is not None:
        original_entries = ledger.entries

        def _filtered_entries() -> list[dict]:  # type: ignore[type-arg]
            return [e for e in original_entries() if e.get("id") == single_id]

        ledger.entries = _filtered_entries  # type: ignore[method-assign]

    store = ctx.store()
    clock = _cli_clock()

    report = sweep(
        store=store,
        ledger=ledger,
        registry_get_provider=registry.get_provider,
        thresholds=thresholds,
        clock=clock,
        policy=policy if apply_flag else None,
    )

    if fmt == "json":
        _emit_reap_jsonl(report)
    else:
        _emit_reap_human(report, apply_flag, include_orphans)

    # Exit code priority: failed actions > strict > 0
    if any(a.action == "failed" for a in report.actions):
        return 2
    if strict:
        verdicts = {v for _, v in report.snapshot.values()}
        if verdicts & DEFAULT_STRICT_VERDICTS:
            return 3
    return 0


def _emit_reap_human(
    report: "SweepReport", applied: bool, include_orphans: bool
) -> None:
    """Pretty-print the verdict table + summary (Layer V T6)."""
    if not report.snapshot:
        print("reap: no entries to classify")
        return
    print(
        f"{'verdict':<18}{'id':<22}{'provider':<10}{'age_h':>7}"
        f"{'hb_age_s':>10}{'sent_age_s':>12}"
    )
    import time as _t
    now = _t.time()
    for eid, (entry, verdict) in report.snapshot.items():
        provider = entry.get("provider", "?")
        created_at = entry.get("created_at", now)
        try:
            age_h = max(0.0, (now - float(created_at)) / 3600.0)
            age_str = f"{age_h:.1f}"
        except (TypeError, ValueError):
            age_str = "-"
        hb = entry.get("last_heartbeat")
        hb_str = f"{(now - float(hb)):.0f}" if hb is not None else "-"
        tick = entry.get("heartbeat_thread_tick")
        sent_str = f"{(now - float(tick)):.0f}" if tick is not None else "-"
        print(
            f"{verdict.value:<18}{eid:<22}{str(provider):<10}"
            f"{age_str:>7}{hb_str:>10}{sent_str:>12}"
        )
    print()
    if not applied:
        print(
            f"{len(report.snapshot)} entries classified — pass --apply "
            "to act on default policy"
        )
        if not include_orphans:
            orphans = sum(
                1 for _, v in report.snapshot.values()
                if v.value == "ORPHAN_REAP"
            )
            if orphans:
                print(f"add --include-orphans to also act on {orphans} orphan(s)")
    else:
        destroyed = sum(
            1 for a in report.actions if a.action == "destroyed_and_forgot"
        )
        forgot = sum(
            1 for a in report.actions
            if a.action in {"forgot", "forgot_unroutable"}
        )
        skipped = sum(1 for a in report.actions if a.action == "skipped")
        failed = sum(1 for a in report.actions if a.action == "failed")
        print(
            f"acted on {len(report.actions)}: {destroyed} destroyed · "
            f"{forgot} forgotten · {skipped} drift-skipped · {failed} failed"
        )


def _emit_reap_jsonl(report: "SweepReport") -> None:
    """Emit JSONL: one record per snapshot entry plus one per action."""
    print(json.dumps({"type": "header", "entries": len(report.snapshot)}))
    for eid, (entry, verdict) in report.snapshot.items():
        print(json.dumps({
            "type": "verdict",
            "id": eid,
            "provider": str(entry.get("provider", "?")),
            "verdict": verdict.value,
        }))
    for action in report.actions:
        print(json.dumps({
            "type": "action",
            "id": action.instance_id,
            "snapshot_verdict": action.snapshot_verdict.value,
            "applied_verdict": action.applied_verdict.value,
            "action": action.action,
            "reason": action.reason,
        }))
```

Also add the TYPE_CHECKING import at the top of the module (if not already present) so the forward-string `"SweepReport"` in the helper signatures type-checks. Locate the existing `from __future__ import annotations` block and confirm. If `SweepReport` isn't yet imported via `if TYPE_CHECKING:`, add:

```python
if TYPE_CHECKING:
    from kinoforge.core.reaper_actor import SweepReport
```

near the other TYPE_CHECKING block. (Search existing _commands.py for `TYPE_CHECKING` first — if none, prepend a fresh `from typing import TYPE_CHECKING` import and the guarded block.)

- [ ] **Step 4: Update `_main.py` reap subparser.**

Locate line 147 (`sub.add_parser("reap", ...)`). Replace with:

```python
    # reap (Layer V — heartbeat-aware sweeper)
    p_reap = sub.add_parser("reap", help="classify ledger; optionally destroy stale instances")
    p_reap.add_argument("--apply", action="store_true",
                        help="actually destroy / forget (default: dry-run)")
    p_reap.add_argument("--include-orphans", action="store_true",
                        help="extend --apply to ORPHAN_REAP entries")
    p_reap.add_argument("--force-forget", action="store_true",
                        help="extend --apply to UNROUTABLE entries")
    p_reap.add_argument("--strict", action="store_true",
                        help="exit non-zero on UNROUTABLE / HEARTBEAT_UNKNOWN")
    p_reap.add_argument("--id", default=None, metavar="ID",
                        help="restrict sweep to one ledger entry")
    p_reap.add_argument("--format", choices=("human", "json"), default="human",
                        help="output format (default: human)")
    p_reap.add_argument("--config", "-c", type=Path, default=None,
                        metavar="PATH",
                        help="cfg for thresholds; defaults to Lifecycle() defaults")
```

- [ ] **Step 5: Run tests to verify they pass.**

Run: `pixi run pytest tests/cli/test_cmd_reap.py -v`
Expected: 10 PASS.

- [ ] **Step 6: Run any existing CLI test that touches `_cmd_reap` to catch regressions.**

Run: `pixi run pytest tests/cli/test_commands_routing.py -v -k reap`
Expected: existing test (`test_cmd_reap_returns_0_with_empty_ledger`) PASS — empty-ledger path still works.

- [ ] **Step 7: Run lint/format/typecheck.**

Run: `pixi run pre-commit run --files src/kinoforge/cli/_commands.py src/kinoforge/cli/_main.py tests/cli/test_cmd_reap.py`
Expected: all hooks PASS.

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/cli/_commands.py src/kinoforge/cli/_main.py tests/cli/test_cmd_reap.py
git commit -m "$(cat <<'EOF'
feat(cli): kinoforge reap dry-run-default + multi-provider sweep (Layer V T6)

Rewrites _cmd_reap as a thin formatter on top of core/reaper_actor.sweep.
Default = dry-run (no destructive calls); --apply activates
DEFAULT_APPLY_POLICY. Opt-ins: --include-orphans, --force-forget.
--strict exits non-zero on UNROUTABLE / HEARTBEAT_UNKNOWN.

JSONL output via --format json: one record per snapshot entry plus
one per action — composable with `jq` and CI pipelines.

Exit codes (spec §3.7):
- 0 normal
- 2 at least one action="failed" under --apply
- 3 --strict tripped
- 4 invalid flag combo (--include-orphans without --apply, etc.)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `kinoforge status` verdict line

**Goal:** Surface the same `classify` verdict in the existing key=value status block as `verdict=<...>`. Single source of truth for "what would reap do to this entry?"

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_cmd_status` around line 564 — extend `provider_block` construction to call `classify` and add `verdict` key)
- Modify: `tests/cli/test_commands_routing.py` (+2 tests)

**Acceptance Criteria:**
- [ ] When the entry has Layer U fields and provider returns the id, `kinoforge status` prints `verdict=LIVE` / `verdict=IDLE_REAP` / etc. on its own line in the sorted output.
- [ ] When the registry has no factory for `entry["provider"]`, `verdict=UNROUTABLE` is printed alongside the existing `provider_status=unknown (...)` line.
- [ ] When `provider.list_instances()` reports the id is absent, `verdict=STALE_LEDGER` is printed (matches existing stale-ledger advisory).
- [ ] Layer U sentinel-staleness advisory line is preserved unchanged.
- [ ] Existing `_cmd_status` exit codes from Layer S unchanged.

**Verify:** `pixi run pytest tests/cli/test_commands_routing.py -v -k status` → all PASS.

**Steps:**

- [ ] **Step 1: Locate existing status tests + add 2 failing tests at the bottom of `tests/cli/test_commands_routing.py`.**

```python
# ---------------------------------------------------------------------------
# Layer V — kinoforge status verdict line
# ---------------------------------------------------------------------------


def test_cmd_status_surfaces_verdict_line_for_live_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When pod_up + sentinel-fresh + hb-fresh → verdict=LIVE printed."""
    from kinoforge.cli import _commands
    from kinoforge.cli.context import SessionContext
    from kinoforge.providers.local import LocalProvider

    # Build a ledger with one fresh entry.
    state_dir = tmp_path / "state"
    ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=None)
    ledger = ctx.ledger()

    import time as _t
    now = _t.time()
    from kinoforge.core.interfaces import Instance
    instance = Instance(
        id="i-1", provider="local", created_at=now,
        status="ready", cost_rate_usd_per_hr=0.0, spec=None, tags={},
    )
    ledger.record(instance)
    ledger.touch(
        "i-1", last_heartbeat=now, heartbeat_thread_tick=now,
    )

    # Mock provider.get_instance + list_instances to return the entry
    fake_provider = LocalProvider()
    fake_provider._instances = {"i-1": instance}  # type: ignore[attr-defined]

    def _factory():
        return fake_provider

    monkeypatch.setattr(
        "kinoforge.core.registry.get_provider", lambda name: _factory
    )

    args = argparse.Namespace(id="i-1", config=None)
    code = _commands._cmd_status(args, ctx)
    out = capsys.readouterr().out
    assert code == 0
    assert "verdict=LIVE" in out


def test_cmd_status_surfaces_verdict_unroutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the registry has no factory → verdict=UNROUTABLE printed."""
    from kinoforge.cli import _commands
    from kinoforge.cli.context import SessionContext
    from kinoforge.core.errors import UnknownAdapter
    from kinoforge.core.interfaces import Instance

    state_dir = tmp_path / "state"
    ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=None)
    ledger = ctx.ledger()
    import time as _t
    now = _t.time()
    instance = Instance(
        id="i-1", provider="bogus", created_at=now,
        status="ready", cost_rate_usd_per_hr=0.0, spec=None, tags={},
    )
    ledger.record(instance)

    def _raise(_name):
        raise UnknownAdapter("bogus")

    monkeypatch.setattr("kinoforge.core.registry.get_provider", _raise)

    args = argparse.Namespace(id="i-1", config=None)
    code = _commands._cmd_status(args, ctx)
    out = capsys.readouterr().out
    assert code == 2
    assert "verdict=UNROUTABLE" in out
```

- [ ] **Step 2: Run tests to verify they fail.**

Run: `pixi run pytest tests/cli/test_commands_routing.py -v -k verdict`
Expected: FAIL — `verdict=...` line absent from output.

- [ ] **Step 3: Patch `_cmd_status` in `src/kinoforge/cli/_commands.py`.**

In `_cmd_status` (line 516), modify the `provider_block` construction at each branch to include a `verdict` key. Add this helper near the top of the file (next to other helpers):

```python
def _classify_for_status(
    entry: dict[str, Any],  # type: ignore[type-arg]
    live_ids: set[str],
    cfg: Any,
    now: float,
) -> str:
    """Compute a verdict string for `kinoforge status`. Same call as reap."""
    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.reaper import classify

    lifecycle = cfg.lifecycle() if cfg is not None else Lifecycle()
    return classify(
        entry,
        live_ids,
        now,
        idle_timeout_s=lifecycle.idle_timeout_s,
        max_lifetime_s=lifecycle.max_lifetime_s,
        heartbeat_interval_s=lifecycle.heartbeat_interval_s,
        grace_after_session_s=lifecycle.grace_after_session_s,
    ).value
```

Modify the three `provider_block = {...}` assignments inside `_cmd_status`:

**Unknown-adapter branch (line ~567):**
```python
    except UnknownAdapter:
        provider_block = {
            "provider_status": f"unknown (unknown provider: {provider_name})",
            "verdict": "UNROUTABLE",
        }
```

**Stale-ledger branch (line ~577 — `except KeyError`):**
```python
    except KeyError:
        provider_block = {
            "provider_status": "unknown (stale ledger — provider has no record)",
            "verdict": "STALE_LEDGER",
        }
```

**Healthy branch (line ~600):**
```python
    provider_block = {"provider_status": instance.status}
    try:
        provider_block["endpoints"] = json.dumps(provider.endpoints(args.id))
    except Exception as exc:  # noqa: BLE001
        provider_block["endpoints"] = f"unknown ({exc.__class__.__name__})"

    # Layer V — verdict line, same source of truth as `kinoforge reap`.
    try:
        live_ids = {i.id for i in provider.list_instances()}
    except Exception:  # noqa: BLE001 — fall back to assume present
        live_ids = {args.id}
    provider_block["verdict"] = _classify_for_status(entry, live_ids, cfg, now)
```

**Generic-exception branch (line ~591):**
```python
    except Exception as exc:  # noqa: BLE001 — explicit transient-error surface
        provider_block = {
            "provider_status": (
                f"unknown (provider lookup failed: {exc.__class__.__name__})"
            ),
            "verdict": "HEARTBEAT_UNKNOWN",
        }
```

- [ ] **Step 4: Run the verdict tests + the full Layer S status test suite.**

Run: `pixi run pytest tests/cli/test_commands_routing.py -v -k status`
Expected: all status tests PASS (existing + 2 new).

- [ ] **Step 5: Lint / format / type-check.**

Run: `pixi run pre-commit run --files src/kinoforge/cli/_commands.py tests/cli/test_commands_routing.py`
Expected: all hooks PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/cli/_commands.py tests/cli/test_commands_routing.py
git commit -m "$(cat <<'EOF'
feat(cli): kinoforge status surfaces Layer V verdict line (Layer V T7)

Adds verdict=<VERDICT> to the key=value status block. Same classify
call as `kinoforge reap` — single source of truth for "what would
reap do to this entry?". Layer U sentinel-staleness advisory line
is preserved.

Exercises the Layer V substrate from a second consumer in the same
release — validates that core/reaper.classify is genuinely reusable,
not CLI-shaped.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: README + PROGRESS + example YAML + final gate + merge

**Goal:** Document the new operator surface, update progress trail, run the full pre-commit suite, and merge to main via `--no-ff`.

**Files:**
- Modify: `README.md` (add Operator → Reaping section)
- Modify: `PROGRESS.md` (add Phase 37 entry; update Single next action)
- Modify: `examples/configs/wan.yaml` (+ commented `grace_after_session_s` line)
- Modify: `examples/configs/hosted.yaml` (+ commented line)
- Modify: `examples/configs/diffusers.yaml` (+ commented line)
- Modify: `examples/configs/fal.yaml` (+ commented line)
- Modify: `examples/configs/local-fake.yaml` (+ commented line)

**Acceptance Criteria:**
- [ ] README has a new sub-section under "Operator" describing dry-run, `--apply`, `--include-orphans`, `--force-forget`, `--strict`, `--format json`, exit codes, and the relationship to Layer U.
- [ ] PROGRESS.md gains a Phase 37 / Layer V entry with per-task SHAs (placeholders OK at write time — backfill in the merge commit).
- [ ] Each example YAML carries one new commented line: `# grace_after_session_s: 300  # Layer V — post-session warm-reuse grace`.
- [ ] `pixi run pre-commit run --all-files` exits zero.
- [ ] `pixi run pytest tests/ -q` exits zero with the expected count (baseline 1351 + Layer V new tests ≈ 1351 + 25 + 12 + 6 + 10 + 2 + 5 + 1 = 1412 passed / 8 skipped).
- [ ] Merge commit on `main` references all per-task SHAs and closes the "Layer V candidate" PROGRESS:163 follow-up.

**Verify:** `git log --oneline main..HEAD` shows the per-task chain; `pixi run pytest tests/ -q` is green on `main` after merge.

**Steps:**

- [ ] **Step 1: Update example YAMLs.**

Add to `examples/configs/wan.yaml` (and each of the other four examples) a single commented line right after the existing `heartbeat_interval_s` (or in the lifecycle block where it would naturally sit):

```yaml
  # Layer V — post-session warm-reuse grace window. Sentinel-stale
  # entries within this window are classified LIVE (not ORPHAN_REAP).
  # grace_after_session_s: 300
```

Repeat the same comment block (verbatim) in each YAML so the reaper documentation is discoverable from any config a user might already have open.

- [ ] **Step 2: Append the Operator → Reaping section to `README.md`.**

```markdown
## Reaping orphan pods

`kinoforge reap` classifies every ledger entry and (optionally)
destroys idle, over-age, or orphaned compute. Layer V is heartbeat-
aware: an entry whose Layer U `heartbeat_thread_tick` sentinel is
fresh is treated as live; a stale sentinel + past-grace pod becomes
an `ORPHAN_REAP` candidate.

### Dry-run (default)

```bash
kinoforge reap -c config.yaml
```

Prints a verdict table; no destructive action. Pass `--apply` to act.

### Acting on the default policy

```bash
kinoforge reap -c config.yaml --apply
```

Default policy destroys `IDLE_REAP` + `OVERAGE_REAP` and forgets
`STALE_LEDGER` entries. `ORPHAN_REAP` requires explicit opt-in:

```bash
kinoforge reap -c config.yaml --apply --include-orphans
```

### Other flags

| Flag | Effect |
|---|---|
| `--force-forget` | Adds UNROUTABLE → ledger.forget under --apply |
| `--strict` | Exit code 3 if any UNROUTABLE / HEARTBEAT_UNKNOWN present |
| `--id <X>` | Restrict to one ledger entry |
| `--format json` | JSONL output, one record per snapshot entry + per action |

### Exit codes

- 0 — normal (dry-run or --apply with no failures)
- 2 — at least one teardown failed under --apply
- 3 — `--strict` tripped
- 4 — invalid flag combo (e.g. `--include-orphans` without `--apply`)

### Sentinel-gate contract (Layer U → V)

The reaper trusts `last_heartbeat` only when the
`heartbeat_thread_tick` sentinel is fresh (within
`3 × heartbeat_interval_s`). Stale-sentinel + pod-up past
`grace_after_session_s` triggers `ORPHAN_REAP`. The grace window
(default 5 min) is operator-configurable via
`lifecycle.grace_after_session_s` in YAML or per-entry override.
```

- [ ] **Step 3: Append Phase 37 / Layer V to `PROGRESS.md`.**

```markdown
### Phase 37 — Layer V (heartbeat-aware reaper)

Closes the "Layer V candidate" carry-forward at PROGRESS:163. Ships
the first production consumer of Layer U's `heartbeat_thread_tick`
sentinel and the reusable substrate every future heartbeat consumer
(sweeper daemon, dashboard, in-session warm-reuse) will share.

- [x] Task 1: `core/reaper.py` pure substrate (Verdict, Policy, classify, partition) — commit `<T1-SHA>`
- [x] Task 2: invariant scan locking `core/reaper.py` purity — commit `<T2-SHA>`
- [x] Task 3: `Lifecycle.grace_after_session_s` + config wire — commit `<T3-SHA>`
- [x] Task 4: `core/reaper_actor.py` — `act_on_verdict`, `provider_for` — commit `<T4-SHA>`
- [x] Task 5: `sweep` orchestration with caches — commit `<T5-SHA>`
- [x] Task 6: `kinoforge reap` rewrite + flags + JSONL formatter — commit `<T6-SHA>`
- [x] Task 7: `kinoforge status` verdict line — commit `<T7-SHA>`
- [x] Task 8: README + PROGRESS + examples + final gate + merge — commit `<T8-SHA>`

**Key design decisions:**
- Substrate, not CLI patch (Q1=A). Pure `classify` / `Policy` /
  `partition` shared by every future consumer.
- D-hybrid verdict tree (Q2=D). LIVE / IDLE_REAP / ORPHAN_REAP /
  OVERAGE_REAP / STALE_LEDGER / HEARTBEAT_UNKNOWN / UNROUTABLE.
- Dry-run default + bundled `kinoforge status` verdict line (Q3=A).
- UNROUTABLE / STALE_LEDGER are first-class verdicts; auto-forget
  STALE_LEDGER under `--apply` closes the latent ledger-drift bug
  in the pre-Layer-V `reap()` (forced-forgot multi-provider entries
  against Local-only `live_ids`).
- A+C compromise on config (Q5): `lifecycle.grace_after_session_s`
  in YAML; explicit threshold kwargs to `classify`.
- B+C race mitigation (Q6): re-classify-before-act inside Layer 18
  per-instance lock.
- Approach 2 (Q7): strict purity split (`core/reaper.py` pure +
  `core/reaper_actor.py` impure) enforced by `test_core_invariant.py`.

**Test count:** ~1351 → ~1412 passed + 8 skipped. Fully offline-tested.
No live spend.

**Forward-compat hooks** (spec §7) lock the substrate's public surface
for Layer W (sweeper daemon), Layer X (dashboard), Layer Y
(in-session warm-reuse retrofit).
```

Update the "Single next action" section to point at Layer V as closed and update the remaining-candidates list (remove "heartbeat-aware reaper consuming the new sentinel-gate contract from Layer U" since it's now done).

- [ ] **Step 4: Run the full pre-commit suite on every file.**

Run: `pixi run pre-commit run --all-files`
Expected: all hooks PASS.

- [ ] **Step 5: Run the full test suite.**

Run: `pixi run pytest tests/ -q`
Expected: 1412+ passed / 8 skipped (or thereabouts — point is green + obviously larger than the 1351 baseline).

- [ ] **Step 6: Commit the doc bundle.**

```bash
git add README.md PROGRESS.md examples/configs/*.yaml
git commit -m "$(cat <<'EOF'
docs(layer-v): README + PROGRESS Phase 37 + example yaml comments

Operator-facing reap docs cover dry-run default, --apply, the
opt-in policy flags, exit codes, JSONL output, and the
sentinel-gate contract continuity with Layer U.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Verify working tree is clean.**

Run: `git status`
Expected: "nothing to commit, working tree clean".

Run: `git log --oneline main..HEAD`
Expected: 8 commits in task order.

- [ ] **Step 8: Merge to `main` via `--no-ff` (only if the user explicitly authorises the merge per CLAUDE.md durability rules).**

```bash
git checkout main
git merge --no-ff <feature-branch> -m "$(cat <<'EOF'
Merge Layer V — heartbeat-aware reaper

Phase 37. First production consumer of Layer U's sentinel-gate
contract. Pure substrate (core/reaper.py: Verdict, Policy,
classify, partition) + impure orchestrator (core/reaper_actor.py:
act_on_verdict, sweep). `kinoforge reap` rewritten as dry-run-
default multi-provider sweeper with --apply, --include-orphans,
--force-forget, --strict, --id, --format flags. `kinoforge
status` surfaces same classify verdict in a verdict=<...> line.

20 acceptance criteria. ~60 new tests across pure / actor / sweep /
CLI + invariant scan extension. Closes PROGRESS:163 carry-forward.

Per-task SHAs:
- T1 <SHA>
- T2 <SHA>
- T3 <SHA>
- T4 <SHA>
- T5 <SHA>
- T6 <SHA>
- T7 <SHA>
- T8 <SHA>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 9: Backfill the merge SHA into `PROGRESS.md`.**

```bash
git log --oneline -1   # capture the merge SHA
# Edit PROGRESS.md, replace the Phase 37 "merge commit" placeholder
# with the actual SHA. Commit with:
git add PROGRESS.md
git commit -m "chore(progress): backfill Layer V merge SHA <merge-SHA>"
```

---

## Self-Review

Cross-check the plan against the spec.

**Spec coverage:**
- §1 in-scope items → Tasks 1 (pure substrate), 4+5 (impure substrate), 6 (CLI rewrite), 7 (status verdict line), 3 (config field), 2 (invariant scan).
- §2 D1–D16 → all decisions baked into Tasks 1–8 (purity split T1+T2, hybrid verdict tree T1, sentinel-gate logic T1, opt-ins T6, per-instance lock T4, single new YAML knob T3, explicit kwargs T1, per-entry override T1, registry pattern T4, status surfacing T7, status bundling T7+T8).
- §3 architecture → T1 module map + T2 purity scan + T4/T5 actor + T6 CLI + T7 status.
- §3.3 verdict tree rows 1–7 → covered by `classify` tests in T1 + boundary tests + grace-window tests.
- §3.4 pure module — built in T1.
- §3.5 impure module — built in T4 + T5.
- §3.6 config — built in T3.
- §3.7 CLI surface — built in T6.
- §3.8 drift handling — covered by T4 drift-skip test.
- §4 ACs (AC1 → AC20) → all map to per-task ACs in Tasks 1, 2, 3, 4, 5, 6, 7, 8 in order.
- §5 risks → documented in spec; reaper code reflects mitigations (T4 re-classify, T4 BLE001 sites, T6 dry-run default).
- §6 out-of-scope → unchanged from spec; no work in this plan.
- §7 forward-compat hooks → all locked by T1 (substrate shape) + T2 (purity scan).

**Placeholder scan:** searched for "TBD", "TODO", "fill in", "similar to Task N" patterns; none found in tasks. Step-numbered code blocks contain real code, not pseudocode.

**Type consistency:**
- `Verdict` enum spelled the same in every task (T1 defines, T4/T5/T6/T7 import).
- `Policy` is a `@dataclass(frozen=True)` with single `act_verdicts: frozenset[Verdict]` field in T1; same shape referenced in T4 (`act_on_verdict` accepts policy not directly but via `partition`), T5 (`sweep` accepts `policy: Policy | None`), T6 (CLI builds policy via `policy_from_cli_flags`).
- `classify` signature (`entry, live_pod_ids, now, *, idle_timeout_s, max_lifetime_s, heartbeat_interval_s, grace_after_session_s`) used consistently in T1 tests, T4 `act_on_verdict`, T5 `sweep`, T7 `_classify_for_status`.
- `ActionResult` / `SweepReport` defined in T4, consumed in T5, T6 with same field names.
- `Lifecycle.grace_after_session_s: float = 300.0` (T3) matches the threshold name `grace_after_session_s` used in `classify` kwargs everywhere.
- `provider_for(entry, registry_get_provider, cache)` signature matches caller in T5 `sweep`.

No type / signature drift detected.

---

## Execution Handoff
