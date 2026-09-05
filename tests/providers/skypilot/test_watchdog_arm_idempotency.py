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
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from kinoforge.providers.skypilot.watchdog import RENDER_ARM

_STUB = """#!/bin/sh
# Stands in for python3: sleeps so the pid stays alive for the guard check.
sleep 300
"""

_ENV_DUMP_STUB = """#!/bin/sh
# Stands in for python3 for the export test: snapshots its OWN inherited
# environment next to the watchdog.py path it was given, then sleeps like a
# real watchdog so the pid stays alive for the guard check.
env > "$(dirname "$1")/child-env.txt" 2>/dev/null
sleep 300
"""

# F2 — the arming prelude used to call the REAL `sudo` binary directly
# (`sudo shutdown -c`, `sudo shutdown -h +N`). On any host with passwordless
# sudo (CI, a dev laptop) that cancels the operator's real scheduled
# shutdowns and then schedules a real poweroff — this suite must never let
# that happen. Every test below points KF_WD_SUDO at this recording stub
# instead: it never touches privileged state, always "succeeds" (exit 0) so
# it doesn't perturb the prelude's control flow, and records exactly what it
# was called with so tests can assert on it.
_SUDO_STUB = """#!/bin/sh
echo "$*" >> "${KF_WD_DIR:-/tmp}/sudo-calls.log"
exit 0
"""

# A pid outside any realistic pid_max (Linux caps well under 1e9) - never a
# live process, used to exercise the "truly dead pid" respawn path without
# depending on which pids happen to be free on the test host.
_DEAD_PID = 999_999_937


@pytest.fixture
def stub_python(tmp_path: Path) -> Path:
    """A tiny long-running executable used instead of python3."""
    path = tmp_path / "stub-python"
    path.write_text(_STUB)
    path.chmod(0o755)
    return path


@pytest.fixture
def env_dump_python(tmp_path: Path) -> Path:
    """A stub interpreter that snapshots the environment it was spawned with."""
    path = tmp_path / "env-dump-python"
    path.write_text(_ENV_DUMP_STUB)
    path.chmod(0o755)
    return path


@pytest.fixture
def sudo_stub(tmp_path: Path) -> Path:
    """A recording stand-in for `sudo` — see the F2 note above `_SUDO_STUB`."""
    path = tmp_path / "stub-sudo"
    path.write_text(_SUDO_STUB)
    path.chmod(0o755)
    return path


def _sudo_calls(wd_dir: Path) -> list[str]:
    """Return the argv strings the sudo stub recorded, in call order."""
    log = wd_dir / "sudo-calls.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line]


def _arm(
    wd_dir: Path,
    stub: Path,
    deadline: float,
    now: float,
    sudo: Path,
    env_overrides: dict[str, str] | None = None,
) -> str:
    """Run the rendered arming snippet; return its combined output.

    ``env_overrides`` is layered last, so a test can pin e.g. ``PATH``.
    """
    script = RENDER_ARM(deadline_epoch=deadline, now=now)
    completed = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "KF_WD_DIR": str(wd_dir),
            "KF_WD_PYTHON": str(stub),
            "KF_WD_SUDO": str(sudo),
            "HOME": str(wd_dir.parent),
            **(env_overrides or {}),
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
    tmp_path: Path, stub_python: Path, sudo_stub: Path
) -> None:
    """A single arm run leaves deadline, program, and a live pid.

    A bug this catches: writing the deadline but never spawning (a cluster
    that believes it is protected and is not).

    Also asserts (F2/F3) exactly what the prelude invoked through the sudo
    indirection: a cancel followed by an unconditional kernel-backstop
    schedule, even though the spawn succeeded here.
    """
    wd_dir = tmp_path / "wd"
    try:
        out = _arm(
            wd_dir,
            stub_python,
            deadline=2_000_000_000.0,
            now=1_999_999_000.0,
            sudo=sudo_stub,
        )
        assert "armed pid" in out, out
        assert (wd_dir / "watchdog.py").exists()
        assert float((wd_dir / "deadline").read_text().strip()) == 2_000_000_000.0
        pid = int((wd_dir / "pid").read_text().strip())
        os.kill(pid, 0)  # raises if not alive
        calls = _sudo_calls(wd_dir)
        assert calls == ["shutdown -c", "shutdown -h +29"], calls
    finally:
        _kill(wd_dir)


