"""Tests for the Clock protocol, RealClock, and FakeClock (AC #1–4)."""

import pytest

from kinoforge.core.clock import Clock, FakeClock, RealClock


def test_real_clock_returns_float() -> None:
    """AC #1: RealClock().now() returns a float (Unix epoch seconds)."""
    result = RealClock().now()
    assert isinstance(result, float)
    assert result > 0.0


def test_fake_clock_initial_value() -> None:
    """AC #2a: FakeClock(start=0.0).now() == 0.0 before any advance."""
    clock = FakeClock(start=0.0)
    assert clock.now() == 0.0


def test_fake_clock_advance() -> None:
    """AC #2b: After advance(60.0), now() == 60.0 — deterministic."""
    clock = FakeClock(start=0.0)
    clock.advance(60.0)
    assert clock.now() == 60.0


def test_fake_clock_advance_negative_raises() -> None:
    """AC #3: FakeClock.advance(s) raises ValueError on negative s."""
    clock = FakeClock(start=100.0)
    with pytest.raises(ValueError, match="negative"):
        clock.advance(-1.0)


def test_clock_protocol_isinstance() -> None:
    """AC #4: Both RealClock and FakeClock satisfy the Clock runtime Protocol."""
    assert isinstance(RealClock(), Clock)
    assert isinstance(FakeClock(start=0.0), Clock)
