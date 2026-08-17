"""Capability-aware Row-7 gate (Brief 2, Task 6).

The Row-7 gate fires only when a ledger row carries no heartbeat fields,
which a running ``HeartbeatLoop`` always writes. The rows that actually
reach it are the ones written outside a heartbeat loop: ephemeral index
rows, cross-process warm rows, and the provisional
``kf_launch_phase=launching`` row written before ``sky.launch``.

Before this task the gate returned EARLY on an expected absence, so the
age + grace evidence below it — which never depended on heartbeat — was
never consulted and such a row was a permanent dead end. These tests pin
the narrowed contract: expected absence falls through to grace UNDER A
LIVENESS PRECONDITION, declared capability + missing fields stays the
strict anomaly.

The liveness precondition (design §8) is the safety half. Rows 5 & 6 earn
the right to read age as orphanhood by first proving the driver is dead
(``sent_age > sentinel_window``); this call site has no such proof, only
absent fields. Without the precondition an actively-generating pod on a
capability-less provider ages into ORPHAN_REAP and is destroyed
mid-render by a sweeper running ``include_orphans``.

The precondition is POSITIVE evidence of a closed session
(``session_end`` present) plus ``not is_session_busy``. The second half
alone is not enough, and the tests below are anchored to row shapes
production actually emits: whenever this gate is reachable with a live
driver — heartbeat disabled, or the pre-loop launch window — the row
carries no ``session_start``, because its only writer sits inside the
heartbeat-loop branch. A test built on a row no code path can produce is
not coverage of anything.
"""

from __future__ import annotations

from typing import Any

from kinoforge.core.reaper import (
    DEFAULT_APPLY_POLICY,
    Verdict,
    classify,
    partition,
    policy_from_cli_flags,
)

NOW = 1_800_000_000.0
GRACE = 1800.0


def _row(
    provider: str, *, session_end_age: float, pod_age: float | None = None
) -> dict[str, Any]:
    """Ledger row with NO heartbeat fields (written outside a heartbeat loop).

    ``pod_age`` defaults to one minute older than ``session_end_age`` so the
    pod predates its own detach marker, as a real row always does. A fixed
    small default would silently trip the ``max(created_at, session_end)``
    out-of-order guard on every past-grace case and clamp the measurement
    back to ``pod_age`` — making a test that means to exercise grace
    exercise the guard instead.

    Args:
        provider: Provider kind written under the Ledger.record key
            ``"provider"``.
        session_end_age: Seconds between the last driver detach and
            :data:`NOW`.
        pod_age: Seconds since ``created_at``. Defaults to
            ``session_end_age + 60``.

    Returns:
        A ledger-shaped dict carrying neither ``heartbeat_thread_tick``
        nor ``last_heartbeat``.
    """
    if pod_age is None:
        pod_age = session_end_age + 60.0
    return {
        "id": "i-1",
        "provider": provider,
        "created_at": NOW - pod_age,
        "session_end": NOW - session_end_age,
        "grace_after_session_s": GRACE,
    }


def _classify(
    entry: dict[str, Any],
    *,
    now: float = NOW,
    heartbeat_interval_s: float | None = 30.0,
) -> Verdict:
    """Classify ``entry`` with the row live and cfg-shaped thresholds.

    ``grace_after_session_s`` is passed as :data:`GRACE` so it matches the
    per-row override :func:`_row` writes — a reader can derive the boundary
    every test in this module works against from either place.

    Args:
        entry: Ledger-shaped dict under test.
        now: Wall-clock seconds passed through to ``classify``.
        heartbeat_interval_s: Cfg heartbeat cadence. ``None`` models
            ``compute.heartbeat_mode: none``, which makes the row-7 gate
            condition true for every row permanently.

    Returns:
        The verdict ``classify`` assigns.
    """
    return classify(
        entry,
        live_pod_ids={str(entry["id"])},
        now=now,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=heartbeat_interval_s,
        grace_after_session_s=GRACE,
    )


def test_expected_absence_past_grace_reaps_as_orphan() -> None:
    """Catches the dead-end verdict: a row with no heartbeat substrate must
    still be judged on the age evidence it does have."""
    verdict = _classify(_row("skypilot", session_end_age=GRACE + 1))
    assert verdict is Verdict.ORPHAN_REAP


