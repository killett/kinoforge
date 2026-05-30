"""Clock protocol and implementations: RealClock (wall time) + FakeClock (test seam).

The ``Clock`` protocol is the injection point for cost-safety logic (Tasks 17/18);
inject a ``FakeClock`` in tests to step time deterministically without real sleeps.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A minimal wall-clock abstraction; the sole injection point for time.

    Attributes:
        (none — pure protocol)
    """

    def now(self) -> float:
        """Return the current time as a Unix epoch float (seconds).

        Returns:
            Current time in seconds since the Unix epoch.
        """
        ...


class RealClock:
    """Wall-clock implementation backed by ``time.time()``.

    Example:
        >>> import time
        >>> c = RealClock()
        >>> isinstance(c.now(), float)
        True
    """

    def now(self) -> float:
        """Return the current Unix epoch time.

        Returns:
            ``time.time()`` — seconds since the Unix epoch.
        """
        return time.time()


class FakeClock:
    """Deterministic clock for tests; time advances only when told to.

    Args:
        start: Initial time value (default ``0.0``).

    Example:
        >>> c = FakeClock(start=100.0)
        >>> c.now()
        100.0
        >>> c.advance(30.0)
        >>> c.now()
        130.0
    """

    def __init__(self, start: float = 0.0) -> None:
        """Initialise the fake clock at ``start`` seconds."""
        self._t = start

    def now(self) -> float:
        """Return the current (simulated) time.

        Returns:
            The simulated Unix epoch time in seconds.
        """
        return self._t

    def advance(self, s: float) -> None:
        """Advance the clock by ``s`` seconds.

        Args:
            s: Seconds to advance. Must be non-negative.

        Raises:
            ValueError: If ``s`` is negative (clocks don't go backwards).
        """
        if s < 0:
            raise ValueError(f"advance() requires non-negative seconds; got {s}")
        self._t += s
