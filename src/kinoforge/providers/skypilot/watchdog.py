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

from string import Template

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


#: On-instance watchdog program. ``$``-placeholders are substituted by
#: :func:`RENDER_WATCHDOG`; the script itself contains no other ``$``.
_WATCHDOG_TEMPLATE = Template(
    '''\
#!/usr/bin/env python3
"""Kinoforge SkyPilot instance-side deadline watchdog.

Polls a deadline file every $poll_interval_s seconds. Once the deadline has
passed it terminates the instance in two stages:

  stage 1  ask the on-node skylet to autodown NOW (idle_minutes=0,
           wait_for=NONE, down=True). This is a REAL cloud terminate: it
           runs through SkyPilot's own provisioner using the credentials
           SkyPilot already placed on this node. Instance and disk go away
           and sky's cluster state stays consistent.
  stage 2  after $grace_before_halt_s seconds without dying, fall back to
           `sudo shutdown -h now`. Credential-free and local. NOTE: a halt
           is not a terminate — on AWS/GCP the instance stops and its disk
           keeps billing, and on rented-GPU clouds (Lambda, Vast) billing
           continues at the full rate. Stage 2 is the last resort, not the
           plan.

The deadline is re-read on EVERY tick, so a setup re-run on cluster reuse
replaces it rather than stacking a second watchdog.
"""
import os
import subprocess
import time

_WD_DIR = os.environ.get("KF_WD_DIR", os.path.expanduser("~/.kinoforge-watchdog"))
_DEADLINE_FILE = os.path.join(_WD_DIR, "deadline")
_SKY_PYTHON_PATH_FILE = os.path.expanduser("~/.sky/python_path")
_POLL_INTERVAL_S = $poll_interval_s
_GRACE_BEFORE_HALT_S = $grace_before_halt_s

_SKYLET_AUTODOWN_CODE = (
    "from sky.skylet import autostop_lib; "
    "autostop_lib.set_autostop(0, 'CloudVmRayBackend', "
    "autostop_lib.AutostopWaitFor.NONE, True)"
)


def log(message):
    """Print a tagged line; the caller redirects stdout to watchdog.log.

    Best-effort: a full disk or a broken/closed stdout raises ``OSError`` out
    of ``print``, and this function must never be the reason a tick dies.
    """
    try:
        print("[kinoforge-watchdog] " + str(message), flush=True)
    except Exception:
        pass


def read_deadline():
    """Return the deadline epoch from disk, or None when unreadable."""
    try:
        with open(_DEADLINE_FILE) as handle:
            return float(handle.read().strip())
    except Exception as exc:
        log("deadline unreadable: " + repr(exc))
        return None


def run_command(command):
    """Run a shell command best-effort; return its exit code, -1 on failure."""
    try:
        completed = subprocess.run(
            command, shell=True, capture_output=True, timeout=120
        )
        return completed.returncode
    except Exception as exc:
        log("command failed: " + command + " " + repr(exc))
        return -1


def skylet_autodown():
    """Stage 1 - ask the on-node skylet to autodown now (real terminate)."""
    try:
        with open(_SKY_PYTHON_PATH_FILE) as handle:
            sky_python = handle.read().strip()
    except Exception:
        sky_python = ""
    if not sky_python:
        log("no skylet python at " + _SKY_PYTHON_PATH_FILE + "; skipping stage 1")
        return -1
    code = run_command(sky_python + ' -c "' + _SKYLET_AUTODOWN_CODE + '"')
    log("stage 1 skylet autodown rc=" + str(code))
    return code


def halt():
    """Stage 2 - credential-free local halt."""
    code = run_command("sudo shutdown -h now")
    log("stage 2 shutdown rc=" + str(code))
    if code != 0:
        code = run_command("sudo halt -f")
        log("stage 2 halt -f rc=" + str(code))
    return code


def main():
    """Poll until the deadline, then terminate in two stages.

    Every tick's body is exception-guarded: no single bad tick (a raising
    ``log()``, a transient ``read_deadline()`` failure, anything) may escape
    the loop, or the watchdog dies silently while the instance keeps
    billing with no deadline enforcement left.
    """
    log("watchdog started; deadline file " + _DEADLINE_FILE)
    fired_at = None
    while True:
        now = time.time()
        try:
            deadline = read_deadline()
            if deadline is None:
                pass
            elif now < deadline:
                fired_at = None
            elif fired_at is None:
                log("deadline reached; firing stage 1")
                skylet_autodown()
                fired_at = now
            elif now - fired_at >= _GRACE_BEFORE_HALT_S:
                log("still alive after stage 1; firing stage 2")
                halt()
                fired_at = now
        except Exception as exc:
            log("tick failed: " + repr(exc))
        time.sleep(_POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
'''
)


def RENDER_WATCHDOG(  # noqa: N802 — public, used as RENDER_WATCHDOG(...)
    *,
    poll_interval_s: float = 15.0,
    grace_before_halt_s: float = 120.0,
) -> str:
    """Render the on-instance watchdog program.

    Args:
        poll_interval_s: Seconds between deadline checks.
        grace_before_halt_s: Seconds to wait after the stage-1 skylet
            autodown before falling back to a local halt.

    Returns:
        A self-contained python program (stdlib only) to be written to the
        instance and run with ``nohup``.

    Example:
        >>> "def main()" in RENDER_WATCHDOG()
        True
    """
    return _WATCHDOG_TEMPLATE.substitute(
        poll_interval_s=poll_interval_s,
        grace_before_halt_s=grace_before_halt_s,
    )
