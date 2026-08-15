# SkyPilot instance-side deadline watchdog — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every SkyPilot cluster a wall-clock deadline enforced on the instance itself, armed
at the top of `Task.setup`, so the cluster dies even when the orchestrator process does not.

**Architecture:** A new `providers/skypilot/watchdog.py` renders (a) a pure deadline calculation,
(b) a standalone python watchdog that runs on the instance, and (c) a bash arming prelude that is
prepended to `Task.setup`. `SkyPilotProvider.create_instance` arms it, passes `down=True` to
`sky.launch`, and writes a provisional ledger row before `sky.launch` so a mid-launch process death
still leaves a discoverable record. Nothing in `ComputeProvider`'s ABC, the reaper's verdict tree,
or the YAML config schema changes.

**Tech Stack:** Python 3.12, `string.Template` (no new deps), pytest, SkyPilot `0.12.3.post1`
(pinned in `pixi.lock`), AWS `us-west-2` for the live smoke.

**Global Constraints:**
- Design doc: `docs/superpowers/specs/2026-08-15-skypilot-instance-deadline-watchdog-design.md`.
  Every task must match it.
- **Additive only.** Do NOT modify `ComputeProvider` (`core/interfaces.py`), the reaper verdict
  tree (`core/reaper.py`), or `LifecycleConfig`/`ComputeConfig` in `core/config.py`.
- No cloud credential is embedded in any rendered artefact. The stage-1 terminate uses only what
  SkyPilot itself already places on the node.
- Deadline formula is exactly `min(launch_epoch + max_lifetime_s, launch_epoch +
  budget_usd/rate*3600 when rate>0 and budget>0)`. No `time_buffer_s` term.
- The arming step is the FIRST content of `Task.setup`, before any provision-script line.
- Arming is idempotent: a second setup run refreshes the deadline file and spawns no second process.
- Live spend: `pixi run preflight` first; RED scaffold committed BEFORE the spend; smoke tears down
  in a `finally`.
- Every commit runs `pixi run pre-commit run --all-files` and passes. Conventional Commits.

**User decisions (already made):**
- Kill path: "Skylet autodown, then shutdown -h" — stage 1 asks the on-node skylet to autodown NOW
  (real terminate, credentials already on the node); stage 2 halts locally after a 120 s grace.
- Deadline: "launch + max_lifetime_s" — measured from launch (covers provisioning), no buffer term,
  additionally capped by budget/rate when the rate is known.
- Live smoke cloud: "AWS us-west-2", cheapest CPU SKU, budget < $0.05.
- Durable record: "Existing Ledger, provisional row" — written before `sky.launch`, forgotten on the
  success path so the orchestrator's post-create `record` writes the final row.

---

## File structure

| File | Responsibility |
|---|---|
| `src/kinoforge/providers/skypilot/watchdog.py` (create) | Pure deadline math + the two render functions. No I/O, no sky import. |
| `src/kinoforge/providers/skypilot/__init__.py` (modify) | `create_instance`: arm the watchdog, pass `down=True`, write/forget the provisional ledger row. Module docstring correction. |
| `src/kinoforge/core/orchestrator.py` (modify) | Hand the provider a `Ledger` after resolution (duck-typed — core must not import provider modules). |
| `tests/providers/skypilot/test_watchdog_deadline.py` (create) | U1–U3: deadline math. |
| `tests/providers/skypilot/test_watchdog_fire_conditions.py` (create) | U7: `exec` the rendered watchdog against a fake clock + fake subprocess. |
| `tests/providers/skypilot/test_watchdog_arm_idempotency.py` (create) | U6: run the real bash prelude twice in a temp dir. |
| `tests/providers/test_skypilot.py` (modify) | U4, U5, U8, U9, U10: setup ordering, `down` kwarg, ledger call ordering. |
| `tests/live/test_skypilot_watchdog_smoke.py` (create) | Live AWS smoke. |

`tests/providers/skypilot/` is a new directory — it needs an `__init__.py` (the repo's `tests/`
tree uses package-style test dirs, e.g. `tests/providers/runpod/`).

---

### Task 0: Deadline math

**Goal:** A pure, unit-tested `compute_deadline` in a new `watchdog.py` module.

**Files:**
- Create: `src/kinoforge/providers/skypilot/watchdog.py`
- Create: `tests/providers/skypilot/__init__.py` (empty)
- Test: `tests/providers/skypilot/test_watchdog_deadline.py`

**Acceptance Criteria:**
- [ ] `compute_deadline(launch_epoch=1000.0, max_lifetime_s=18000.0)` returns `19000.0`
- [ ] Budget bound applies when it is tighter: `budget_usd=0.10, rate_usd_per_hr=1.00` at
      `max_lifetime_s=18000` returns `launch_epoch + 360.0`
- [ ] Budget bound is ignored when `rate_usd_per_hr <= 0` or `budget_usd <= 0`
- [ ] Budget bound is ignored when it is looser than the lifetime bound
- [ ] `max_lifetime_s <= 0` raises `ValueError`

**Verify:** `pixi run pytest tests/providers/skypilot/test_watchdog_deadline.py -v` → 5 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
"""Deadline math for the SkyPilot instance-side watchdog.

Behaviour under test: :func:`compute_deadline` turns a launch timestamp plus
lifecycle policy into the absolute epoch after which the instance must be
dead. The deadline is LAUNCH-relative (so it covers the provisioning window,
which is the F12 failure window) and carries no ``time_buffer_s`` term.
"""

from __future__ import annotations

import pytest

from kinoforge.providers.skypilot.watchdog import compute_deadline


def test_deadline_is_launch_plus_max_lifetime_when_rate_unknown() -> None:
    """With no rate, the deadline is exactly launch + max_lifetime_s.

    A bug this catches: reintroducing a ``time_buffer_s`` term (which would
    return 17_200.0 at Lifecycle defaults) or measuring from boot instead of
    from launch.
    """
    assert compute_deadline(launch_epoch=1000.0, max_lifetime_s=18000.0) == 19000.0


def test_budget_bound_wins_when_tighter_than_lifetime() -> None:
    """$0.10 at $1.00/hr caps the instance at 360 s regardless of max_lifetime.

    A bug this catches: the budget bound computed but discarded, or combined
    with ``max`` instead of ``min`` — either leaves a 5 h deadline on a
    cluster the budget can only afford for 6 minutes.
    """
    got = compute_deadline(
        launch_epoch=1000.0,
        max_lifetime_s=18000.0,
        budget_usd=0.10,
        rate_usd_per_hr=1.00,
    )
    assert got == pytest.approx(1360.0)


def test_lifetime_bound_wins_when_budget_bound_is_looser() -> None:
    """A generous budget must not extend the deadline past max_lifetime_s.

    A bug this catches: returning the budget bound unconditionally once a
    rate is known.
    """
    got = compute_deadline(
        launch_epoch=1000.0,
        max_lifetime_s=18000.0,
        budget_usd=100.0,
        rate_usd_per_hr=1.00,
    )
    assert got == 19000.0


