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
