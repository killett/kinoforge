"""Behavior: a parked pool worker does not hold the interpreter open (U31).

``_DaemonThreadPoolExecutor`` exists so that an ungraceful exit does not wait
on the pool, and its docstring said so: "on ungraceful exit the workers now die
with the process instead of blocking pytest's interpreter shutdown". That held
only while a worker was IDLE.

``concurrent.futures.thread`` installs ``_python_exit`` via
``threading._register_atexit``, so it runs BEFORE ``threading._shutdown`` and
joins **every thread registered in ``_threads_queues``, daemon or not**. The
daemon flag exempts a thread from ``threading._shutdown`` and from nothing
else. A worker blocked inside its current work item never reaches the sentinel
``_python_exit`` puts on its queue, so that join never returns — the same
symptom U13 describes on ``kinoforge upscale``: the traceback prints, and then
the process simply never terminates.

These tests run in SUBPROCESSES because the claim is about interpreter exit,
which cannot be observed from inside the interpreter making it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Seconds a child gets to exit. Generous: the failure mode is "never", not
# "slow", so a false pass would need the hang to resolve itself.
_EXIT_BUDGET_S = 25.0

_PARK_A_WORKER_THEN_EXIT = """
    import threading, sys
    from kinoforge.core.pool import _DaemonThreadPoolExecutor

    never_set = threading.Event()
    ex = _DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="u31")
    ex.submit(never_set.wait)          # park a worker INSIDE a work item
    while not any("u31" in t.name for t in threading.enumerate()):
        pass                            # ensure the worker actually started
    print("parked", flush=True)
    sys.exit(0)                         # no shutdown() — the ungraceful exit
"""

_CLOSE_WAITS_FOR_AN_IN_FLIGHT_ITEM = """
    import threading, time, sys
    from kinoforge.core.pool import _DaemonThreadPoolExecutor

    finished = []

    def slow():
        time.sleep(1.0)
        finished.append(True)

    ex = _DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="u31")
    ex.submit(slow)
    ex.shutdown(wait=True)
    print("finished" if finished else "ABANDONED", flush=True)
    sys.exit(0)
"""


def _run_child(body: str) -> subprocess.CompletedProcess[str]:
    """Run *body* in a subprocess under a hard timeout.

    Args:
        body: Python source, indented as written in the constants above.

    Returns:
        The completed process.

    Raises:
        pytest.fail.Exception: The child did not exit within the budget.
    """
    try:
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(body)],
            capture_output=True,
            text=True,
            timeout=_EXIT_BUDGET_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"child did not exit within {_EXIT_BUDGET_S}s — the interpreter is "
            f"held open by a pool worker (U31). stdout={exc.stdout!r}"
        )


def test_exit_is_not_blocked_by_a_worker_parked_in_its_work_item() -> None:
    """U31: the daemon flag must actually buy an unblocked exit.

    Bug caught: ``_adjust_thread_count`` registering its daemon workers in
    ``concurrent.futures.thread._threads_queues``, whose ``_python_exit``
    handler joins every registered thread without a timeout. The child below
    parks a worker inside its work item and exits without ``shutdown()`` — the
    ungraceful shape — and hangs forever unless the registration is gone.
    """
    proc = _run_child(_PARK_A_WORKER_THEN_EXIT)

    assert "parked" in proc.stdout, f"worker never started: {proc.stderr}"
    assert proc.returncode == 0


def test_graceful_shutdown_still_waits_for_the_in_flight_item() -> None:
    """The fix must not turn ``close()`` into a kill.

    Bug caught: "solving" the hang by abandoning in-flight work — dropping the
    sentinel, or shutting down with ``wait=False``. A pool item is a
    generation's poll-and-publish, so abandoning one mid-flight discards a
    result the pod has already been paid for. ``shutdown(wait=True)`` joins
    ``self._threads`` directly and must keep doing so.
    """
    proc = _run_child(_CLOSE_WAITS_FOR_AN_IN_FLIGHT_ITEM)

    assert proc.returncode == 0
    assert "finished" in proc.stdout, (
        f"shutdown(wait=True) returned before the work item completed: "
        f"stdout={proc.stdout!r} stderr={proc.stderr}"
    )