def test_second_arm_refreshes_deadline_without_second_watchdog(
    tmp_path: Path, stub_python: Path, sudo_stub: Path
) -> None:
    """Re-running setup replaces the deadline and reuses the live watchdog.

    A bug this catches: an unconditional spawn, leaving two watchdogs racing
    on cluster reuse — the exact failure the design's pid guard exists for.
    """
    wd_dir = tmp_path / "wd"
    try:
        _arm(
            wd_dir,
            stub_python,
            deadline=2_000_000_000.0,
            now=1_999_999_000.0,
            sudo=sudo_stub,
        )
        first_pid = (wd_dir / "pid").read_text().strip()

        out = _arm(
            wd_dir,
            stub_python,
            deadline=2_000_000_500.0,
            now=1_999_999_000.0,
            sudo=sudo_stub,
        )

        assert "already armed" in out, out
        assert (wd_dir / "pid").read_text().strip() == first_pid
        assert float((wd_dir / "deadline").read_text().strip()) == 2_000_000_500.0
        alive = subprocess.run(
            ["pgrep", "-f", str(stub_python)], capture_output=True, text=True
        )
        pids = [line for line in alive.stdout.split() if line]
        assert len(pids) == 1, f"expected exactly one watchdog, got {pids!r}"
        # F3: each arm cancels-then-reschedules the backstop, so two arms
        # leave FOUR recorded calls, not a stacked pile of timers. The second
        # backstop is later than the first because it's sized off the second,
        # farther-out deadline — proof it isn't a leftover from the first arm.
        calls = _sudo_calls(wd_dir)
        assert calls == [
            "shutdown -c",
            "shutdown -h +29",
            "shutdown -c",
            "shutdown -h +38",
        ], calls
    finally:
        _kill(wd_dir)


def test_arm_snippet_is_the_first_content_it_renders(tmp_path: Path) -> None:
    """The rendered snippet leads with its marker comment.

    Task 3 concatenates it ahead of the provision script; this pins the
    marker other code and humans grep for.
    """
    script = RENDER_ARM(deadline_epoch=1.0, now=0.0)
    assert script.lstrip().startswith("# --- kinoforge watchdog arm"), script[:120]


def test_arm_schedules_kernel_poweroff_backstop_past_the_deadline(
    tmp_path: Path,
) -> None:
    """The snippet carries a `shutdown -h +N` backstop sized past the deadline.

    A bug this catches: dropping the backstop, so an instance with no usable
    python3 (or a daemon that later dies) gets no bound at all. N must land
    strictly after `deadline + grace_before_halt_s` (F3) — not merely at the
    deadline itself, which is what an unrelated regression back to the old
    "cover only until the deadline" sizing would produce.
    """
    script = RENDER_ARM(deadline_epoch=1_000_600.0, now=1_000_000.0)
    assert "shutdown -h +23" in script, script


def test_dead_pid_triggers_respawn(
    tmp_path: Path, stub_python: Path, sudo_stub: Path
) -> None:
    """A `pid` file naming a truly dead process must trigger a fresh spawn.

    A bug this catches: a guard that trusts the pid FILE's mere presence
    (e.g. `[ -f pid ]` with no liveness check at all) would report "already
    armed" for a dead watchdog and leave the instance with no enforcement.
    """
    wd_dir = tmp_path / "wd"
    wd_dir.mkdir()
    (wd_dir / "pid").write_text(str(_DEAD_PID))
    try:
        out = _arm(
            wd_dir,
            stub_python,
            deadline=2_000_000_000.0,
            now=1_999_999_000.0,
            sudo=sudo_stub,
        )
        assert "armed pid" in out, out
        assert "already armed" not in out, out
        pid = int((wd_dir / "pid").read_text().strip())
        assert pid != _DEAD_PID
        os.kill(pid, 0)  # raises if not alive
    finally:
        _kill(wd_dir)


def test_recycled_pid_does_not_suppress_arming(
    tmp_path: Path, stub_python: Path, sudo_stub: Path
) -> None:
    """A `pid` file naming a live but UNRELATED process must not block arming.

    A bug this catches: trusting `kill -0 "$(cat pid)"` alone treats any
    live process with that pid number as "the watchdog". If the real
    watchdog died and the OS recycled its pid onto an unrelated process
    (a decoy here), that check reports "already armed" AND the trailing
    backstop-shutdown check also sees a "live" pid and skips its
    `shutdown -h +N` too — zero enforcement, silently. The fix must
    identify the watchdog by command line (script path in the process
    table), not by pid number alone, so a same-numbered stranger can't
    impersonate it.
    """
    wd_dir = tmp_path / "wd"
    wd_dir.mkdir()
    decoy = subprocess.Popen([str(stub_python)])
    try:
        (wd_dir / "pid").write_text(str(decoy.pid))
        out = _arm(
            wd_dir,
            stub_python,
            deadline=2_000_000_000.0,
            now=1_999_999_000.0,
            sudo=sudo_stub,
        )
        assert "armed pid" in out, out
        assert "already armed" not in out, out
        real_pid = int((wd_dir / "pid").read_text().strip())
        assert real_pid != decoy.pid, "guard was fooled by the decoy's pid"
        os.kill(real_pid, 0)  # the REAL watchdog must be alive
    finally:
        decoy.kill()
        decoy.wait(timeout=5)
        _kill(wd_dir)