@pytest.mark.parametrize(
    ("budget_usd", "rate_usd_per_hr"),
    [(0.0, 1.0), (0.10, 0.0), (0.0, 0.0), (-1.0, 1.0), (0.10, -1.0)],
)
def test_budget_bound_ignored_when_budget_or_rate_non_positive(
    budget_usd: float, rate_usd_per_hr: float
) -> None:
    """Zero/negative budget or rate drops the budget bound entirely.

    A bug this catches: ``Lifecycle.budget_usd`` defaults to 0.0, so a naive
    ``budget/rate`` would make every default-config cluster self-destruct on
    the first watchdog tick; a zero rate would raise ZeroDivisionError inside
    create_instance.
    """
    got = compute_deadline(
        launch_epoch=1000.0,
        max_lifetime_s=18000.0,
        budget_usd=budget_usd,
        rate_usd_per_hr=rate_usd_per_hr,
    )
    assert got == 19000.0


def test_non_positive_max_lifetime_raises() -> None:
    """A zero/negative max_lifetime_s is a caller bug, not a 'die now' order.

    A bug this catches: silently rendering an already-expired deadline, which
    would kill every cluster ~15 s into setup.
    """
    with pytest.raises(ValueError, match="max_lifetime_s"):
        compute_deadline(launch_epoch=1000.0, max_lifetime_s=0.0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run pytest tests/providers/skypilot/test_watchdog_deadline.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'kinoforge.providers.skypilot.watchdog'`

- [ ] **Step 3: Write the module**

Create `tests/providers/skypilot/__init__.py` as an empty file, then:

```python
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
```

`math` is imported here for Task 2's `RENDER_ARM`; if the linter flags it as unused at this
point, add it in Task 2 instead.

- [ ] **Step 4: Run to verify they pass**

Run: `pixi run pytest tests/providers/skypilot/test_watchdog_deadline.py -v`
Expected: 5 passed (the parametrized case counts as 5 ids — expect 9 passed total; either way, all green)

- [ ] **Step 5: Lint + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/providers/skypilot/watchdog.py \
  tests/providers/skypilot/__init__.py \
  tests/providers/skypilot/test_watchdog_deadline.py
git add src/kinoforge/providers/skypilot/watchdog.py tests/providers/skypilot/
git commit -m "feat(skypilot): add launch-relative deadline math for the watchdog"
```

---

### Task 1: The watchdog program and its fire conditions

**Goal:** `RENDER_WATCHDOG` produces the on-instance daemon; its fire conditions are pinned by
executing the rendered source against a fake clock and fake subprocess.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/watchdog.py`
- Test: `tests/providers/skypilot/test_watchdog_fire_conditions.py`

**Acceptance Criteria:**
- [ ] Before the deadline: no subprocess is spawned
- [ ] At the deadline: exactly one command is run, containing the skylet python path and
      `autostop_lib.set_autostop(0, 'CloudVmRayBackend', autostop_lib.AutostopWaitFor.NONE, True)`
- [ ] Within the grace window after stage 1: no halt is issued
- [ ] After the grace window: `sudo shutdown -h now` is issued
- [ ] The deadline is re-read from disk on every tick (rewriting the file to a later value stops
      the fire)
- [ ] Missing `~/.sky/python_path` skips stage 1 without raising, and stage 2 still runs

**Verify:** `pixi run pytest tests/providers/skypilot/test_watchdog_fire_conditions.py -v` → 6 passed

**Steps:**

- [ ] **Step 1: Write the failing test**

Pattern copied from `tests/providers/runpod/test_selfterm_reap_conditions.py` (audit B4): execute
the rendered artefact rather than asserting substrings, because substring tests let a mislabelled
RunPod timer survive 100+ commits. Two gotchas carried over from that file — the script's own
`import time` / `import subprocess` rebind over any pre-seeded namespace, so swap those module
objects **after** the `exec`, and never compare POSIX timestamps with `pytest.approx` (the relative
tolerance at ~1.7e9 is ±1700 s).

```python
"""Fire conditions of the rendered SkyPilot instance watchdog.

Behaviour under test: the watchdog terminates the instance at its deadline
and not before, escalating from the skylet autodown (a real cloud terminate)
to a local halt only after a grace window.

These tests ``exec`` the rendered source and call ``main()`` against a fake
clock and a fake ``subprocess`` module, so they pin what the script DOES.
Substring-presence assertions are deliberately avoided — audit B4 showed
those let a mislabelled RunPod timer survive since ``1be572d``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kinoforge.providers.skypilot.watchdog import RENDER_WATCHDOG

_POLL_S = 15.0
_GRACE_S = 120.0


class _StopLoop(Exception):
    """Raised by the fake clock to break the watchdog's infinite loop."""


class _FakeTime:
    """Clock whose ``time()`` walks a fixed list; ``sleep()`` ends the run."""

    def __init__(self, times: list[float]) -> None:
        self._times = list(times)
        self.sleeps: list[float] = []

    def time(self) -> float:
        if not self._times:
            raise _StopLoop
        return self._times.pop(0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if not self._times:
            raise _StopLoop


class _FakeSubprocess:
    """Records every command string the watchdog runs."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[str] = []
        self._returncode = returncode

    def run(self, cmd: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(cmd)
        return SimpleNamespace(returncode=self._returncode, stdout=b"", stderr=b"")


def _load(
    tmp_path: Path,
    *,
    deadline: float,
    times: list[float],
    sky_python: str | None = "/opt/skypilot-runtime/bin/python",
    returncode: int = 0,
) -> tuple[dict[str, Any], _FakeSubprocess, Path]:
    """Exec the rendered watchdog and wire fakes into its namespace."""
    deadline_file = tmp_path / "deadline"
    deadline_file.write_text(f"{deadline}\n")
    source = RENDER_WATCHDOG(poll_interval_s=_POLL_S, grace_before_halt_s=_GRACE_S)
    ns: dict[str, Any] = {"__name__": "kf_watchdog_under_test"}
    exec(compile(source, "watchdog.py", "exec"), ns)  # noqa: S102 — the artefact IS the SUT
    # Swap AFTER exec: the script's own imports rebind these names.
    fake_sub = _FakeSubprocess(returncode=returncode)
    ns["time"] = _FakeTime(times)
    ns["subprocess"] = fake_sub
    ns["_DEADLINE_FILE"] = str(deadline_file)
    if sky_python is None:
        ns["_SKY_PYTHON_PATH_FILE"] = str(tmp_path / "absent")
    else:
        path_file = tmp_path / "python_path"
        path_file.write_text(sky_python + "\n")
        ns["_SKY_PYTHON_PATH_FILE"] = str(path_file)
    return ns, fake_sub, deadline_file


def _run(ns: dict[str, Any]) -> None:
    with pytest.raises(_StopLoop):
        ns["main"]()


def test_no_action_before_deadline(tmp_path: Path) -> None:
    """A watchdog whose deadline is in the future must run no commands.

    A bug this catches: comparing ``now > deadline`` against a boot-relative
    or zero-initialised deadline, which would kill every cluster during setup.
    """
    ns, sub, _ = _load(tmp_path, deadline=1000.0, times=[100.0, 200.0])
    _run(ns)
    assert sub.calls == [], f"expected no commands before the deadline, got {sub.calls!r}"


def test_stage_one_issues_skylet_autodown_at_deadline(tmp_path: Path) -> None:
    """At the deadline the watchdog asks the on-node skylet to autodown NOW.

    ``wait_for=NONE`` is what bypasses the permanently-False idle check (F1);
    ``down=True`` is what makes it a terminate rather than a stop (F2).

    A bug this catches: dropping ``AutostopWaitFor.NONE`` (the autodown would
    never fire because the server job keeps the cluster non-idle) or passing
    ``down=False`` (leaving a stopped instance with a billing disk).
    """
    ns, sub, _ = _load(tmp_path, deadline=1000.0, times=[1000.0])
    _run(ns)
    assert len(sub.calls) == 1, f"expected exactly one stage-1 command, got {sub.calls!r}"
    cmd = sub.calls[0]
    assert "/opt/skypilot-runtime/bin/python" in cmd, cmd
    assert "autostop_lib.set_autostop(0, 'CloudVmRayBackend'" in cmd, cmd
    assert "autostop_lib.AutostopWaitFor.NONE" in cmd, cmd
    assert "True)" in cmd, cmd


def test_no_halt_inside_the_grace_window(tmp_path: Path) -> None:
    """Stage 2 must not fire while stage 1 is still plausibly working.

    A bug this catches: halting immediately after stage 1, which converts a
    clean terminate into a stopped instance whose disk keeps billing.
    """
    ns, sub, _ = _load(tmp_path, deadline=1000.0, times=[1000.0, 1000.0 + _GRACE_S - 1])
    _run(ns)
    assert len(sub.calls) == 1, f"expected stage 1 only inside the grace window: {sub.calls!r}"


def test_stage_two_halts_after_the_grace_window(tmp_path: Path) -> None:
    """When the instance survives stage 1, the watchdog halts it locally.

    A bug this catches: a watchdog that gives up after stage 1 — on any cloud
    where the skylet is missing or the terminate API refuses, the instance
    would then bill forever.
    """
    ns, sub, _ = _load(tmp_path, deadline=1000.0, times=[1000.0, 1000.0 + _GRACE_S + 1])
    _run(ns)
    assert len(sub.calls) == 2, f"expected stage 1 then stage 2, got {sub.calls!r}"
    assert "shutdown -h now" in sub.calls[1], sub.calls[1]


def test_deadline_is_reread_every_tick(tmp_path: Path) -> None:
    """Rewriting the deadline file to a later value defers the fire.

    This is what makes a setup re-run on cluster reuse REPLACE the deadline
    instead of stacking a second watchdog.

    A bug this catches: reading the deadline once at startup, which would
    make the arming step's refresh silently ineffective.
    """
    ns, sub, deadline_file = _load(tmp_path, deadline=1000.0, times=[900.0, 1000.0])
    original_sleep = ns["time"].sleep

    def _sleep_then_extend(seconds: float) -> None:
        deadline_file.write_text("9999.0\n")
        original_sleep(seconds)

    ns["time"].sleep = _sleep_then_extend  # type: ignore[method-assign]
    _run(ns)
    assert sub.calls == [], f"extended deadline must defer the fire, got {sub.calls!r}"


def test_missing_skylet_python_skips_stage_one_and_still_halts(tmp_path: Path) -> None:
    """No ``~/.sky/python_path`` → stage 1 is skipped, stage 2 still halts.

    A bug this catches: an unguarded ``open()`` raising out of the tick loop,
    which would kill the watchdog and leave the instance unbounded.
    """
    ns, sub, _ = _load(
        tmp_path,
        deadline=1000.0,
        times=[1000.0, 1000.0 + _GRACE_S + 1],
        sky_python=None,
    )
    _run(ns)
    assert len(sub.calls) == 1, f"stage 1 must not run without a skylet python: {sub.calls!r}"
    assert "shutdown -h now" in sub.calls[0], sub.calls[0]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run pytest tests/providers/skypilot/test_watchdog_fire_conditions.py -v`
Expected: `ImportError: cannot import name 'RENDER_WATCHDOG'`

- [ ] **Step 3: Implement `RENDER_WATCHDOG`**

Append to `src/kinoforge/providers/skypilot/watchdog.py`:

```python
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
import shlex
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
    """Print a tagged line; the caller redirects stdout to watchdog.log."""
    print("[kinoforge-watchdog] " + str(message), flush=True)


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
    code = run_command(sky_python + " -c " + shlex.quote(_SKYLET_AUTODOWN_CODE))
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
    """Poll until the deadline, then terminate in two stages."""
    log("watchdog started; deadline file " + _DEADLINE_FILE)
    fired_at = None
    while True:
        now = time.time()
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run pytest tests/providers/skypilot/test_watchdog_fire_conditions.py -v`
Expected: 6 passed

- [ ] **Step 5: Lint + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/providers/skypilot/watchdog.py \
  tests/providers/skypilot/test_watchdog_fire_conditions.py
git add src/kinoforge/providers/skypilot/watchdog.py \
        tests/providers/skypilot/test_watchdog_fire_conditions.py
git commit -m "feat(skypilot): render the instance-side deadline watchdog"
```

---

### Task 2: The arming prelude and its idempotency

**Goal:** `RENDER_ARM` produces the bash prelude; running it twice leaves exactly one watchdog and
the refreshed deadline.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/watchdog.py`
- Test: `tests/providers/skypilot/test_watchdog_arm_idempotency.py`

**Acceptance Criteria:**
- [ ] Running the rendered snippet writes `deadline`, `watchdog.py`, and `pid` under `$KF_WD_DIR`
- [ ] A second run with a different deadline leaves the pid file unchanged and updates `deadline`
- [ ] Exactly one stub interpreter process is alive after both runs
- [ ] The second run's stdout says `already armed`

**Verify:** `pixi run pytest tests/providers/skypilot/test_watchdog_arm_idempotency.py -v` → 4 passed

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
"""Idempotency of the SkyPilot watchdog arming prelude.

Behaviour under test: ``Task.setup`` re-runs on cluster reuse, so the arming
step must refresh the deadline WITHOUT spawning a second watchdog. Two
watchdogs racing would be worse than none — each would fire on its own copy
of the deadline.

The test runs the REAL rendered bash in a temp ``KF_WD_DIR`` with a stub
interpreter, because the guarantee lives in the shell, not in python.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

from kinoforge.providers.skypilot.watchdog import RENDER_ARM

_STUB = """#!/bin/sh
# Stands in for python3: sleeps so the pid stays alive for the guard check.
sleep 300
"""


@pytest.fixture
def stub_python(tmp_path: Path) -> Path:
    """A tiny long-running executable used instead of python3."""
    path = tmp_path / "stub-python"
    path.write_text(_STUB)
    path.chmod(0o755)
    return path


def _arm(wd_dir: Path, stub: Path, deadline: float, now: float) -> str:
    """Run the rendered arming snippet; return its combined output."""
    script = RENDER_ARM(deadline_epoch=deadline, now=now)
    completed = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "KF_WD_DIR": str(wd_dir),
            "KF_WD_PYTHON": str(stub),
            "HOME": str(wd_dir.parent),
        },
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout + completed.stderr


def _kill(wd_dir: Path) -> None:
    """Reap the stub watchdog so no process outlives the test."""
    pid_file = wd_dir / "pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def test_arming_writes_state_and_spawns_one_watchdog(
    tmp_path: Path, stub_python: Path
) -> None:
    """A single arm run leaves deadline, program, and a live pid.

    A bug this catches: writing the deadline but never spawning (a cluster
    that believes it is protected and is not).
    """
    wd_dir = tmp_path / "wd"
    try:
        out = _arm(wd_dir, stub_python, deadline=2_000_000_000.0, now=1_999_999_000.0)
        assert "armed pid" in out, out
        assert (wd_dir / "watchdog.py").exists()
        assert float((wd_dir / "deadline").read_text().strip()) == 2_000_000_000.0
        pid = int((wd_dir / "pid").read_text().strip())
        os.kill(pid, 0)  # raises if not alive
    finally:
        _kill(wd_dir)


