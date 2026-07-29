"""Provider ready-poll must sleep between polls and honour boot_timeout_s.

Audit B5: both orchestrator ready-polls waited on
``provider.get_instance`` in a bare ``while instance.status != "ready"``
loop. ``deploy()``'s copy had no sleep at all — a stuck-"starting" pod
turned into a hot loop hammering the provider API — and neither copy
enforced ``boot_timeout_s``, so a pod that never leaves "starting"
(image-pull hang, host wedged) held the CLI forever while billing.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import ProvisionTimeout
from kinoforge.core.interfaces import Instance
from kinoforge.core.orchestrator import _wait_for_provider_ready


class _FakeClock:
    """Monotonic clock advanced only by the injected sleep seam."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.slept.append(secs)
        self.t += secs


class _StatusProvider:
    """Returns a scripted status sequence; last value repeats forever."""

    def __init__(self, statuses: list[str]) -> None:
        self._statuses = statuses
        self.calls = 0

    def get_instance(self, instance_id: str) -> Instance:
        self.calls += 1
        idx = min(self.calls - 1, len(self._statuses) - 1)
        return Instance(
            id=instance_id,
            provider="fake",
            status=self._statuses[idx],
            created_at=0.0,
            tags={},
            cost_rate_usd_per_hr=0.0,
        )


def _starting() -> Instance:
    return Instance(
        id="pod-1",
        provider="fake",
        status="starting",
        created_at=0.0,
        tags={},
        cost_rate_usd_per_hr=0.0,
    )


def test_never_ready_raises_provision_timeout_at_the_deadline() -> None:
    """A pod stuck in "starting" ends the wait at ``boot_timeout_s``.

    Bug caught: the pre-fix loop had no deadline, so a wedged pod held
    the CLI indefinitely — the operator's only signal was the bill.
    """
    clock = _FakeClock()
    provider = _StatusProvider(["starting"])

    with pytest.raises(ProvisionTimeout) as excinfo:
        _wait_for_provider_ready(
            provider,  # type: ignore[arg-type]
            _starting(),
            boot_timeout_s=10.0,
            clock=clock,
            sleep=clock.sleep,
            interval_s=2.0,
        )

    assert "pod-1" in str(excinfo.value)
    # 10 s budget at a 2 s interval: 5 polls, then the deadline check
    # ends it. Pins that the bound is real, not a very large number.
    assert provider.calls == 5
    assert clock.slept == [2.0] * 5


def test_sleeps_between_polls_instead_of_busy_spinning() -> None:
    """Each re-poll is preceded by the poll interval.

    Bug caught: ``deploy()``'s copy called ``get_instance`` with no sleep
    whatsoever — thousands of API calls per second against the provider
    while a pod booted, which is rate-limit (and ban) territory.
    """
    clock = _FakeClock()
    provider = _StatusProvider(["starting", "ready"])

    status = _wait_for_provider_ready(
        provider,  # type: ignore[arg-type]
        _starting(),
        boot_timeout_s=600.0,
        clock=clock,
        sleep=clock.sleep,
        interval_s=2.0,
    )

    assert status == "ready"
    assert provider.calls == 2
    assert clock.slept == [2.0, 2.0]


def test_already_ready_instance_never_polls_or_sleeps() -> None:
    """LocalProvider's synchronous-ready path stays free.

    Bug caught: a fix that unconditionally sleeps before the first check
    adds a fixed delay to every local/dry run and to warm-attach paths
    that hand in an already-ready instance.
    """
    clock = _FakeClock()
    provider = _StatusProvider(["ready"])
    ready = Instance(
        id="pod-1",
        provider="fake",
        status="ready",
        created_at=0.0,
        tags={},
        cost_rate_usd_per_hr=0.0,
    )

    assert (
        _wait_for_provider_ready(
            provider,  # type: ignore[arg-type]
            ready,
            boot_timeout_s=600.0,
            clock=clock,
            sleep=clock.sleep,
        )
        == "ready"
    )
    assert provider.calls == 0
    assert clock.slept == []
