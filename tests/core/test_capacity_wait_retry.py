"""Capacity-wait retry: re-query offers + retry create on CapacityError."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import CapacityError
from kinoforge.core.orchestrator import _create_with_capacity_wait


class _Clock:
    def __init__(self, times: list[float]) -> None:
        self._times = times
        self._i = 0

    def now(self) -> float:
        t = self._times[min(self._i, len(self._times) - 1)]
        self._i += 1
        return t


def test_retries_then_succeeds() -> None:
    # Bug caught: a transient capacity miss fails the whole run instead of
    # riding the ~seconds-to-minutes drought RunPod recovers from.
    query_calls = {"n": 0}

    def find_offers() -> list[str]:
        query_calls["n"] += 1
        return ["offer"]  # non-empty

    attempts = {"n": 0}

    def create(_offers: list[str]) -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise CapacityError("no capacity")
        return "instance-ok"

    result = _create_with_capacity_wait(
        find_offers=find_offers,
        create=create,
        capacity_wait_s=300.0,
        retry_interval_s=25.0,
        clock=_Clock([0.0, 10.0, 20.0, 30.0]),
        sleep=lambda _s: None,
    )
    assert result == "instance-ok"
    assert attempts["n"] == 3
    assert query_calls["n"] == 3  # re-queried offers each attempt


def test_zero_wait_fails_on_first_miss() -> None:
    # Bug caught: capacity_wait=0 (smoke) still hangs retrying.
    def create(_offers: list[str]) -> str:
        raise CapacityError("no capacity")

    with pytest.raises(CapacityError):
        _create_with_capacity_wait(
            find_offers=lambda: ["offer"],
            create=create,
            capacity_wait_s=0.0,
            retry_interval_s=25.0,
            clock=_Clock([0.0, 1.0]),
            sleep=lambda _s: None,
        )


def test_deadline_exceeded_reraises() -> None:
    # Bug caught: an infinite loop when capacity never returns.
    def create(_offers: list[str]) -> str:
        raise CapacityError("still no capacity")

    with pytest.raises(CapacityError):
        _create_with_capacity_wait(
            find_offers=lambda: ["offer"],
            create=create,
            capacity_wait_s=60.0,
            retry_interval_s=25.0,
            clock=_Clock([0.0, 30.0, 61.0, 62.0]),
            sleep=lambda _s: None,
        )


def test_non_capacity_error_propagates() -> None:
    # Bug caught: a hard create error (auth/schema) is swallowed as retryable.
    def create(_offers: list[str]) -> str:
        raise RuntimeError("bad schema")

    with pytest.raises(RuntimeError):
        _create_with_capacity_wait(
            find_offers=lambda: ["offer"],
            create=create,
            capacity_wait_s=300.0,
            retry_interval_s=25.0,
            clock=_Clock([0.0, 10.0]),
            sleep=lambda _s: None,
        )