def test_second_arm_refreshes_deadline_without_second_watchdog(
    tmp_path: Path, stub_python: Path
) -> None:
    """Re-running setup replaces the deadline and reuses the live watchdog.

    A bug this catches: an unconditional spawn, leaving two watchdogs racing
    on cluster reuse — the exact failure the design's pid guard exists for.
    """
    wd_dir = tmp_path / "wd"
    try:
        _arm(wd_dir, stub_python, deadline=2_000_000_000.0, now=1_999_999_000.0)
        first_pid = (wd_dir / "pid").read_text().strip()

        out = _arm(wd_dir, stub_python, deadline=2_000_000_500.0, now=1_999_999_000.0)

        assert "already armed" in out, out
        assert (wd_dir / "pid").read_text().strip() == first_pid
        assert float((wd_dir / "deadline").read_text().strip()) == 2_000_000_500.0
        alive = subprocess.run(
            ["pgrep", "-f", str(stub_python)], capture_output=True, text=True
        )
        pids = [line for line in alive.stdout.split() if line]
        assert len(pids) == 1, f"expected exactly one watchdog, got {pids!r}"
    finally:
        _kill(wd_dir)


def test_arm_snippet_is_the_first_content_it_renders(tmp_path: Path) -> None:
    """The rendered snippet leads with its marker comment.

    Task 3 concatenates it ahead of the provision script; this pins the
    marker other code and humans grep for.
    """
    script = RENDER_ARM(deadline_epoch=1.0, now=0.0)
    assert script.lstrip().startswith("# --- kinoforge watchdog arm"), script[:120]


