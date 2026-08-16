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

import math
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
           plan: firing it while stage 1's terminate is still in flight
           DOWNGRADES a clean terminate into a stopped instance with a
           billing disk, which is strictly worse than waiting longer. The
           grace default (600 s) is sized off a real run, not a guess: a
           2026-08-15 live AWS smoke (cluster kinoforge-wd-13b6aaeb) logged
           "stage 1 skylet autodown rc=0" — the autodown call itself
           succeeded — but the underlying teardown (SkyPilot's
           AutostopEvent tick, <=60s, followed by `_stop_cluster` and the
           provisioner terminate) had not finished 120s later when stage 2
           fired, so the halt landed on a still-terminating instance and
           the box ended up `stopped`, not `terminated`. 600 s gives that
           end-to-end path room to finish before stage 2 is allowed to
           preempt it, while still catching a genuinely wedged stage 1.

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
#: Overridable so tests (and any host with passwordless sudo) never have to
#: let this daemon invoke the REAL privileged binary. The arming prelude
#: exports ``KF_WD_SUDO`` alongside ``KF_WD_DIR``/``KF_WD_PYTHON`` before
#: spawning this script, so a test's recording stub travels with it.
_SUDO_BIN = os.environ.get("KF_WD_SUDO", "sudo")

#: A 2026-08-15 live AWS run (cluster kinoforge-wd-6722c3f1) proved stage 1
#: has NEVER been able to terminate an instance: `_stop_cluster`
#: (sky/skylet/events.py:364) compares `autostop_config.backend` against
#: `cloud_vm_ray_backend.CloudVmRayBackend.NAME`, whose runtime value is the
#: string 'cloudvmray' -- NOT the class name 'CloudVmRayBackend' this literal
#: used to pass. Every non-matching backend falls through to
#: `else: raise NotImplementedError`, so the skylet's own log showed the
#: autodown call return rc=0 (the RPC itself succeeded) while the actual
#: dispatch inside `_stop_cluster` raised and the instance stayed `running`.
#: Importing the NAME constant on the instance (with the literal as a
#: fallback for an import failure) ties this to sky's own source of truth
#: instead of a hand-copied string that can silently drift again.
_SKYLET_AUTODOWN_CODE = (
    "from sky.skylet import autostop_lib\\n"
    "try:\\n"
    "    from sky.backends import cloud_vm_ray_backend as _b\\n"
    "    _backend = _b.CloudVmRayBackend.NAME\\n"
    "except Exception:\\n"
    "    _backend = 'cloudvmray'\\n"
    "autostop_lib.set_autostop(0, _backend, "
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
    code = run_command(_SUDO_BIN + " shutdown -h now")
    log("stage 2 shutdown rc=" + str(code))
    if code != 0:
        code = run_command(_SUDO_BIN + " halt -f")
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
    grace_before_halt_s: float = 600.0,
) -> str:
    """Render the on-instance watchdog program.

    Args:
        poll_interval_s: Seconds between deadline checks.
        grace_before_halt_s: Seconds to wait after the stage-1 skylet
            autodown before falling back to a local halt. Default is 600 s,
            not a round-number guess: a 2026-08-15 live AWS run showed
            SkyPilot's autodown path (AutostopEvent tick <=60s ->
            `_stop_cluster` -> provisioner terminate) needing more than
            120 s end to end, and a stage-2 halt that preempts an
            in-flight stage-1 terminate downgrades it into a `stopped`
            instance whose disk keeps billing — the exact outcome stage 1
            exists to avoid. Stage 2 stays as a backstop for a genuinely
            wedged stage 1 (missing skylet, sky version drift, terminate
            API refusing); it must simply wait long enough not to race a
            healthy one.

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


class _BashTemplate(Template):
    """Template with an ``@`` delimiter so bash's own ``$`` survives intact."""

    delimiter = "@"


