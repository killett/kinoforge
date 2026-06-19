"""Shared subprocess-pytest harness for thread-diagnostic e2e tests.

The post-session dump e2e and the L1 policy e2e both spawn pytest as a
subprocess against a temp test module, with kinoforge's pyproject
``pythonpath = ["src"]`` config bypassed (subprocess cwd is the tmp
dir). They both also need TimeoutExpired-handling because a leaked
non-daemon thread blocks the subprocess past summary.

Single helper here keeps both tests honest and prevents drift.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

_TIMEOUT_DEFAULT: Final[float] = 10.0


def _as_str(payload: str | bytes | None) -> str:
    """subprocess.TimeoutExpired carries raw bytes regardless of text=True."""
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def _run_subprocess_pytest(
    tmp_path: Path,
    test_body: str,
    *,
    test_filename: str = "test_leaky.py",
    timeout: float = _TIMEOUT_DEFAULT,
) -> tuple[str, str, int | None]:
    """Spawn pytest in `tmp_path` against `test_body` written to `test_filename`.

    Copies the project's ``tests/conftest.py`` + ``tests/_thread_dump_helper.py``
    into a sibling ``tests/`` directory under ``tmp_path`` so the
    subprocess picks up the same diagnostic + policy stack. Injects
    ``PYTHONPATH=tmp:project/src:project`` so the conftest's
    ``kinoforge.core.errors`` import resolves.

    Args:
        tmp_path: pytest's per-test tmp dir.
        test_body: Source code of the test module to run.
        test_filename: Filename under ``tmp_path/tests/``.
        timeout: Seconds before the subprocess is killed. Default 10s
            — leaked threads block shutdown indefinitely so we never
            want to wait for natural exit.

    Returns:
        Tuple of (stdout, stderr, returncode). returncode is ``None``
        when the subprocess was killed for timeout.
    """
    project_root = Path(__file__).resolve().parents[1]
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    for name in ("conftest.py", "_thread_dump_helper.py"):
        (tests_dir / name).write_text(
            (project_root / "tests" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (tests_dir / test_filename).write_text(test_body, encoding="utf-8")

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(tmp_path), str(project_root / "src"), str(project_root)]
        ),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tests_dir / test_filename),
            "-v",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    returncode: int | None
    try:
        stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
        stdout, stderr = _as_str(stdout_raw), _as_str(stderr_raw)
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        post_stdout, post_stderr = proc.communicate()
        stdout = _as_str(exc.stdout) or _as_str(post_stdout)
        stderr = _as_str(exc.stderr) or _as_str(post_stderr)
        returncode = None
    return stdout, stderr, returncode
