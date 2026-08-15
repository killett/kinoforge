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