#: Arming prelude prepended to ``Task.setup``. ``@``-placeholders only.
#:
#: The whole body runs inside ``( set +e +u; ...; ) || true`` so it can never
#: propagate a non-zero exit into the CALLER's shell, regardless of whether
#: that caller runs under ``set -e`` / ``set -u`` (SkyPilot's own setup
#: wrapper, or an unset ``$HOME``) — a failed arm must never abort
#: ``Task.setup``, because SkyPilot deliberately leaves a cluster UP when
#: setup fails, which is exactly the unbounded-billing cluster this exists
#: to prevent.
#:
#: The live-watchdog guard identifies the daemon by COMMAND LINE
#: (``pgrep -f "$KF_WD_DIR/watchdog.py"``), never by trusting the pid number
#: recorded in the ``pid`` file alone: a stale pid can be silently recycled
#: onto an unrelated live process (which would fool a bare ``kill -0``), and
#: ``$!`` after ``setsid nohup ... &`` can name the short-lived ``setsid``
#: wrapper rather than the daemon it forked. Matching by script path in the
#: live process table sidesteps both.
_ARM_TEMPLATE = _BashTemplate(
    """\
# --- kinoforge watchdog arm (must stay first in setup) ---------------------
(
set +e +u
KF_WD_DIR="${KF_WD_DIR:-$HOME/.kinoforge-watchdog}"
KF_WD_PYTHON="${KF_WD_PYTHON:-python3}"
KF_WD_SUDO="${KF_WD_SUDO:-sudo}"
export KF_WD_DIR
export KF_WD_PYTHON
export KF_WD_SUDO
mkdir -p "$KF_WD_DIR" 2>/dev/null
printf '%s\\n' '@deadline_epoch' > "$KF_WD_DIR/deadline.tmp" 2>/dev/null
mv -f "$KF_WD_DIR/deadline.tmp" "$KF_WD_DIR/deadline" 2>/dev/null
cat > "$KF_WD_DIR/watchdog.py" <<'KF_WD_PY_EOF' 2>/dev/null
@watchdog_source
KF_WD_PY_EOF
_kf_live_pid="$(pgrep -f "$KF_WD_DIR/watchdog.py" 2>/dev/null | head -n1)"
if [ -n "$_kf_live_pid" ]; then
  echo "$_kf_live_pid" > "$KF_WD_DIR/pid" 2>/dev/null
  echo "[kinoforge-watchdog] already armed (pid $_kf_live_pid); deadline refreshed to @deadline_epoch"
else
  setsid nohup "$KF_WD_PYTHON" "$KF_WD_DIR/watchdog.py" >> "$KF_WD_DIR/watchdog.log" 2>&1 &
  sleep 0.2
  _kf_live_pid="$(pgrep -f "$KF_WD_DIR/watchdog.py" 2>/dev/null | head -n1)"
  if [ -n "$_kf_live_pid" ]; then
    echo "$_kf_live_pid" > "$KF_WD_DIR/pid" 2>/dev/null
    echo "[kinoforge-watchdog] armed pid $_kf_live_pid deadline=@deadline_epoch"
  else
    echo "[kinoforge-watchdog] spawn failed; kernel poweroff backstop is the only enforcement left"
  fi
fi
# F3 — the kernel poweroff timer is now UNCONDITIONAL: armed on every run
# whether or not the daemon spawn was confirmed above. A live daemon can
# still die later (OOM-kill, crash) and nothing re-arms it, because
# Task.setup never re-runs on a cluster that is already up. Cancel any
# previous timer FIRST so a re-arm never stacks two, then schedule fresh —
# sized (see @fallback_minutes in RENDER_ARM) to land AFTER this arm's own
# deadline plus its stage-2 grace window, so a healthy daemon always gets
# first crack and this is a pure backstop, never a race against stage 1/2.
$KF_WD_SUDO shutdown -c >/dev/null 2>&1
echo "[kinoforge-watchdog] kernel poweroff backstop armed in @fallback_minutes min"
$KF_WD_SUDO shutdown -h +@fallback_minutes >/dev/null 2>&1
) || true
# --- end kinoforge watchdog arm -------------------------------------------
"""
)