def test_expected_absence_within_grace_is_unchanged() -> None:
    """Boundary partner — inside grace the row is not an orphan."""
    verdict = _classify(_row("skypilot", session_end_age=GRACE - 1))
    assert verdict is Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_expected_absence_exactly_at_grace_is_not_an_orphan() -> None:
    """Grace is a strict-``>`` comparison in rows 5 & 6; the fall-through
    must use the same boundary. Catches a ``>=`` slipping in, which would
    reap a row the instant it touched the window instead of past it."""
    verdict = _classify(_row("skypilot", session_end_age=GRACE))
    assert verdict is Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_unexpected_absence_stays_strict() -> None:
    """Provider DECLARES a heartbeat read and the row has none — a real
    anomaly, not an expected gap. Catches an inverted capability check."""
    verdict = _classify(_row("runpod", session_end_age=GRACE + 1))
    assert verdict is Verdict.HEARTBEAT_UNKNOWN


def test_legacy_row_without_provider_stays_unknown() -> None:
    """A pre-Layer-S row carries neither ``provider`` nor ``provider_kind``,
    so the absence cannot be shown to be expected. Catches the fall-through
    being applied to rows whose provider is simply unidentifiable."""
    entry = _row("skypilot", session_end_age=GRACE + 1)
    del entry["provider"]
    assert _classify(entry) is Verdict.HEARTBEAT_UNKNOWN


def test_missing_session_end_is_never_an_orphan_at_any_age() -> None:
    """A row with no ``session_end`` has no evidence a session ever closed,
    only that it is old. Neither age makes it reapable.

    Supersedes an earlier version of this test that asserted the ancient
    half becomes ORPHAN_REAP via the ``pod_age`` fallback — the round-2
    re-review showed that is precisely the live-pod destroy path, because
    a driver that is mid-render writes no ``session_end`` until teardown.
    """
    young = _row("skypilot", session_end_age=0.0, pod_age=GRACE - 1)
    del young["session_end"]
    assert _classify(young) is Verdict.HEARTBEAT_SUBSTRATE_MISSING

    ancient = _row("skypilot", session_end_age=0.0, pod_age=GRACE + 1)
    del ancient["session_end"]
    assert _classify(ancient) is Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_out_of_order_session_end_measures_from_creation() -> None:
    """``max(created_at, session_end)`` guards a corrupt write where the
    detach marker predates the pod. A young pod with an ancient
    ``session_end`` must be judged on its own age. Catches the shared
    grace helper dropping the guard."""
    entry = _row("skypilot", session_end_age=100_000.0, pod_age=10.0)
    assert _classify(entry) is Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_overage_still_precedes_the_gate() -> None:
    """Catches the fall-through being inserted above the age check."""
    row = _row("skypilot", session_end_age=1.0, pod_age=100_000.0)
    row["max_lifetime_s"] = 3600.0
    assert _classify(row) is Verdict.OVERAGE_REAP


