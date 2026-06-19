"""End-to-end smoke for the post-session thread-dump diagnostic.

Spawns pytest as a subprocess on a temp test module that constructs a
non-daemon thread and exits, then asserts the diagnostic produced both
the banner and a daemon=False line in stderr.

This is the only test that exercises the actual pytest hook dispatch
+ faulthandler.cancel_dump_traceback_later() call. The unit tests in
tests/test_post_session_dump.py cover the formatter in isolation.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_session_dump_fires_on_leaked_non_daemon_thread(tmp_path: Path) -> None:
    """A leaked non-daemon thread surfaces in the subprocess stderr.

    Catches: hook registration regression, conftest.py syntax error,
    faulthandler-timer cancellation bug (would manifest as a SECOND
    'Timeout (0:00:15)!' dump appearing in stderr on a healthy run),
    fast-path / leak-path branch swap.
    """
    # The temp module starts a non-daemon thread that waits on a
    # threading.Event the test never sets, then pytest's "session" ends
    # with that thread still alive. The diagnostic must flag it.
    test_module = tmp_path / "test_leaky.py"
    test_module.write_text(
        textwrap.dedent(
            """
            import threading

            _stop = threading.Event()


            def test_starts_a_non_daemon_thread() -> None:
                t = threading.Thread(
                    target=_stop.wait,
                    name="e2e_leaker",
                    daemon=False,
                )
                t.start()
                # Intentionally do NOT join. The diagnostic must catch it.
                assert t.is_alive()
            """
        ),
        encoding="utf-8",
    )

    # Copy the project's tests/conftest.py + tests/_thread_dump_helper.py
    # into a sibling `tests/` directory under tmp_path so the subprocess
    # picks up the same diagnostic. Using `rootdir=tmp_path` via -c
    # avoids interference with the outer pytest invocation.
    project_root = Path(__file__).resolve().parents[1]
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    for name in ("conftest.py", "_thread_dump_helper.py"):
        (tests_dir / name).write_text(
            (project_root / "tests" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    # Use the leaky test path scoped under the temp tests/ dir so the
    # in-tmp conftest applies. We could also write the leaky test
    # directly to tests/, but keeping it at tmp_path/test_leaky.py
    # means the subprocess collects exactly one test.
    (tests_dir / "test_leaky.py").write_text(
        test_module.read_text(encoding="utf-8"), encoding="utf-8"
    )

    # The copied conftest imports `kinoforge` + the `tests` package; the
    # subprocess cwd is tmp_path, so neither the project's
    # pyproject.toml `pythonpath = ["src"]` config nor the project root
    # are visible by default. Inject both via PYTHONPATH so the imports
    # resolve identically to the outer pytest invocation.
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(tmp_path), str(project_root / "src"), str(project_root)]
        ),
    }

    # The leaked non-daemon thread blocks the subprocess's
    # threading._shutdown() indefinitely — that is the bug this
    # diagnostic exists to surface. So we EXPECT the subprocess to hang
    # past summary; the dump appears in stderr just before it stalls.
    # Use Popen + communicate(timeout=...) so the TimeoutExpired path
    # still gives us the captured streams, then kill the runaway.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tests_dir / "test_leaky.py"),
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

    def _as_str(payload: str | bytes | None) -> str:
        """subprocess.TimeoutExpired carries raw bytes regardless of text=True."""
        if payload is None:
            return ""
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return payload

    try:
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        # Drain whatever was captured before the kill; communicate
        # returns the buffers populated by the TimeoutExpired path.
        post_stdout, post_stderr = proc.communicate()
        # Prefer the exception's pre-kill buffers when they have content
        # (avoids losing the dump if the post-kill drain races).
        stdout = _as_str(exc.stdout) or _as_str(post_stdout)
        stderr = _as_str(exc.stderr) or _as_str(post_stderr)

    combined = _as_str(stdout) + _as_str(stderr)
    assert "=== POST-SESSION THREAD DUMP ===" in combined, (
        f"diagnostic banner missing\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "daemon=False" in combined, (
        f"non-daemon thread not surfaced\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "e2e_leaker" in combined, (
        f"leaker thread name missing\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