def RENDER_ARM(  # noqa: N802 — public, used as RENDER_ARM(...)
    *,
    deadline_epoch: float,
    now: float,
    poll_interval_s: float = 15.0,
    grace_before_halt_s: float = 600.0,
) -> str:
    """Render the bash prelude that arms the watchdog on the instance.

    The prelude is prepended to ``Task.setup`` so the deadline is armed
    before the heavy installs: a cluster that dies during a 20-minute pip
    install is still covered.

    Idempotent by construction — ``Task.setup`` re-runs on cluster reuse, so
    the snippet refreshes the deadline file and skips the spawn when a live
    watchdog is already found running. The live-watchdog check matches by
    COMMAND LINE (``pgrep -f ".../watchdog.py"``), not by trusting a
    recorded pid number: a pid can be silently recycled onto an unrelated
    live process, and ``$!`` right after ``setsid nohup ... &`` can name the
    short-lived ``setsid`` wrapper rather than the daemon it forked. Either
    mistake would either falsely report "already armed" with no watchdog
    actually running, or spawn a second watchdog racing the first — this
    closes both by construction rather than by trusting either number.
    ``KF_WD_DIR``/``KF_WD_PYTHON``/``KF_WD_SUDO`` are exported before the
    spawn so the daemon resolves the SAME directory the arm step just wrote
    to, and the same (possibly stubbed) privileged-command indirection, even
    when the caller never set them.

    F2 — every privileged call in this prelude goes through
    ``$KF_WD_SUDO`` (default ``sudo``) rather than the literal binary, so a
    test can point it at a recording stub instead of letting the REAL prelude
    call ``sudo shutdown -c`` / ``sudo shutdown -h +N`` on whatever host runs
    the suite. The on-instance daemon (:func:`RENDER_WATCHDOG`) is exported
    the same override for its stage-2 ``shutdown -h now`` / ``halt -f``.

    F3 — the ``shutdown -h +N`` kernel poweroff timer is UNCONDITIONAL: it is
    cancelled (``shutdown -c``) and rescheduled on EVERY arm, whether or not
    the daemon spawn was confirmed. This closes the residual single point of
    failure where a live daemon dies later (OOM-kill, crash): there is no
    re-arm on a live cluster (``Task.setup`` never re-runs), so the timer
    scheduled at first arm is the only thing left standing between a dead
    daemon and unbounded billing. ``fallback_minutes`` is sized to
    ``deadline_epoch + grace_before_halt_s + a fixed safety margin`` — i.e.
    to fire strictly AFTER the daemon's own stage-2 halt would have fired —
    so a healthy daemon always gets first crack and this kernel timer never
    races stage 1/2, it only catches their absence. Cancelling first, every
    time, keeps repeated re-arms (cluster reuse across multiple ``setup``
    runs) from stacking timers.

    The whole body runs inside a ``( set +e +u; ...; ) || true`` block:
    failure of ANY step — including resolving ``$HOME`` when it is unset —
    must never abort setup, regardless of the shell options the surrounding
    ``Task.setup`` happens to run under. SkyPilot deliberately leaves a
    cluster UP when setup fails ("for debugging purposes"), so aborting here
    would produce precisely the unbounded-billing cluster this exists to
    prevent.

    Args:
        deadline_epoch: Absolute POSIX epoch from :func:`compute_deadline`.
        now: POSIX epoch at render time; used only to size the
            ``shutdown -h +N`` backstop.
        poll_interval_s: Passed through to :func:`RENDER_WATCHDOG`; also
            factored into the backstop's safety margin.
        grace_before_halt_s: Passed through to :func:`RENDER_WATCHDOG`; also
            factored into the backstop's timing so it lands after stage 2.

    Returns:
        A bash snippet, safe to concatenate ahead of a provision script.

    Example:
        >>> RENDER_ARM(deadline_epoch=60.0, now=0.0).lstrip()[:26]
        '# --- kinoforge watchdog a'
    """
    # F3: the backstop must fire strictly AFTER the daemon's own stage-2 halt
    # would have — i.e. after (deadline + grace) — not merely at the same
    # instant. The margin covers the daemon's worst-case additional delay
    # past that instant: up to one more poll tick before it notices grace has
    # elapsed, plus run_command's subprocess timeout (120s) if the halt
    # command itself hangs.
    backstop_margin_s = poll_interval_s + 120.0
    backstop_after_s = (deadline_epoch - now) + grace_before_halt_s + backstop_margin_s
    fallback_minutes = max(1, math.ceil(backstop_after_s / 60.0))
    return _ARM_TEMPLATE.substitute(
        deadline_epoch=repr(float(deadline_epoch)),
        watchdog_source=RENDER_WATCHDOG(
            poll_interval_s=poll_interval_s,
            grace_before_halt_s=grace_before_halt_s,
        ),
        fallback_minutes=fallback_minutes,
    )