def test_arm_schedules_kernel_poweroff_fallback(tmp_path: Path) -> None:
    """The snippet carries a `shutdown -h +N` fallback for a failed spawn.

    A bug this catches: dropping the fallback, so an instance with no usable
    python3 gets no bound at all.
    """
    script = RENDER_ARM(deadline_epoch=1_000_600.0, now=1_000_000.0)
    assert "shutdown -h +10" in script, script
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run pytest tests/providers/skypilot/test_watchdog_arm_idempotency.py -v`
Expected: `ImportError: cannot import name 'RENDER_ARM'`

- [ ] **Step 3: Implement `RENDER_ARM`**

The bash template uses `@` as the placeholder delimiter — bash is full of `$`, and
`string.Template`'s default would try to substitute `$HOME`, `$KF_WD_DIR`, `$!` and fail.

Append to `src/kinoforge/providers/skypilot/watchdog.py`:

```python
class _BashTemplate(Template):
    """Template with an ``@`` delimiter so bash's own ``$`` survives intact."""

    delimiter = "@"


#: Arming prelude prepended to ``Task.setup``. ``@``-placeholders only.
_ARM_TEMPLATE = _BashTemplate(
    '''\
# --- kinoforge watchdog arm (must stay first in setup) ---------------------
KF_WD_DIR="${KF_WD_DIR:-$HOME/.kinoforge-watchdog}"
KF_WD_PYTHON="${KF_WD_PYTHON:-python3}"
mkdir -p "$KF_WD_DIR"
printf '%s\\n' '@deadline_epoch' > "$KF_WD_DIR/deadline.tmp"
mv -f "$KF_WD_DIR/deadline.tmp" "$KF_WD_DIR/deadline"
cat > "$KF_WD_DIR/watchdog.py" <<'KF_WD_PY_EOF'
@watchdog_source
KF_WD_PY_EOF
if [ -f "$KF_WD_DIR/pid" ] && kill -0 "$(cat "$KF_WD_DIR/pid")" 2>/dev/null; then
  echo "[kinoforge-watchdog] already armed (pid $(cat "$KF_WD_DIR/pid")); deadline refreshed to @deadline_epoch"
else
  setsid nohup "$KF_WD_PYTHON" "$KF_WD_DIR/watchdog.py" >> "$KF_WD_DIR/watchdog.log" 2>&1 &
  echo $! > "$KF_WD_DIR/pid"
  echo "[kinoforge-watchdog] armed pid $(cat "$KF_WD_DIR/pid") deadline=@deadline_epoch"
fi
if ! kill -0 "$(cat "$KF_WD_DIR/pid" 2>/dev/null)" 2>/dev/null; then
  echo "[kinoforge-watchdog] spawn failed; scheduling kernel poweroff in @fallback_minutes min"
  sudo shutdown -h +@fallback_minutes || true
fi
# --- end kinoforge watchdog arm -------------------------------------------
'''
)


def RENDER_ARM(  # noqa: N802 — public, used as RENDER_ARM(...)
    *,
    deadline_epoch: float,
    now: float,
    poll_interval_s: float = 15.0,
    grace_before_halt_s: float = 120.0,
) -> str:
    """Render the bash prelude that arms the watchdog on the instance.

    The prelude is prepended to ``Task.setup`` so the deadline is armed
    before the heavy installs: a cluster that dies during a 20-minute pip
    install is still covered.

    Idempotent by construction — ``Task.setup`` re-runs on cluster reuse, so
    the snippet refreshes the deadline file and skips the spawn when a live
    watchdog pid is already recorded. Never leaves two watchdogs racing.

    Failure of the spawn does NOT abort setup: SkyPilot deliberately leaves a
    cluster up when setup fails ("for debugging purposes"), so aborting would
    produce precisely the unbounded-billing cluster this exists to prevent.
    Instead the snippet best-effort schedules ``sudo shutdown -h +N``, a
    kernel-timed poweroff needing no daemon.

    Args:
        deadline_epoch: Absolute POSIX epoch from :func:`compute_deadline`.
        now: POSIX epoch at render time; used only to size the
            ``shutdown -h +N`` fallback.
        poll_interval_s: Passed through to :func:`RENDER_WATCHDOG`.
        grace_before_halt_s: Passed through to :func:`RENDER_WATCHDOG`.

    Returns:
        A bash snippet, safe to concatenate ahead of a provision script.

    Example:
        >>> RENDER_ARM(deadline_epoch=60.0, now=0.0).lstrip()[:26]
        '# --- kinoforge watchdog a'
    """
    fallback_minutes = max(1, math.ceil((deadline_epoch - now) / 60.0))
    return _ARM_TEMPLATE.substitute(
        deadline_epoch=repr(float(deadline_epoch)),
        watchdog_source=RENDER_WATCHDOG(
            poll_interval_s=poll_interval_s,
            grace_before_halt_s=grace_before_halt_s,
        ),
        fallback_minutes=fallback_minutes,
    )
```

If the `repr(float(...))` form makes the idempotency test's float comparison awkward, keep it —
`float("2000000000.0") == 2_000_000_000.0` holds.

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run pytest tests/providers/skypilot/test_watchdog_arm_idempotency.py -v`
Expected: 4 passed

- [ ] **Step 5: Lint + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/providers/skypilot/watchdog.py \
  tests/providers/skypilot/test_watchdog_arm_idempotency.py
git add src/kinoforge/providers/skypilot/watchdog.py \
        tests/providers/skypilot/test_watchdog_arm_idempotency.py
git commit -m "feat(skypilot): arm the deadline watchdog idempotently at top of setup"
```

---

### Task 3: Wire arming and `down=True` into `create_instance`

**Goal:** Every launched cluster carries the arming step at the top of `setup` and autodowns
(terminates) rather than autostops.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py` (module docstring ~43-51; `__init__`
  507-566; `create_instance` 677-815)
