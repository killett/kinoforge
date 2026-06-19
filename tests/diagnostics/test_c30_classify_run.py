"""Unit tests for ``c30_probe.classify_run`` verdict classifier."""

from __future__ import annotations

from kinoforge.diagnostics.c30_probe import Verdict, classify_run


def test_survived_when_monotonic_and_no_fires() -> None:
    """0 fires + uptime increases monotonically across >=2 samples → SURVIVED."""
    trail = [(0.0, 1), (30.0, 31), (60.0, 61)]
    assert classify_run(trail, fire_count=0) is Verdict.SURVIVED


def test_restarted_when_uptime_resets_and_fires_present() -> None:
    """Uptime drops to a smaller value AND >=3 trap fires → RESTARTED."""
    trail = [(0.0, 1), (30.0, 1), (60.0, 1), (90.0, 1)]
    assert classify_run(trail, fire_count=4) is Verdict.RESTARTED


def test_ambiguous_when_one_or_two_fires() -> None:
    """1-2 fires is below the >=3 threshold and above 0 → AMBIGUOUS."""
    trail = [(0.0, 100), (30.0, 130)]
    assert classify_run(trail, fire_count=2) is Verdict.AMBIGUOUS


def test_ambiguous_when_trail_empty() -> None:
    """No samples at all → cannot decide → AMBIGUOUS."""
    assert classify_run([], fire_count=0) is Verdict.AMBIGUOUS


def test_ambiguous_when_uptime_none() -> None:
    """Poll returned None for uptime (RunPod transient) → AMBIGUOUS."""
    assert classify_run([(0.0, None)], fire_count=0) is Verdict.AMBIGUOUS


def test_ambiguous_when_single_sample_and_no_fires() -> None:
    """Single sample cannot establish monotonicity → AMBIGUOUS."""
    assert classify_run([(0.0, 30)], fire_count=0) is Verdict.AMBIGUOUS


def test_restarted_when_any_uptime_negative() -> None:
    """Non-physical negative uptime is a positive restart signal even with no fires.

    Empirical: C30 A1a run c30-a1a-20260614T222804 returned uptimes like
    ``[6, -2, -15, -11, 0, -4, ...]`` while the S3 EXIT trap never fired
    (pod was killed before ``aws s3 cp`` completed). Negatives are
    non-physical and unambiguously indicate the pod is actively cycling.
    """
    trail = [
        (0.0, None),
        (30.0, 6),
        (60.0, -2),
        (90.0, -15),
        (120.0, -11),
    ]
    assert classify_run(trail, fire_count=0) is Verdict.RESTARTED


def test_restarted_takes_precedence_over_monotonic_appearance() -> None:
    """Even if uptime later climbs again, >=3 fires means RESTARTED."""
    trail = [(0.0, 60), (30.0, 90), (60.0, 1), (90.0, 31)]
    assert classify_run(trail, fire_count=5) is Verdict.RESTARTED


def test_single_negative_in_monotonic_trail_is_not_restarted() -> None:
    """C33 (a) refinement — a single isolated negative-uptime sample in an
    otherwise-monotonic trail with zero fires is platform-incident noise,
    not a restart signal.

    C33 Q3 sweep (16-hour window, 154 samples across 12 GPU types,
    2026-06-13) proved single-sample negatives are platform-incident
    noise. The pre-refinement rule flagged RESTARTED on ANY negative —
    which in production means healthy pods that hit one transient
    negative get reaped, forced cold-boot, and the operator pays the
    setup spend again. Require a corroborating signal (fire_count >= 1
    OR negative_count >= 2) before tripping RESTARTED.
    """
    # Trail otherwise strictly monotonic; one isolated negative at t=60.
    trail = [
        (0.0, 5),
        (30.0, 35),
        (60.0, -1),  # the single platform-incident negative
        (90.0, 95),
        (120.0, 125),
    ]
    assert classify_run(trail, fire_count=0) is Verdict.AMBIGUOUS


def test_two_negatives_no_fires_is_restarted() -> None:
    """C33 (a) refinement — two negatives WITHOUT any S3 fires is the
    multi-sample corroborator that trips RESTARTED. Boundary guard.

    A naive over-cautious refinement that required ``fire_count >= 1 OR
    negative_count >= 3`` would mis-classify this trail as AMBIGUOUS. The
    spec'd threshold is ``negative_count >= 2``.
    """
    trail = [
        (0.0, 5),
        (30.0, -2),
        (60.0, -5),
        (90.0, 5),
    ]
    assert classify_run(trail, fire_count=0) is Verdict.RESTARTED
