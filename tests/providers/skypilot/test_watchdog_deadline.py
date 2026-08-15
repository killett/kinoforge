"""Deadline math for the SkyPilot instance-side watchdog.

Behaviour under test: :func:`compute_deadline` turns a launch timestamp plus
lifecycle policy into the absolute epoch after which the instance must be
dead. The deadline is LAUNCH-relative (so it covers the provisioning window,
which is the F12 failure window) and carries no ``time_buffer_s`` term.
"""

from __future__ import annotations

import pytest

from kinoforge.providers.skypilot.watchdog import compute_deadline


def test_deadline_is_launch_plus_max_lifetime_when_rate_unknown() -> None:
    """With no rate, the deadline is exactly launch + max_lifetime_s.

    A bug this catches: reintroducing a ``time_buffer_s`` term (which would
    return 17_200.0 at Lifecycle defaults) or measuring from boot instead of
    from launch.
    """
    assert compute_deadline(launch_epoch=1000.0, max_lifetime_s=18000.0) == 19000.0


def test_budget_bound_wins_when_tighter_than_lifetime() -> None:
    """$0.10 at $1.00/hr caps the instance at 360 s regardless of max_lifetime.

    A bug this catches: the budget bound computed but discarded, or combined
    with ``max`` instead of ``min`` — either leaves a 5 h deadline on a
    cluster the budget can only afford for 6 minutes.
    """
    got = compute_deadline(
        launch_epoch=1000.0,
        max_lifetime_s=18000.0,
        budget_usd=0.10,
        rate_usd_per_hr=1.00,
    )
    assert got == pytest.approx(1360.0)


def test_lifetime_bound_wins_when_budget_bound_is_looser() -> None:
    """A generous budget must not extend the deadline past max_lifetime_s.

    A bug this catches: returning the budget bound unconditionally once a
    rate is known.
    """
    got = compute_deadline(
        launch_epoch=1000.0,
        max_lifetime_s=18000.0,
        budget_usd=100.0,
        rate_usd_per_hr=1.00,
    )
    assert got == 19000.0


@pytest.mark.parametrize(
    ("budget_usd", "rate_usd_per_hr"),
    [(0.0, 1.0), (0.10, 0.0), (0.0, 0.0), (-1.0, 1.0), (0.10, -1.0)],
)
def test_budget_bound_ignored_when_budget_or_rate_non_positive(
    budget_usd: float, rate_usd_per_hr: float
) -> None:
    """Zero/negative budget or rate drops the budget bound entirely.

    A bug this catches: ``Lifecycle.budget_usd`` defaults to 0.0, so a naive
    ``budget/rate`` would make every default-config cluster self-destruct on
    the first watchdog tick; a zero rate would raise ZeroDivisionError inside
    create_instance.
    """
    got = compute_deadline(
        launch_epoch=1000.0,
        max_lifetime_s=18000.0,
        budget_usd=budget_usd,
        rate_usd_per_hr=rate_usd_per_hr,
    )
    assert got == 19000.0


def test_non_positive_max_lifetime_raises() -> None:
    """A zero/negative max_lifetime_s is a caller bug, not a 'die now' order.

    A bug this catches: silently rendering an already-expired deadline, which
    would kill every cluster ~15 s into setup.
    """
    with pytest.raises(ValueError, match="max_lifetime_s"):
        compute_deadline(launch_epoch=1000.0, max_lifetime_s=0.0)