- Test: `tests/providers/test_skypilot.py`

**Acceptance Criteria:**
- [ ] `task_config["setup"]` exists on every launch, including specs with no provision script
- [ ] The arming marker's index in `setup` is lower than the first provision-script line's index
- [ ] `launch_kwargs["down"] is True` by default; `SkyPilotProvider(autodown=False)` sends `False`
- [ ] `idle_minutes_to_autostop` is still passed (correct for one-shot configs)
- [ ] The deadline baked into `setup` equals `compute_deadline(...)` for the spec's lifecycle+offer
- [ ] Module docstring no longer presents autostop as the SkyPilot cost model

**Verify:** `pixi run pytest tests/providers/test_skypilot.py -v` → all pass (existing + 5 new)

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/test_skypilot.py`:

```python
# ---------------------------------------------------------------------------
# Watchdog arming + autodown (2026-08-15 instance-deadline design)
# ---------------------------------------------------------------------------


def _watchdog_spec(**overrides: Any) -> InstanceSpec:
    """Build a minimal InstanceSpec for the watchdog-wiring tests."""
    base: dict[str, Any] = {
        "run_id": "kf-watchdog-unit",
        "image": "",
        "env": {},
        "tags": {},
        "lifecycle": Lifecycle(idle_timeout_s=600, max_lifetime_s=18000),
        "offer": Offer(
            id="sky-cpu-auto",
            provider="skypilot",
            gpu_type="",
            vram_gb=0,
            cost_rate_usd_per_hr=0.0,
            region="us-west-2",
        ),
        "provision_script": "",
        "run_cmd": [],
    }
    base.update(overrides)
    return InstanceSpec(**base)


def test_setup_is_always_present_and_carries_the_arming_step() -> None:
    """Even a provision-script-less spec ships with an armed watchdog.

    A bug this catches: keeping the old ``if spec.provision_script:`` guard,
    which leaves server-less deploys (and the CPU smoke) with no setup at all
    and therefore no deadline.
    """
    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)
    provider.create_instance(_watchdog_spec())
    config = fake.Task.from_yaml_config_calls[-1]
    assert "setup" in config, f"no setup key in {sorted(config)!r}"
    assert "# --- kinoforge watchdog arm" in config["setup"]


def test_arming_precedes_the_provision_script() -> None:
    """The watchdog is armed before the heavy installs, not after.

    A bug this catches: appending the arming step, so a cluster that dies
    during a 20-minute pip install is never covered — the exact window the
    design calls out.
    """
    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)
    provider.create_instance(
        _watchdog_spec(provision_script="pip install --quiet torch\n")
    )
    setup = fake.Task.from_yaml_config_calls[-1]["setup"]
    assert setup.index("# --- kinoforge watchdog arm") < setup.index(
        "pip install --quiet torch"
    ), setup


def test_setup_deadline_matches_compute_deadline() -> None:
    """The baked deadline is launch + max_lifetime_s for an unknown rate.

    A bug this catches: passing ``idle_timeout_s`` or a boot-relative
    duration into the arming step instead of the computed absolute epoch.
    """
    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)
    before = time.time()
    provider.create_instance(_watchdog_spec())
    after = time.time()
    setup = fake.Task.from_yaml_config_calls[-1]["setup"]
    match = re.search(r"deadline=([0-9.]+)", setup)
    assert match is not None, setup
    baked = float(match.group(1))
    assert before + 18000 <= baked <= after + 18000, (
        f"baked deadline {baked} outside [launch+18000] window "
        f"[{before + 18000}, {after + 18000}]"
    )


def test_create_instance_passes_down_true_by_default() -> None:
    """Autodown terminates; plain autostop leaves a billing disk (F2).

    A bug this catches: shipping ``down`` unset, so any autostop that does
    fire stops the instance and keeps paying for its disk.
    """
    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)
    provider.create_instance(_watchdog_spec())
    _, kwargs = fake.launch_calls[-1]
    assert kwargs.get("down") is True, kwargs
    assert "idle_minutes_to_autostop" in kwargs, kwargs


def test_autodown_false_opts_out() -> None:
    """A workflow needing stop-and-restart can turn autodown off.

    A bug this catches: hard-coding ``down=True`` with no escape hatch.
    """
    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake, autodown=False)
    provider.create_instance(_watchdog_spec())
    _, kwargs = fake.launch_calls[-1]
    assert kwargs.get("down") is False, kwargs
```

Check `_FakeSky`'s recorded-call attribute names before writing (`launch_calls`,
`Task.from_yaml_config_calls`) and adapt these accessors to whatever the existing fake exposes —
do not rename the fake's attributes. `Offer`'s constructor kwargs likewise: mirror an existing
`Offer(...)` construction in that file rather than the sketch above.

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run pytest tests/providers/test_skypilot.py -k "watchdog or down_true or autodown or arming or deadline" -v`
Expected: FAIL — `KeyError: 'setup'`, `assert None is True`, `TypeError: unexpected keyword argument 'autodown'`

- [ ] **Step 3: Implement**

In `src/kinoforge/providers/skypilot/__init__.py`:

1. Add the import near the other in-package imports:

```python
from kinoforge.providers.skypilot import watchdog
```

2. Add the ctor arg (alongside `retry_until_up`):

```python
        autodown: bool = True,
```

with, in `__init__`:

```python
        self._autodown = autodown
```

and this docstring entry:

```
            autodown: When ``True`` (default) ``down=True`` is passed to
                ``sky.launch`` so an autostop TERMINATES the cluster instead
                of stopping it — a stopped cluster keeps billing its disk
                (finding F2). Set ``False`` only for a workflow that
                genuinely needs stop-and-restart onto the same disk;
                kinoforge has none today (``stop_instance`` is a no-op).
```

3. In `create_instance`, replace the setup block:

```python
        launch_epoch = time.time()
        deadline_epoch = watchdog.compute_deadline(
            launch_epoch=launch_epoch,
            max_lifetime_s=spec.lifecycle.max_lifetime_s,
            budget_usd=spec.lifecycle.budget_usd,
            rate_usd_per_hr=(
                spec.offer.cost_rate_usd_per_hr if spec.offer is not None else 0.0
            ),
        )
        # The watchdog is armed at the TOP of setup, before the provision
        # script's installs: SkyPilot autostop cannot fire for a server-mode
        # deploy (F1), so this is the only guardrail that survives the
        # orchestrator dying.
        setup_parts: list[str] = [
            watchdog.RENDER_ARM(deadline_epoch=deadline_epoch, now=launch_epoch)
        ]
        if spec.provision_script:
            setup_parts.append(_strip_trailing_exec(spec.provision_script))
        task_config["setup"] = "\n".join(setup_parts)
        if spec.run_cmd:
            task_config["run"] = " ".join(shlex.quote(c) for c in spec.run_cmd)
```

4. Add `down` to the launch kwargs:

```python
        launch_kwargs: dict[str, Any] = {
            "cluster_name": cluster_name,
            "idle_minutes_to_autostop": autostop_minutes,
            "down": self._autodown,
        }
```

5. Replace the autostop paragraph in the module docstring with:

