"""In-pod self-terminator script template for RunPod instances.

The :func:`RENDER` function substitutes lifecycle parameters into the template
and returns a Python script string that can be embedded in the pod environment
as ``KINOFORGE_SELFTERM_SCRIPT``.

The generated script enforces two independent **boot-relative** caps and
terminates the pod when either elapses:

- ``effective_deadline`` = ``start_time + max_lifetime - time_buffer``.
- ``boot_cap`` = ``start_time + 2 * idle_timeout``.

Effective pod lifetime is therefore
``min(2 * idle_timeout, max_lifetime - time_buffer)`` — at :class:`Lifecycle`
defaults, 4 h.

**The second cap is NOT a dead-man's switch** (audit B4, 2026-07-28). It was
documented as one from ``1be572d`` until 2026-07-28, but nothing inside the
pod has ever written a liveness signal: the template's ``heartbeat()`` helper
was never called, and the orchestrator's heartbeat writes to RunPod pod tags
(:mod:`kinoforge.providers.runpod.heartbeat`) which this script never reads.
So the timer always measured pod age, never idleness. It is kept — renamed and
documented for what it is — as a pod-side money backstop that survives the
controller dying; the unreachable ``job_timeout`` branch was deleted.

Wiring a real in-pod heartbeat was considered and rejected on 2026-07-28: no
kinoforge render approaches 4 h, and the project's heartbeat history (C33 —
the RunPod ``podEditJob`` satisfier restarting containers, commits ``7e22467``
and ``a2e1a75``) argues against adding a third in-pod liveness notion with
terminate authority.

``tests/providers/runpod/test_selfterm_reap_conditions.py`` executes the
rendered script against a fake clock and pins these conditions. Other selfterm
tests assert substring presence only.
"""

from __future__ import annotations

from string import Template

# ---------------------------------------------------------------------------
# Script template
# ---------------------------------------------------------------------------

#: Raw Python script template; placeholders use $-style substitution.
_TEMPLATE = Template(
    '''\
#!/usr/bin/env python3
"""Kinoforge in-pod self-terminator.

Lifecycle parameters (seconds):
  idle_timeout    = $idle_timeout
  max_lifetime    = $max_lifetime
  time_buffer     = $time_buffer

Both caps are BOOT-RELATIVE — they measure pod age, never idleness:
  effective_deadline = start_time + max_lifetime - time_buffer
  boot_cap           = start_time + 2 * idle_timeout

The pod is destroyed when EITHER elapses, so effective lifetime is
min(2 * idle_timeout, max_lifetime - time_buffer).

NOTE: boot_cap is a fixed money backstop, NOT a heartbeat dead-man\'s
switch. Nothing inside this pod writes a liveness signal, so activity
cannot extend it (audit B4, 2026-07-28 — the timer had been mislabelled
a dead-man\'s switch since the provider first shipped). A render longer
than this cap will be killed mid-job; that is accepted, and the cap is
kept because it reaps the pod even when the controller dies.

All termination requests go to the RunPod terminate endpoint authenticated
with the RUNPOD_TERMINATE_KEY environment variable (scoped key — NOT the
main RUNPOD_API_KEY).
"""
import os
import time
import urllib.request
import json

_POD_ID: str = os.environ.get("RUNPOD_POD_ID", "")
_TERMINATE_KEY: str = os.environ.get("RUNPOD_TERMINATE_KEY", "")
_IDLE_TIMEOUT: float = $idle_timeout
_MAX_LIFETIME: float = $max_lifetime
_TIME_BUFFER: float = $time_buffer

_BOOT_CAP: float = 2.0 * _IDLE_TIMEOUT

_start_time: float = time.time()


def effective_deadline() -> float:
    """Return the absolute POSIX timestamp after which the pod must terminate.

    effective_deadline = start_time + max_lifetime - time_buffer
    """
    return _start_time + _MAX_LIFETIME - _TIME_BUFFER


def boot_cap_deadline() -> float:
    """Return the absolute POSIX timestamp of the boot-relative cap.

    boot_cap_deadline = start_time + 2 * idle_timeout
    """
    return _start_time + _BOOT_CAP


def _terminate() -> None:
    """Best-effort DELETE to the RunPod REST pods endpoint.

    Any HTTP error or transport failure is silently swallowed; the
    watchdog will retry on the next poll iteration.
    """
    url = f"https://rest.runpod.io/v1/pods/{_POD_ID}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {_TERMINATE_KEY}",
            "User-Agent": "kinoforge-selfterm/0.1",
        },
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            resp.read()
    except Exception:  # noqa: BLE001
        pass  # best-effort; we are terminating anyway


def _check_and_reap() -> None:
    """Terminate the pod once either boot-relative cap has elapsed."""
    now = time.time()

    # Cap 1: max_lifetime / effective_deadline enforcement
    if now >= effective_deadline():
        _terminate()
        return

    # Cap 2: fixed boot-relative backstop at 2 * idle_timeout. Pod age only —
    # no activity signal exists in the pod, so nothing postpones this.
    if now >= boot_cap_deadline():
        _terminate()
        return


if __name__ == "__main__":
    while True:
        _check_and_reap()
        time.sleep(15)
'''
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def RENDER(  # noqa: N802 — public, used as RENDER(...)
    *,
    idle_timeout: float,
    max_lifetime: float,
    time_buffer: float,
) -> str:
    """Render the self-terminator script with the given lifecycle parameters.

    ``job_timeout`` was removed from this surface on 2026-07-28 (audit B4):
    the branch that consumed it was unreachable dead code, because the
    template never assigned a job-start timestamp.

    Args:
        idle_timeout: Idle-timeout in seconds; the boot-relative cap is
            2×this. Despite the name it does NOT measure idleness — see the
            module docstring.
        max_lifetime: Hard ceiling on pod age in seconds.
        time_buffer: Safety margin subtracted from max_lifetime when
            computing the effective deadline.

    Returns:
        A Python script string ready to be embedded in the pod environment
        as ``KINOFORGE_SELFTERM_SCRIPT``.

    Example:
        >>> script = RENDER(idle_timeout=1800, max_lifetime=7200,
        ...                 time_buffer=300)
        >>> "effective_deadline" in script
        True
    """
    return _TEMPLATE.substitute(
        idle_timeout=idle_timeout,
        max_lifetime=max_lifetime,
        time_buffer=time_buffer,
    )
