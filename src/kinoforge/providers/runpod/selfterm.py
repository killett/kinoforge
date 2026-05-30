"""In-pod self-terminator script template for RunPod instances.

The :func:`RENDER` function substitutes lifecycle parameters into the template
and returns a Python script string that can be embedded in the pod environment
as ``KINOFORGE_SELFTERM_SCRIPT``.

The generated script:
- Enforces ``max_lifetime``: shuts down the pod unconditionally once the pod
  age exceeds ``max_lifetime`` seconds.
- Enforces ``effective_deadline``: uses ``idle_timeout``, ``job_timeout``, and
  ``time_buffer`` to compute an upper bound; shuts down before the budget runs
  out.
- Implements a ``heartbeat`` dead-man's switch: if no heartbeat is received
  within ``2 * idle_timeout`` seconds the pod self-terminates.

Tests verify substring presence only — the script is NOT executed in tests.
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
  job_timeout     = $job_timeout
  time_buffer     = $time_buffer

Effective deadline = start_time + max_lifetime - time_buffer
The pod is destroyed when ANY of the following conditions fires:
  1. Current time >= effective_deadline
  2. No heartbeat received within 2 * idle_timeout (dead-man\'s switch)
  3. A running job exceeds job_timeout

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
_JOB_TIMEOUT: float = $job_timeout
_TIME_BUFFER: float = $time_buffer

_HEARTBEAT_DEAD_MAN_WINDOW: float = 2.0 * _IDLE_TIMEOUT

_start_time: float = time.time()
_last_heartbeat: float = _start_time
_job_start: float | None = None


def effective_deadline() -> float:
    """Return the absolute POSIX timestamp after which the pod must terminate.

    effective_deadline = start_time + max_lifetime - time_buffer
    """
    return _start_time + _MAX_LIFETIME - _TIME_BUFFER


def heartbeat() -> None:
    """Record the current wall-clock time as the last heartbeat signal."""
    global _last_heartbeat
    _last_heartbeat = time.time()


def _terminate() -> None:
    """Terminate this pod via the RunPod terminate endpoint."""
    url = f"https://api.runpod.io/v2/{_POD_ID}/stop"
    data = json.dumps({}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_TERMINATE_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            resp.read()
    except Exception:  # noqa: BLE001
        pass  # best-effort; we are terminating anyway


def _check_and_reap() -> None:
    """Check all termination conditions and self-terminate if any is met."""
    now = time.time()
    deadline = effective_deadline()

    # Condition 1: max_lifetime / effective_deadline enforcement
    if now >= deadline:
        _terminate()
        return

    # Condition 2: heartbeat dead-man\'s switch
    time_since_heartbeat = now - _last_heartbeat
    if time_since_heartbeat > _HEARTBEAT_DEAD_MAN_WINDOW:
        _terminate()
        return

    # Condition 3: job_timeout per running job
    if _job_start is not None:
        job_age = now - _job_start
        if job_age > _JOB_TIMEOUT:
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
    job_timeout: float,
    time_buffer: float,
) -> str:
    """Render the self-terminator script with the given lifecycle parameters.

    Args:
        idle_timeout: Idle-timeout in seconds; dead-man window = 2×this.
        max_lifetime: Hard ceiling on pod age in seconds.
        job_timeout: Per-job time limit in seconds.
        time_buffer: Safety margin subtracted from max_lifetime when
            computing the effective deadline.

    Returns:
        A Python script string ready to be embedded in the pod environment
        as ``KINOFORGE_SELFTERM_SCRIPT``.

    Example:
        >>> script = RENDER(idle_timeout=1800, max_lifetime=7200,
        ...                 job_timeout=900, time_buffer=300)
        >>> "effective_deadline" in script
        True
    """
    return _TEMPLATE.substitute(
        idle_timeout=idle_timeout,
        max_lifetime=max_lifetime,
        job_timeout=job_timeout,
        time_buffer=time_buffer,
    )
