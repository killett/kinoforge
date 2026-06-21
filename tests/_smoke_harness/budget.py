"""BudgetTracker — live-rate × wall-clock cap assertion."""

from __future__ import annotations

import time


def _get_cost_rate(pod_id: str) -> float:
    """Test-seam — overridden in unit tests."""
    from kinoforge.core import registry as kf_registry
    from kinoforge.providers import runpod  # noqa: F401

    provider = kf_registry.get_provider("runpod")()
    instance = provider.get_instance(pod_id)
    return float(instance.cost_rate_usd_per_hr)


class BudgetTracker:
    """Cumulative-spend cap asserter.

    Spend is approximated as ``live_cost_rate × elapsed_hours`` —
    accurate enough as a smoke-side post-condition. The pod-side
    selfterm watcher is the actual safety net; this is the "fail
    loud during teardown so a regression is obvious" surface.
    """

    def __init__(self, *, cap_usd: float, pod_id: str) -> None:
        self.cap_usd = cap_usd
        self.pod_id = pod_id
        self._start_ts = time.time()

    def assert_under_cap(self) -> None:
        rate = _get_cost_rate(self.pod_id)
        elapsed_hours = (time.time() - self._start_ts) / 3600.0
        spend = rate * elapsed_hours
        assert spend < self.cap_usd, (
            f"smoke spend ${spend:.2f} > cap ${self.cap_usd:.2f} — "
            f"rate=${rate:.2f}/hr, elapsed={elapsed_hours * 60:.1f}min"
        )
