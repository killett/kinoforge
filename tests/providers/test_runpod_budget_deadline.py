"""Behavior: `lifecycle.budget` bounds a RunPod pod in DOLLARS, not just time.

U51. Only SkyPilot's watchdog consumed `lifecycle.budget`. RunPod's
`ON_INSTANCE_DEADLINE` is ``min(2 * idle_timeout, max_lifetime - time_buffer)``
and never referenced it, so a RunPod cfg carrying ``budget: 0.50`` was bounded
by TIME only — while reading, to anyone scanning the cfg, like a spend guard.

It was found while assessing whether raising ``max_usd_per_hr`` 0.40 -> 0.60 on
the Tier-3 smoke cfg was safe. It was — but because the 20-minute deadline caps
the worst case at ~$0.20/run, NOT because the budget field did anything.

The fix enforces rather than warns, which is available on RunPod specifically
because it declares ``Capability.RATE_DETERMINISTIC``: the requested SKU is the
billed SKU, so the booked offer's ``cost_rate_usd_per_hr`` IS the rate, known at
create time. (SkyPilot cannot do this — it declares ``RATE_READBACK``, the rate
being knowable only after launch, which is why its watchdog does the arithmetic
on the instance instead.) The budget is converted to a wall-clock cap and folded
into the deadline with ``min``, so it can only ever SHORTEN a pod's life.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer
from kinoforge.providers.runpod import RunPodProvider


def _offer(rate: float) -> Offer:
    return Offer(
        id="NVIDIA GeForce RTX 4090",
        gpu_type="NVIDIA GeForce RTX 4090",
        vram_gb=24,
        cuda="12.8",
        cost_rate_usd_per_hr=rate,
    )


def _spec(*, budget: float, max_lifetime: float = 7200.0) -> InstanceSpec:
    return InstanceSpec(
        run_id="budget-test",
        image="img:latest",
        lifecycle=Lifecycle(
            idle_timeout_s=3600.0,
            max_lifetime_s=max_lifetime,
            time_buffer_s=0.0,
            budget_usd=budget,
        ),
    )


def _rendered_max_lifetime(env: dict[str, str]) -> float:
    """Pull `_MAX_LIFETIME` back out of the embedded self-terminator script."""
    script = env["KINOFORGE_SELFTERM_SCRIPT"]
    m = re.search(r"^_MAX_LIFETIME:\s*float\s*=\s*([0-9.]+)", script, re.M)
    assert m, f"no _MAX_LIFETIME in rendered selfterm:\n{script[:400]}"
    return float(m.group(1))


def _env(provider: RunPodProvider, spec: InstanceSpec, offer: Offer | None) -> Any:
    return provider._assemble_create_env(spec, offer=offer)


def test_a_budget_shortens_the_pods_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect. $0.50 at $1.00/hr is half an hour, not the cfg's two.

    Bug caught: `budget_usd` never reaching the self-terminator, leaving the
    pod bounded only by `max_lifetime`. An operator reading `budget: 0.50` in
    the cfg believes a dollar ceiling is in force; nothing enforced one, and
    the run could bill 4x that before the time cap fired.

    Expected value is hand-computed, not read off the code: 0.50 USD / 1.00
    USD-per-hour = 0.5 h = 1800 s.
    """
    provider = RunPodProvider()

    env = _env(provider, _spec(budget=0.50), _offer(1.00))

    assert _rendered_max_lifetime(env) == 1800.0


def test_a_budget_larger_than_the_time_cap_does_not_extend_the_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard must not become a licence.

    Bug caught: assigning the budget-derived lifetime instead of taking the
    `min`. At $1.00/hr a $10 budget is ten hours — writing that over a
    two-hour `max_lifetime` would let a cfg's own time ceiling be silently
    overridden by a field whose entire purpose is to be a ceiling.
    """
    provider = RunPodProvider()

    env = _env(provider, _spec(budget=10.0, max_lifetime=7200.0), _offer(1.00))

    assert _rendered_max_lifetime(env) == 7200.0


def test_the_default_zero_budget_does_not_cap_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control, and the one that would be catastrophic to get wrong.

    `budget_usd` defaults to 0.0 and means "no budget declared", NOT "a
    zero-dollar budget".

    Bug caught: treating 0 as a real budget. 0 / rate = 0 seconds, so every
    pod in the project would self-terminate the instant it booted — every
    run broken, on a field almost no cfg sets.
    """
    provider = RunPodProvider()

    env = _env(provider, _spec(budget=0.0, max_lifetime=7200.0), _offer(1.00))

    assert _rendered_max_lifetime(env) == 7200.0


@pytest.mark.parametrize("rate", [0.0, -1.0], ids=["zero", "negative"])
def test_an_unusable_rate_leaves_the_deadline_alone(rate: float) -> None:
    """A rate we cannot divide by is not a licence to guess.

    Bug caught: dividing unconditionally — `ZeroDivisionError` at create time
    on a provider whose catalog occasionally reports an unpriced row (U49's
    `unknown` GPU type had both prices null), turning a bookkeeping gap into
    a failed launch.
    """
    provider = RunPodProvider()

    env = _env(provider, _spec(budget=0.50, max_lifetime=7200.0), _offer(rate))

    assert _rendered_max_lifetime(env) == 7200.0


def test_no_offer_leaves_the_deadline_alone() -> None:
    """Callers that cannot supply an offer must not be broken by this.

    Bug caught: making `offer` required. `_assemble_create_env` has callers
    and tests that pass only a spec; a required parameter would turn a
    lifetime refinement into a signature break.
    """
    provider = RunPodProvider()

    env = _env(provider, _spec(budget=0.50, max_lifetime=7200.0), None)

    assert _rendered_max_lifetime(env) == 7200.0


def test_the_load_time_advisory_mentions_the_budget_arm() -> None:
    """The cfg-load message must stop implying budget does nothing on RunPod.

    At load no offer is chosen, so the rate is unknown and the advisory
    cannot print a number — that is why U51's filing said the budget arm is
    "not knowable here". But it can say the arm EXISTS, which is the whole
    point of the item: the field used to read like a spend guard that was
    not one, and a message naming only the time formula reproduces exactly
    that impression now that it IS one.

    Bug caught: fixing the enforcement and leaving the operator-facing text
    describing the old behaviour — the same class of staleness U53 found in
    CLAUDE.md's rotted headroom note.
    """
    from kinoforge.core.config import LifecycleConfig
    from kinoforge.validation.checks.capabilities import _runpod_deadline_phrase

    with_budget = _runpod_deadline_phrase(
        LifecycleConfig(idle_timeout=3600, max_lifetime=7200, time_buffer=0, budget=0.5)
    )
    without = _runpod_deadline_phrase(
        LifecycleConfig(idle_timeout=3600, max_lifetime=7200, time_buffer=0, budget=0.0)
    )

    assert "budget" in with_budget
    assert "budget" not in without, (
        "a cfg that declares no budget must not be told about a budget arm"
    )