```
Cost model
----------
``idle_timeout_s`` is still mapped to SkyPilot's ``idle_minutes_to_autostop``,
but that mechanism is INERT for kinoforge's server-mode deploys: ``run_cmd``
becomes ``Task.run``, a job that never terminates, so ``is_cluster_idle()``
is permanently False and the 60 s ``AutostopEvent`` tick resets the idleness
timer forever (finding F1, verified against skypilot-0.12.3.post1). It
remains correct for the one-shot configs whose ``run`` terminates.

The real guardrail is the instance-side deadline watchdog
(:mod:`kinoforge.providers.skypilot.watchdog`), armed at the top of
``Task.setup`` and enforced on the machine, so it survives the orchestrator
process dying. ``down=True`` makes any autostop that DOES fire terminate
rather than stop (a stopped cluster keeps billing its disk — finding F2).
```

6. Update `test_ac4_create_instance_passes_idle_minutes_to_autostop`'s docstring in
   `tests/providers/test_skypilot.py` so it no longer describes the kwarg as the SkyPilot cost
   backstop; keep its assertions unchanged.

- [ ] **Step 4: Run to verify they pass**

Run: `pixi run pytest tests/providers/test_skypilot.py -v`
Expected: all pass, including the 5 new tests

- [ ] **Step 5: Full suite + commit**

```bash
pixi run pytest tests/providers -q
pixi run pre-commit run --all-files
git add src/kinoforge/providers/skypilot/__init__.py tests/providers/test_skypilot.py
git commit -m "feat(skypilot): arm the instance deadline watchdog and autodown on launch"
```

---

### Task 4: Pre-launch durable ledger row

**Goal:** A cluster is recorded durably BEFORE `sky.launch` is called, so a process death during
the multi-minute launch still leaves something `kinoforge list` / `destroy` can find.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py` (`__init__`, `create_instance`)
- Modify: `src/kinoforge/core/orchestrator.py` (after `_resolve_provider` at `:1143`)
- Test: `tests/providers/test_skypilot.py`

**Acceptance Criteria:**
- [ ] With a ledger installed, `record` is called before `sky.launch` (proved by a shared
      call-sequence list, not by mock call counts)
- [ ] The provisional row carries `id`, `provider="skypilot"`, the offer's rate, and tags
      `kf_launch_phase="launching"`, `kf_cloud`, `kf_run_id`, `kf_launched_at`, `kf_deadline_epoch`
- [ ] The success path calls `forget(cluster_name)` so the orchestrator's post-create `record`
      is the only surviving row
- [ ] The tunnel-failure path does NOT call `forget` (that row is the orphan record)
- [ ] With no ledger installed the provider behaves exactly as before (no crash, no writes)

**Verify:** `pixi run pytest tests/providers/test_skypilot.py -k ledger -v` → 5 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/test_skypilot.py`:

```python
class _RecordingLedger:
    """Ledger-shaped fake sharing one call-sequence list with the fake sky."""

    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.recorded: list[Any] = []
        self.forgotten: list[str] = []

    def record(self, instance: Any, **kwargs: Any) -> None:
        self.sequence.append("record")
        self.recorded.append(instance)

    def forget(self, instance_id: str) -> None:
        self.sequence.append("forget")
        self.forgotten.append(instance_id)


def test_ledger_row_is_written_before_launch() -> None:
    """The durable record exists BEFORE sky.launch is entered (F12).

    A bug this catches: recording after ``create_instance`` returns — which
    is what HEAD does via ``on_instance_created`` — leaving a SIGKILL during
    the multi-minute launch with a billing cluster no kinoforge command can
    see.
    """
    sequence: list[str] = []
    fake = _FakeSky()
    original_launch = fake.launch

    def _recording_launch(task: Any, **kwargs: Any) -> Any:
        sequence.append("launch")
        return original_launch(task, **kwargs)

    fake.launch = _recording_launch  # type: ignore[method-assign]
    ledger = _RecordingLedger(sequence)
    provider = SkyPilotProvider(sky_client=fake)
    provider.set_launch_ledger(ledger)

    provider.create_instance(_watchdog_spec())

    assert sequence[:2] == ["record", "launch"], sequence


def test_provisional_row_carries_enough_to_find_and_destroy() -> None:
    """The row names the cluster, its cloud, its rate and its deadline.

    A bug this catches: a row with only an id — a later sweep could not tell
    which cloud to reach, what the run cost, or whether it is already past
    its deadline.
    """
    fake = _FakeSky()
    ledger = _RecordingLedger([])
    provider = SkyPilotProvider(sky_client=fake, clouds=["aws"])
    provider.set_launch_ledger(ledger)

    provider.create_instance(_watchdog_spec(run_id="kf-provisional"))

    row = ledger.recorded[0]
    assert row.id == "kf-provisional"
    assert row.provider == "skypilot"
    assert row.tags["kf_launch_phase"] == "launching"
    assert row.tags["kf_cloud"] == "aws"
    assert row.tags["kf_run_id"] == "kf-provisional"
    assert float(row.tags["kf_deadline_epoch"]) > float(row.tags["kf_launched_at"])


def test_success_path_forgets_the_provisional_row() -> None:
    """On success the provisional row is removed, avoiding a duplicate.

    The orchestrator's ``on_instance_created`` writes the real row; Ledger
    .record appends, so leaving both would show one cluster twice in
    ``kinoforge list``.

    A bug this catches: skipping the forget and shipping duplicate rows.
    """
    fake = _FakeSky()
    ledger = _RecordingLedger([])
    provider = SkyPilotProvider(sky_client=fake)
    provider.set_launch_ledger(ledger)

    provider.create_instance(_watchdog_spec(run_id="kf-forget-me"))

    assert ledger.forgotten == ["kf-forget-me"]


def test_tunnel_failure_keeps_the_provisional_row() -> None:
    """A cluster whose tunnel failed stays recorded — it may still be alive.

    ``create_instance`` best-effort ``sky.down``s on tunnel failure; if that
    down also fails the cluster bills on. That is exactly the orphan the row
    exists for.

    A bug this catches: forgetting in a blanket ``finally``, deleting the
    record precisely when it matters most.
    """

    def _boom(cluster: str, local_port: int, remote_port: int) -> Any:
        raise OSError("ssh refused")

    fake = _FakeSky()
    ledger = _RecordingLedger([])
    provider = SkyPilotProvider(sky_client=fake, ssh_spawn=_boom)
    provider.set_launch_ledger(ledger)

    with pytest.raises(ProvisionFailed):
        provider.create_instance(
            _watchdog_spec(run_id="kf-orphan", run_cmd=["sleep", "1"])
        )

    assert ledger.forgotten == [], ledger.forgotten
    assert ledger.recorded and ledger.recorded[0].id == "kf-orphan"


def test_no_ledger_installed_is_a_no_op() -> None:
    """Without a ledger the provider still launches normally.

    A bug this catches: an unconditional ledger call, breaking every existing
    construction of the provider (all unit tests, the live smokes).
    """
    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)
    inst = provider.create_instance(_watchdog_spec(run_id="kf-no-ledger"))
    assert inst.id == "kf-no-ledger"
```

Import `ProvisionFailed` from `kinoforge.core.errors` at the top of the test module if it is not
already imported.

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run pytest tests/providers/test_skypilot.py -k ledger -v`
Expected: FAIL — `AttributeError: 'SkyPilotProvider' object has no attribute 'set_launch_ledger'`

- [ ] **Step 3: Implement**

In `providers/skypilot/__init__.py`:

```python
class _LaunchLedger(Protocol):
    """The slice of :class:`~kinoforge.core.lifecycle.Ledger` used pre-launch."""

    def record(self, instance: Instance, **kwargs: Any) -> None:  # noqa: ANN401
        """Append a row for *instance*."""

    def forget(self, instance_id: str) -> None:
        """Remove the row for *instance_id*."""
