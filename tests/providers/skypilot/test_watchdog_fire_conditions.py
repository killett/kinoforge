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
    assert sub.calls == [], (
        f"expected no commands before the deadline, got {sub.calls!r}"
    )


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
    assert len(sub.calls) == 1, (
        f"expected exactly one stage-1 command, got {sub.calls!r}"
    )
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
    assert len(sub.calls) == 1, (
        f"expected stage 1 only inside the grace window: {sub.calls!r}"
    )


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

    ns["time"].sleep = _sleep_then_extend
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
    assert len(sub.calls) == 1, (
        f"stage 1 must not run without a skylet python: {sub.calls!r}"
    )
    assert "shutdown -h now" in sub.calls[0], sub.calls[0]
