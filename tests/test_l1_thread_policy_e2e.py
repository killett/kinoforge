"""End-to-end smoke for the L1 thread-leak policy.

Spawns pytest as a subprocess on a temp module containing two tests:
one unmanaged leaker (must show as ERROR via the L1 hook in FAIL mode),
one managed leaker (must show as PASSED because the managed_thread
fixture joins it).

This is the only L1 test that exercises the actual
pytest_runtest_makereport dispatch + fixture finalization ordering.
The 8 unit tests in tests/test_l1_thread_policy.py cover the hook by
direct invocation; this one covers the integration.
"""

from __future__ import annotations

import textwrap
from pathlib import Path


def test_unmanaged_leaker_renders_error_and_managed_renders_pass(
    tmp_path: Path,
) -> None:
    """Subprocess: unmanaged → ERROR (L1 longrepr); managed → PASSED.

    Catches: L1 hook registration drop, fixture-finalization-vs-hook
    ordering regression, longrepr template drift, mode-flip bug.

    The subprocess must run with _L1_MODE='fail' temporarily — we
    write the conftest copy with the constant overridden inline at
    the top of the file, so the outer pytest run remains in WARN.
    """
    from tests._subprocess_pytest_helper import _run_subprocess_pytest  # noqa: PLC0415

    test_body = textwrap.dedent(
        """
        import threading

        # Force the in-tmp conftest's L1 hook into FAIL mode for THIS
        # subprocess only. The outer pytest run is still WARN.
        import tests.conftest as _cf
        _cf._L1_MODE = "fail"

        _stop_unmanaged = threading.Event()
        _stop_managed = threading.Event()


        def test_unmanaged_leaker_fails() -> None:
            t = threading.Thread(
                target=_stop_unmanaged.wait,
                name="unmanaged_e2e_leaker",
                daemon=False,
            )
            t.start()
            assert t.is_alive()


        def test_managed_leaker_passes(managed_thread) -> None:
            t = managed_thread.spawn(
                target=lambda: _stop_managed.wait(timeout=0.5),
                name="managed_e2e_thread",
            )
            assert t.is_alive() or _stop_managed.is_set()
            # The fixture teardown joins t — we don't.
        """
    )
    stdout, stderr, returncode = _run_subprocess_pytest(
        tmp_path, test_body, timeout=15.0
    )
    combined = stdout + stderr

    assert returncode is None or returncode != 0, (
        f"subprocess should fail (exit != 0); got {returncode}\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "test_unmanaged_leaker_fails" in combined and "ERROR" in combined, (
        f"L1 hook did not render ERROR for the unmanaged leaker\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "L1 thread-leak policy" in combined, (
        f"L1 longrepr text missing\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "test_managed_leaker_passes" in combined and "PASSED" in combined, (
        f"managed_thread fixture did not let the test pass\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
