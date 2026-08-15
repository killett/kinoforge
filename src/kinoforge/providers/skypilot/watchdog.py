"""Instance-side deadline watchdog for SkyPilot clusters.

SkyPilot's ``idle_minutes_to_autostop`` is inert for kinoforge's server-mode
deploys: ``spec.run_cmd`` becomes ``Task.run``, which is submitted as a
cluster job and never terminates, so ``job_lib.is_cluster_idle()`` is
permanently False and the 60 s ``AutostopEvent`` tick resets the idleness
timer forever (finding F1,
``docs/superpowers/research/2026-08-15-cloud-layer-findings-verification.md``).

This module ports the RunPod in-pod self-terminator model
(:mod:`kinoforge.providers.runpod.selfterm`) to SkyPilot: a wall-clock
deadline, armed at the TOP of ``Task.setup`` before the heavy installs, and
enforced by a small python daemon running ON the instance. It therefore
survives the orchestrator process dying — the failure mode
``idle_minutes_to_autostop`` was believed to cover and does not.

Design: ``docs/superpowers/specs/2026-08-15-skypilot-instance-deadline-watchdog-design.md``.
"""

from __future__ import annotations

_SECONDS_PER_HOUR: float = 3600.0


def compute_deadline(
    *,
    launch_epoch: float,
    max_lifetime_s: float,
    budget_usd: float = 0.0,
    rate_usd_per_hr: float = 0.0,
) -> float:
    """Return the absolute POSIX epoch after which the instance must be dead.

    The deadline is measured from ``launch_epoch`` rather than from instance
    boot, so it also bounds the multi-minute provisioning window — the window
    in which a controller death leaves a billing cluster with no durable
    record (finding F12).

    No ``time_buffer_s`` term is applied: that buffer is a controller-side
    reap concept, and folding it in here would make the instance-side
    backstop tighter than the configured policy, turning a backstop into a
    scheduler.

    Args:
        launch_epoch: POSIX timestamp taken immediately before ``sky.launch``.
        max_lifetime_s: ``Lifecycle.max_lifetime_s`` — the hard ceiling.
        budget_usd: ``Lifecycle.budget_usd``. Ignored when non-positive
            (0.0 is the dataclass default and must not mean "die now").
        rate_usd_per_hr: The offer's hourly rate. Ignored when non-positive
            (an unknown rate cannot bound anything).

    Returns:
        The earlier of the lifetime bound and, when both ``budget_usd`` and
        ``rate_usd_per_hr`` are positive, the budget bound.

    Raises:
        ValueError: ``max_lifetime_s`` is not positive.

    Example:
        >>> compute_deadline(launch_epoch=1000.0, max_lifetime_s=18000.0)
        19000.0
    """
    if max_lifetime_s <= 0:
        raise ValueError(f"max_lifetime_s must be positive, got {max_lifetime_s!r}")
    deadline = launch_epoch + max_lifetime_s
    if budget_usd > 0 and rate_usd_per_hr > 0:
        budget_bound = launch_epoch + (budget_usd / rate_usd_per_hr) * _SECONDS_PER_HOUR
        deadline = min(deadline, budget_bound)
    return deadline