```

In `__init__`, add `self._launch_ledger: _LaunchLedger | None = None`, plus:

```python
    def set_launch_ledger(self, ledger: _LaunchLedger) -> None:
        """Install the ledger used for the pre-launch provisional row.

        Duck-typed rather than an ABC method: :class:`ComputeProvider` is out
        of scope for this change, and ``kinoforge.core`` must not import
        provider modules. The orchestrator calls this via ``getattr``.

        Args:
            ledger: A :class:`~kinoforge.core.lifecycle.Ledger`-shaped object.
        """
        self._launch_ledger = ledger
```

In `create_instance`, immediately before `raw = sky.launch(...)`:

```python
        # F12 — durable record BEFORE the launch. sky.launch is a multi-minute
        # call; a SIGKILL inside it would otherwise leave a billing cluster
        # that no kinoforge command can see. Forgotten on the success path so
        # the orchestrator's post-create record is the only surviving row.
        provisional = Instance(
            id=cluster_name,
            provider=self.name,
            status="starting",
            created_at=launch_epoch,
            endpoints={},
            tags={
                **dict(spec.tags),
                "kf_launch_phase": "launching",
                "kf_cloud": ",".join(self._clouds) if self._clouds else "auto",
                "kf_run_id": spec.run_id or cluster_name,
                "kf_launched_at": repr(launch_epoch),
                "kf_deadline_epoch": repr(deadline_epoch),
            },
            cost_rate_usd_per_hr=(
                spec.offer.cost_rate_usd_per_hr if spec.offer is not None else 0.0
            ),
        )
        if self._launch_ledger is not None:
            self._launch_ledger.record(
                provisional, max_age_s=int(spec.lifecycle.max_lifetime_s)
            )
        raw = sky.launch(task, **launch_kwargs)
```

and immediately before the successful `return Instance(...)`:

```python
        # Success: hand the record over to the orchestrator's post-create
        # write. Deliberately NOT in a finally — the tunnel-failure path
        # must keep its row, because a failed best-effort ``sky.down``
        # leaves a live cluster that only this row can surface.
        if self._launch_ledger is not None:
            self._launch_ledger.forget(cluster_name)
```

In `core/orchestrator.py`, after `resolved_provider = _resolve_provider(cfg, provider)` at `:1143`:

```python
        # F12 — hand the provider a ledger so it can write a durable row
        # BEFORE its (multi-minute) create call. Duck-typed: core must not
        # import provider modules, and ComputeProvider's ABC is out of scope.
        _install_launch_ledger = getattr(
            resolved_provider, "set_launch_ledger", None
        )
        if _install_launch_ledger is not None:
            _install_launch_ledger(Ledger(store=store))
```

`deploy_session` is the CLI path and the one that matters; the bare `deploy()` entry point at
`:1571` has no `store` in scope and is left unwired — note this in the design doc's §6 when
committing.

- [ ] **Step 4: Run to verify they pass**

Run: `pixi run pytest tests/providers/test_skypilot.py -v && pixi run pytest tests/core/test_orchestrator*.py -q`
Expected: all pass

- [ ] **Step 5: Full suite + commit**

```bash
pixi run pytest -q
pixi run pre-commit run --all-files
git add src/kinoforge/providers/skypilot/__init__.py src/kinoforge/core/orchestrator.py \
        tests/providers/test_skypilot.py \
        docs/superpowers/specs/2026-08-15-skypilot-instance-deadline-watchdog-design.md