def test_unwritable_dir_does_not_abort_under_set_euo_pipefail(
    tmp_path: Path, stub_python: Path, sudo_stub: Path
) -> None:
    """The prelude must self-defend even when the caller's shell is strict.

    A bug this catches: an unguarded `mkdir`/`printf`/`mv`/`cat`/`echo`
    chain aborts under the CALLER's `set -e` (SkyPilot's own setup script
    may run under one) the moment any step fails against an unwritable
    directory. `Task.setup` then exits non-zero, and SkyPilot deliberately
    leaves a cluster UP when setup fails ("for debugging") — exactly the
    unbounded-billing cluster this watchdog exists to prevent. The prelude
    must swallow every internal failure and still exit 0.
    """
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)  # read + execute only: no write, no create
    wd_dir = parent / "wd"
    script = RENDER_ARM(deadline_epoch=2_000_000_000.0, now=1_999_999_000.0)
    try:
        completed = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "KF_WD_DIR": str(wd_dir),
                "KF_WD_PYTHON": str(stub_python),
                "KF_WD_SUDO": str(sudo_stub),
                "HOME": str(parent),
            },
            timeout=30,
        )
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
    finally:
        parent.chmod(0o700)
        _kill(wd_dir)


def test_watchdog_process_inherits_kf_wd_dir_via_export(
    tmp_path: Path, env_dump_python: Path, sudo_stub: Path
) -> None:
    """The spawned watchdog must see the SAME `KF_WD_DIR` the arm step wrote to.

    A bug this catches: the arm step resolves `KF_WD_DIR="${KF_WD_DIR:-...}"`
    as a plain (never `export`ed) shell variable when the caller didn't set
    one. `setsid nohup` then spawns the interpreter without it in its
    environment, so the daemon falls back to ITS OWN default and polls a
    DIFFERENT directory than the one the arm step just wrote `deadline` and
    `watchdog.py` into — `read_deadline()` returns None forever and the
    watchdog silently never fires, even though everything LOOKS armed.
    """
    home = tmp_path / "home"
    home.mkdir()
    wd_dir = home / ".kinoforge-watchdog"  # KF_WD_DIR left unset -> this default
    script = RENDER_ARM(deadline_epoch=2_000_000_000.0, now=1_999_999_000.0)
    env = {
        **os.environ,
        "KF_WD_PYTHON": str(env_dump_python),
        "KF_WD_SUDO": str(sudo_stub),
        "HOME": str(home),
    }
    env.pop("KF_WD_DIR", None)
    try:
        completed = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        assert "armed pid" in completed.stdout, completed.stdout

        child_env_file = wd_dir / "child-env.txt"
        deadline = time.time() + 5
        while time.time() < deadline and not child_env_file.exists():
            time.sleep(0.1)
        assert child_env_file.exists(), "watchdog never wrote its env snapshot"
        child_env = child_env_file.read_text()
        assert f"KF_WD_DIR={wd_dir}\n" in child_env, child_env
    finally:
        _kill(wd_dir)


_PRELUDE_TOOLS = (
    "bash",
    "sh",
    "mkdir",
    "mv",
    "cat",
    "pgrep",
    "head",
    "nohup",
    "sleep",
    "env",
    "dirname",
)


def test_arming_spawns_when_setsid_is_absent_from_path(
    tmp_path: Path, stub_python: Path, sudo_stub: Path
) -> None:
    """The spawn must not depend on ``setsid``, which macOS does not ship.

    A bug this catches: ``setsid nohup ...`` hard-wired into the prelude, so
    every host without util-linux (macOS CI, a dev laptop) reports
    "spawn failed" and leaves only the kernel backstop. Reproduced by running
    the real prelude under a PATH that holds every tool it needs EXCEPT
    ``setsid`` — on Linux, where ``setsid`` is otherwise always found.
    """
    bin_dir = tmp_path / "bin-without-setsid"
    bin_dir.mkdir()
    for tool in _PRELUDE_TOOLS:
        real = shutil.which(tool)
        assert real is not None, f"{tool} missing on test host"
        (bin_dir / tool).symlink_to(real)
    wd_dir = tmp_path / "wd"
    try:
        out = _arm(
            wd_dir,
            stub_python,
            deadline=2_000_000_000.0,
            now=1_999_999_000.0,
            sudo=sudo_stub,
            env_overrides={"PATH": str(bin_dir)},
        )
        assert "spawn failed" not in out, out
        assert "armed pid" in out, out
        pid = int((wd_dir / "pid").read_text().strip())
        os.kill(pid, 0)  # raises if not alive
    finally:
        _kill(wd_dir)
