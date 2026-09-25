"""Behavior: the reaper must not delete a pre-launch provisional row mid-boot.

U64. The orchestrator writes a provisional ledger row BEFORE ``create_instance``
returns, tagged ``kf_launch_phase=launching`` and keyed by the CLIENT-side
``run_id`` — the pod NAME on RunPod, the app run id on Modal. That id is never
in the provider's live-pod listing, so ``classify`` row 1 (``if not pod_up``)
returned ``STALE_LEDGER``, which is in :data:`DEFAULT_APPLY_POLICY`, which
``act_on_verdict`` answers with ``ledger.forget(instance_id)``.

The consequence is the orphan-pod hole itself, not a cosmetic one: ``kinoforge
reap --apply`` — and the sweeper daemon CLAUDE.md tells operators to run
unsupervised as a safety net — deleted the row during the multi-minute window in
which it is the ONLY durable handle on a pod that may already be billing. The
1800 s grace window ``cli/_reconcile`` applies to exactly these rows was
bypassed entirely: ``rg`` found zero references to the phase tag anywhere in
``reaper.py`` or ``reaper_actor.py``.

Why a grace window and not a blanket exemption: past the window an unadoptable
launching row IS debris, and on a provider ``cli/_reconcile`` cannot adopt
(Modal's listing exposes no name matchable against a ``run_id``) a blanket
exemption would make the row permanent — the mirror-image hole that
``_age_out_unadoptable_row`` exists to close.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from kinoforge.core.launch_phase import (
    LAUNCH_PHASE_LAUNCHING,
    LAUNCH_PHASE_TAG,
    LAUNCHING_GRACE_S,
)
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict, classify


def _entry(*, id: str, age_s: float, launching: bool, now: float) -> dict[str, Any]:
    """Build a ledger entry aged *age_s* seconds, optionally phase-tagged."""
    tags: dict[str, str] = {"kinoforge_key": "wan-t2v"}
    if launching:
        tags[LAUNCH_PHASE_TAG] = LAUNCH_PHASE_LAUNCHING
    return {
        "id": id,
        "provider": "runpod",
        "tags": tags,
        "created_at": now - age_s,
        "cost_rate_usd_per_hr": 1.0,
    }


def _classify_absent(entry: dict[str, Any], *, now: float) -> Verdict:
    """Classify with the entry's id ABSENT from the live listing.

    That absence is the whole point: a launching row's id is client-side and
    can never appear there, so every launching row takes this path on every
    sweeper tick during boot.
    """
    return classify(
        entry,
        live_pod_ids=frozenset(),
        now=now,
        idle_timeout_s=600.0,
        max_lifetime_s=3_600.0,
        heartbeat_interval_s=10.0,
        grace_after_session_s=60.0,
    )


def test_a_launching_row_inside_its_grace_window_is_not_stale_ledger() -> None:
    """The money bug. A booting pod's only handle must survive the sweeper.

    Bug caught: ``classify`` row 1 returning ``STALE_LEDGER`` for a launching
    row. ``DEFAULT_APPLY_POLICY`` contains ``STALE_LEDGER`` and
    ``act_on_verdict`` answers it with ``ledger.forget`` — so this verdict is
    the difference between a pod that stays findable and a pod that bills
    unattributed until someone reads the provider dashboard.
    """
    now = time.time()
    entry = _entry(id="upscale-20260923-165600", age_s=120.0, launching=True, now=now)

    assert _classify_absent(entry, now=now) == Verdict.LAUNCHING


def test_the_launching_verdict_is_not_acted_on_by_default() -> None:
    """A verdict the apply policy acts on would change nothing.

    Bug caught: adding ``LAUNCHING`` to the enum and then wiring it into
    ``DEFAULT_APPLY_POLICY`` — the row would still be forgotten, just under a
    new name.
    """
    assert Verdict.LAUNCHING not in DEFAULT_APPLY_POLICY.act_verdicts


def test_a_launching_row_past_its_grace_window_is_still_stale_ledger() -> None:
    """Past the window the row IS debris and must still be reapable.

    Bug caught: a blanket launching exemption. On Modal — whose listing
    exposes no name matchable against a ``run_id``, so ``cli/_reconcile``
    can never adopt the row — that would leave a permanent row no branch
    anywhere could clear, which is the hole ``_age_out_unadoptable_row``
    exists to close, re-opened in the reaper.
    """
    now = time.time()
    entry = _entry(
        id="upscale-20260923-165600",
        age_s=LAUNCHING_GRACE_S + 60.0,
        launching=True,
        now=now,
    )

    assert _classify_absent(entry, now=now) == Verdict.STALE_LEDGER


def test_an_untagged_absent_row_is_still_stale_ledger() -> None:
    """Negative control — stale-ledger reaping must keep working.

    Bug caught: a predicate that exempts every row rather than only
    phase-tagged ones, silently disabling the reaping that stops a dead
    pod's ``est_spend`` inflating forever (the 2026-07-06 ~$210 rows).
    """
    now = time.time()
    entry = _entry(id="ordinary-dead-pod", age_s=120.0, launching=False, now=now)

    assert _classify_absent(entry, now=now) == Verdict.STALE_LEDGER


@pytest.mark.parametrize(
    "phase_value",
    ["running", "", "LAUNCHING"],
    ids=["running", "empty", "wrong-case"],
)
def test_a_non_launching_phase_value_is_not_exempt(phase_value: str) -> None:
    """Only the exact persisted value exempts a row.

    Bug caught: testing ``LAUNCH_PHASE_TAG in tags`` rather than its value,
    which would exempt any row the orchestrator ever phase-tags — including
    the ``running`` rows that are precisely what stale-ledger reaping is for.
    ``"LAUNCHING"`` is included because the tag is a wire-format constant
    persisted in ledger JSON: a case-insensitive comparison would be a
    migration hazard dressed as leniency.
    """
    now = time.time()
    entry = _entry(id="phase-tagged", age_s=120.0, launching=False, now=now)
    entry["tags"][LAUNCH_PHASE_TAG] = phase_value

    assert _classify_absent(entry, now=now) == Verdict.STALE_LEDGER


def test_a_live_launching_row_is_not_short_circuited_to_launching() -> None:
    """Once the resource appears in the listing, normal classification resumes.

    Bug caught: returning ``LAUNCHING`` before checking ``pod_up``. On the
    same-key shape (SkyPilot: the cluster name IS the run_id) a launching row
    whose cluster HAS come up would then be exempt from idle and overage
    reaping for the whole grace window — a real billing instance the reaper
    has been told to ignore.
    """
    now = time.time()
    entry = _entry(id="skypilot-cluster-a", age_s=120.0, launching=True, now=now)

    verdict = classify(
        entry,
        live_pod_ids=frozenset({"skypilot-cluster-a"}),
        now=now,
        idle_timeout_s=600.0,
        max_lifetime_s=3_600.0,
        heartbeat_interval_s=10.0,
        grace_after_session_s=60.0,
    )

    assert verdict != Verdict.LAUNCHING


def test_the_reaper_and_the_reconciler_agree_on_the_grace_window() -> None:
    """Both consumers must honour the SAME window, or one of them is wrong.

    Bug caught: drift. ``cli/_reconcile`` deliberately keeps its own copy of
    the launch-phase wire constants rather than importing core on every CLI
    command; that decision is fine, but it means nothing stops the two grace
    windows diverging. If the reaper's were shorter it would delete rows the
    reconciler still considers in-flight — reintroducing U64 in the gap
    between the two numbers.
    """
    from kinoforge.cli import _reconcile

    assert _reconcile._LAUNCHING_GRACE_S == LAUNCHING_GRACE_S