def test_dead_row_still_precedes_the_gate() -> None:
    """Row 1 (STALE_LEDGER) outranks the gate: a row the provider no longer
    reports live is a ledger-cleanup case, not an orphan destroy."""
    entry = _row("skypilot", session_end_age=GRACE + 1)
    verdict = classify(
        entry,
        live_pod_ids=frozenset(),
        now=NOW,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert verdict is Verdict.STALE_LEDGER


# ---------------------------------------------------------------------------
# Liveness precondition (design §8, added after the Task 6 review)
# ---------------------------------------------------------------------------


def test_heartbeat_disabled_live_pod_past_grace_is_not_an_orphan() -> None:
    """THE regression this precondition exists to prevent, in the exact
    shape production emits.

    ``compute.heartbeat_mode: none`` (the default) leaves
    ``heartbeat_interval_s=None``, so the row-7 gate condition is true for
    every row forever. No heartbeat loop runs, so nothing writes
    ``session_start`` — its only writer, ``orchestrator.py:1510-1514``,
    sits inside the ``hb_loop is not None`` branch. And ``session_end`` is
    written only at teardown. An actively-generating pod is therefore a
    bare row with NEITHER claim field, measuring grace from ``pod_age``,
    which sails past 1800 s mid-render.

    An earlier fix guarded this with ``is_session_busy`` alone; on this row
    that returns False and the guard never fired. Requiring positive
    evidence of a CLOSED session is what makes it fire. If this test fails
    with ORPHAN_REAP, a sweeper running ``--include-orphans`` destroys live
    pods mid-render.
    """
    entry = {
        "id": "i-1",
        "provider": "skypilot",
        "created_at": NOW - 2_000.0,  # past the 1800 s grace on pod_age
    }
    assert _classify(entry, heartbeat_interval_s=None) is (
        Verdict.HEARTBEAT_SUBSTRATE_MISSING
    )


def test_launching_row_past_grace_is_not_an_orphan() -> None:
    """The provisional ``kf_launch_phase=launching`` row is written before
    ``sky.launch`` and carries no session fields at all. A slow launch
    (``--retry-until-up`` can exceed 30 minutes) must not make the cluster
    it is still provisioning reapable."""
    entry = {
        "id": "i-1",
        "provider": "skypilot",
        "created_at": NOW - 2_000.0,
        "kf_launch_phase": "launching",
    }
    assert _classify(entry) is Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_closed_session_past_grace_is_still_an_orphan() -> None:
    """AC1 must survive the precondition. A cross-process warm row from a
    completed session carries ``session_end`` — positive evidence the
    session closed — so the age evidence stands and the row is reapable
    again. This is the shape the whole task exists to unstrand; catches a
    guard so wide that nothing is ever reapable."""
    entry = _row("skypilot", session_end_age=GRACE + 1)
    entry["session_start"] = entry["session_end"] - 100.0  # closed cleanly
    assert _classify(entry) is Verdict.ORPHAN_REAP


def test_reopened_session_with_heartbeat_disabled_is_not_an_orphan() -> None:
    """The second condition, on a row production can emit: an older session
    closed (``session_end``), a later one reopened the row
    (``session_start`` newer) and did not close it, and the reap runs from a
    cfg with heartbeat disabled — where ``is_session_busy`` trusts the
    marker. ``session_end`` alone would clear this row for reaping; the
    ``is_session_busy`` half is what holds it."""
    entry = _row("skypilot", session_end_age=GRACE + 1)
    entry["session_start"] = float(entry["session_end"]) + 10.0  # reopened
    assert _classify(entry, heartbeat_interval_s=None) is (
        Verdict.HEARTBEAT_SUBSTRATE_MISSING
    )


def test_unregistered_provider_name_stays_unknown() -> None:
    """``capabilities_for`` returns an empty set both for "declares no
    HEARTBEAT_READ" and for "name we do not recognise"; only the first is
    an EXPECTED absence. A typo'd or third-party provider name must not be
    handed the fall-through on the strength of a declaration it never
    made."""
    entry = _row("not-a-registered-provider", session_end_age=GRACE + 1)
    assert _classify(entry) is Verdict.HEARTBEAT_UNKNOWN


# ---------------------------------------------------------------------------
# Policy — both directions
# ---------------------------------------------------------------------------


def test_default_policy_does_not_act_on_the_new_orphans() -> None:
    """Catches ORPHAN_REAP being added to the default policy while wiring
    this, which would silently make the change destructive."""
    to_act, to_skip = partition({"i-1": Verdict.ORPHAN_REAP}, DEFAULT_APPLY_POLICY)
    assert to_act == {}
    assert to_skip == {"i-1": Verdict.ORPHAN_REAP}


def test_include_orphans_now_acts_on_a_past_grace_expected_absence_row() -> None:
    """The DESTRUCTIVE direction, stated explicitly rather than implied.

    This classifier holds destroy authority, so the change must be pinned
    from the side that ends in a destroy, not only from the safe side. An
    operator who asks for orphan reaping now gets this row: it is fed
    through the real ``classify`` and the real
    ``policy_from_cli_flags(apply=True, include_orphans=True)``, and lands
    in ``to_act``. Before this task the same row produced
    HEARTBEAT_SUBSTRATE_MISSING, which no policy can act on — hence the
    plain-``--apply`` half, which must stay empty.
    """
    verdict = _classify(_row("skypilot", session_end_age=GRACE + 1))
    assert verdict is Verdict.ORPHAN_REAP

    opted_in = policy_from_cli_flags(apply=True, include_orphans=True)
    to_act, to_skip = partition({"i-1": verdict}, opted_in)
    assert to_act == {"i-1": Verdict.ORPHAN_REAP}
    assert to_skip == {}

    plain_apply = policy_from_cli_flags(apply=True)
    assert partition({"i-1": verdict}, plain_apply) == ({}, {"i-1": verdict})
