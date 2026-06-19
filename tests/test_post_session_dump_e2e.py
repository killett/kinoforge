"""End-to-end smoke for the post-session thread-dump diagnostic.

Spawns pytest as a subprocess on a temp test module that constructs a
non-daemon thread and exits, then asserts the diagnostic produced both
the banner and a daemon=False line in stderr.

This is the only test that exercises the actual pytest hook dispatch
+ faulthandler.cancel_dump_traceback_later() call. The unit tests in
tests/test_post_session_dump.py cover the formatter in isolation.
"""

from __future__ import annotations

import textwrap
from pathlib import Path


def test_session_dump_fires_on_leaked_non_daemon_thread(tmp_path: Path) -> None:
    """A leaked non-daemon thread surfaces in the subprocess stderr.

    Catches: hook registration regression, conftest.py syntax error,
    faulthandler-timer cancellation bug (would manifest as a SECOND
    'Timeout (0:00:15)!' dump appearing in stderr on a healthy run),
    fast-path / leak-path branch swap.
    """
    from tests._subprocess_pytest_helper import _run_subprocess_pytest  # noqa: PLC0415

    test_body = textwrap.dedent(
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
    )
    stdout, stderr, _rc = _run_subprocess_pytest(tmp_path, test_body)

    combined = stdout + stderr
    assert "=== POST-SESSION THREAD DUMP ===" in combined, (
        f"diagnostic banner missing\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "daemon=False" in combined, (
        f"non-daemon thread not surfaced\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "e2e_leaker" in combined, (
        f"leaker thread name missing\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