git commit -m "feat(skypilot): write a durable ledger row before sky.launch"
```

---

### Task 5: RED live-smoke scaffold (committed before any spend)

**Goal:** The live AWS smoke exists, is committed, and fails/skips cleanly WITHOUT spending —
per `CLAUDE.md`, the scaffold must be in git before the spend is invoked.

**Files:**
- Create: `tests/live/test_skypilot_watchdog_smoke.py`

**Acceptance Criteria:**
- [ ] Module skips cleanly when `KINOFORGE_LIVE_TESTS != 1` (default) — no cloud call
- [ ] Teardown lives in a `finally` and runs `sky down` even when the assertions fail
- [ ] The pass condition queries EC2 directly (`aws ec2 describe-instances` filtered on
      `tag:ray-cluster-name`), not `sky status`
- [ ] Committed before any live run

**Verify:** `pixi run pytest tests/live/test_skypilot_watchdog_smoke.py -v` → 1 skipped, exit 0

**Steps:**

- [ ] **Step 1: Write the smoke**

```python
"""Opt-in live smoke: a SkyPilot cluster dies without its client.

Launches the cheapest AWS CPU SKU in us-west-2 with a 5-minute deadline and
a run command that sleeps far past it, then drops every in-process handle —
no destroy call is ever made. Pass condition: EC2 itself reports the instance
``shutting-down`` or ``terminated``. ``sky status`` is deliberately NOT the
oracle: the cluster vanishing from sky's local state would prove nothing
about the money.

Gated by (module-level skip if any is missing):
  - ``KINOFORGE_LIVE_TESTS=1``
  - AWS credentials reachable by boto3/awscli
  - ``import sky`` succeeds (use ``pixi run -e live-skypilot``)

Cost: < $0.05 (cheapest CPU SKU ~$0.01/hr, <= ~12 min wall-clock).
Design: docs/superpowers/specs/2026-08-15-skypilot-instance-deadline-watchdog-design.md
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import time
from typing import Any

import pytest

pytestmark = pytest.mark.live

_REASONS: list[str] = []
if os.getenv("KINOFORGE_LIVE_TESTS") != "1":
    _REASONS.append("KINOFORGE_LIVE_TESTS=1 required")
try:
    import sky  # type: ignore[import-not-found, unused-ignore]  # noqa: F401
except ImportError:
    _REASONS.append("skypilot not installed (use `pixi run -e live-skypilot`)")

if _REASONS:
    pytest.skip(
        "SkyPilot watchdog smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

from kinoforge.core.interfaces import (  # noqa: E402
    HardwareRequirements,
    InstanceSpec,
    Lifecycle,
)
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402

_log = logging.getLogger(__name__)

_REGION = "us-west-2"
_DEADLINE_S = 300.0
#: Deadline + skylet tick (<=60 s) + terminate + generous slack.
_KILL_TIMEOUT_S = 900.0
_POLL_INTERVAL_S = 30.0
_DEAD_STATES = {"shutting-down", "terminated"}


def _ec2_states(cluster_name: str) -> list[str]:
    """Return EC2 instance states tagged with this sky cluster name."""
    completed = subprocess.run(
        [
            "aws", "ec2", "describe-instances",
            "--region", _REGION,
            "--filters", f"Name=tag:ray-cluster-name,Values={cluster_name}",
            "--query", "Reservations[].Instances[].State.Name",
            "--output", "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        _log.warning("describe-instances failed: %s", completed.stderr.strip())
        return []
    return list(json.loads(completed.stdout or "[]"))


def _teardown(cluster_name: str, tunnel: Any) -> None:
    """Teardown that must run whatever the assertions did."""
    if tunnel is not None:
        try:
            tunnel.kill()
        except Exception as exc:  # noqa: BLE001
            _log.warning("tunnel kill raised: %r", exc)
    try:
        sky.down(cluster_name, purge=True)
    except Exception as exc:  # noqa: BLE001
        _log.warning("sky.down raised (expected when already gone): %r", exc)
    survivors = [s for s in _ec2_states(cluster_name) if s not in _DEAD_STATES]
    if survivors:
        raise RuntimeError(
            f"cluster {cluster_name!r} survived teardown with states {survivors!r} "
            f"— destroy it by hand in {_REGION}"
        )


def test_skypilot_cluster_dies_without_its_client() -> None:
    """A 5-minute deadline kills the instance with no client involvement.

    A bug this catches: the arming step never reaching the instance, the
    watchdog dying with the setup shell, or stage 1 failing with no stage 2 —
    each of which leaves a cluster billing indefinitely once the CLI is gone.
    """
    cluster_name = f"kinoforge-wd-{secrets.token_hex(4)}"
    provider = SkyPilotProvider(clouds=["aws"], region=_REGION, retry_until_up=True)
    tunnel: Any = None
    try:
        offers = provider.find_offers(HardwareRequirements(min_vram_gb=0, min_cuda="0.0"))
        assert offers, "no CPU offer surfaced from find_offers"
        spec = InstanceSpec(
            run_id=cluster_name,
            image="",
            env={},
            tags={"smoke": "skypilot-watchdog"},
            lifecycle=Lifecycle(idle_timeout_s=600, max_lifetime_s=_DEADLINE_S),
            offer=offers[0],
            provision_script="",
            # Never terminates -> the cluster can never go idle. This is the
            # exact server-mode shape that makes autostop inert (F1).
            run_cmd=["sleep", "3600"],
        )
        _log.info("launching %s in %s with a %.0fs deadline", cluster_name, _REGION, _DEADLINE_S)
        launched_at = time.time()
        provider.create_instance(spec)

        # The client is now gone: kill the tunnel, never call destroy.
        tunnel = provider._tunnels.pop(cluster_name, None)
        if tunnel is not None:
            tunnel.kill()
            tunnel = None

        deadline = time.time() + _KILL_TIMEOUT_S
        states: list[str] = []
        while time.time() < deadline:
            states = _ec2_states(cluster_name)
            _log.info(
                "t+%.0fs ec2 states=%r", time.time() - launched_at, states
            )
            if states and all(s in _DEAD_STATES for s in states):
                break
            if not states:
                break  # already reaped and de-registered
            time.sleep(_POLL_INTERVAL_S)

        elapsed = time.time() - launched_at
        assert states == [] or all(s in _DEAD_STATES for s in states), (
            f"instance still alive {elapsed:.0f}s after launch: states={states!r}"
        )
        _log.info(
            "SMOKE RESULT cluster=%s wall_clock_to_termination=%.0fs states=%r",
            cluster_name,
            elapsed,
            states,
        )
    finally:
        _teardown(cluster_name, tunnel)
```

- [ ] **Step 2: Confirm it skips without spending**

Run: `pixi run pytest tests/live/test_skypilot_watchdog_smoke.py -v`
Expected: `1 skipped` (no `KINOFORGE_LIVE_TESTS`), exit 0, zero cloud calls

- [ ] **Step 3: Commit the RED scaffold before any spend**

```bash
pixi run pre-commit run --files tests/live/test_skypilot_watchdog_smoke.py
git add tests/live/test_skypilot_watchdog_smoke.py
git commit -m "test(skypilot): add the RED live watchdog smoke scaffold"
```

---

### Task 6: Run the live smoke and record the result

**Goal:** Prove on real AWS that the cluster terminates itself with no client, and write the
evidence into the design doc and `PROGRESS.md`.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `docs/superpowers/specs/2026-08-15-skypilot-instance-deadline-watchdog-design.md` (§4.3)
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 before the spend
- [ ] The smoke passes: EC2 reports the instance `terminated`/`shutting-down`, or no instance
      carries the cluster tag
- [ ] Post-run `aws ec2 describe-instances --region us-west-2` shows no survivor with the cluster tag
- [ ] Design doc §4.3 records cluster name, wall-clock launch→termination, which stage fired
      (read `~/.kinoforge-watchdog/watchdog.log` via `sky logs` before the instance dies, or infer
      from the elapsed time: stage 1 fires at the deadline, stage 2 at deadline+120 s)
- [ ] Actual cost recorded (AWS Cost Explorer or the offer rate × wall clock)
- [ ] `PROGRESS.md` RESUME SNAPSHOT points at this workstream as complete

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot pytest tests/live/test_skypilot_watchdog_smoke.py -v -s` → 1 passed

**Steps:**

- [ ] **Step 1: Preflight**

```bash
pixi run preflight
```

Expected: exit 0 (creds present, no active pods, clean tree). If it fails, STOP and report —
do not spend.

- [ ] **Step 2: Confirm AWS identity and region**

```bash
pixi run -e live-skypilot aws sts get-caller-identity
pixi run -e live-skypilot sky check aws
```

Expected: the `kinoforge-ci` identity; `sky check` reports AWS enabled.

- [ ] **Step 3: Run the smoke with live polling**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot \
  pytest tests/live/test_skypilot_watchdog_smoke.py -v -s
```

The test logs `t+<n>s ec2 states=[...]` every 30 s — that IS the polling loop the project's
live-smoke rule requires. Watch it. If states stay `running` past deadline+180 s, capture the
instance console output before the timeout fires:

```bash
pixi run -e live-skypilot sky logs <cluster> --no-follow
pixi run -e live-skypilot ssh <cluster> cat '~/.kinoforge-watchdog/watchdog.log'
```

- [ ] **Step 4: Verify no survivor**

```bash
pixi run -e live-skypilot aws ec2 describe-instances --region us-west-2 \
  --filters Name=tag:ray-cluster-name,Values=<cluster> \
  --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text
pixi run -e live-skypilot sky status
```

Expected: no running instance for the cluster tag.

- [ ] **Step 5: Record the evidence**

Replace §4.3 of the design doc with the real numbers (cluster name, launch epoch, wall clock to
`terminated`, which stage fired, cost). Update `PROGRESS.md`'s RESUME SNAPSHOT with a bullet for
this workstream: spec path, plan path, commits, live result, and the next action.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add docs/superpowers/specs/2026-08-15-skypilot-instance-deadline-watchdog-design.md PROGRESS.md
git commit -m "docs(skypilot): record the live watchdog smoke result"
```

---

## Self-review

**Spec coverage:** §3.1 → Tasks 0-2. §3.2 → Task 3. §3.3 → Task 4. §3.4 → Task 3 step 3.5.
§4.1 U1-U3 → Task 0; U4, U5, U8 → Task 3; U6 → Task 2; U7 → Task 1; U9, U10 → Task 4.
§4.2 → Task 5. §4.3 → Task 6. No spec section is unclaimed.

**Naming consistency:** `compute_deadline`, `RENDER_WATCHDOG`, `RENDER_ARM`, `set_launch_ledger`,
`_launch_ledger`, `_autodown`, `KF_WD_DIR`, `KF_WD_PYTHON`, `kf_launch_phase` are used identically
in every task.

**Known gap, deliberately carried:** the bare `deploy()` entry point (`orchestrator.py:1571`) has
no `store` in scope, so it gets the watchdog but no pre-launch ledger row. Recorded in Task 4 and
in the design doc's out-of-scope list rather than fixed here, because plumbing a store into
`deploy()` changes a public signature — Brief 2/5 territory.
