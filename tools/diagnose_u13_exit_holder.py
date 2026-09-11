"""U13 diagnostic — name what holds the interpreter open after ``UpscaleFailed``.

U13's symptom: after the traceback prints, ``kinoforge upscale`` never
terminates. The traceback printing FIRST is the load-bearing detail — it means
the exception already reached the top of ``main``, so the holder is not in any
``finally`` block. What runs after that is interpreter shutdown, and only two
things there can block forever:

  1. ``threading._shutdown`` joins every non-daemon thread; and
  2. ``atexit`` handlers, which may join threads of their own — including
     daemon ones, because a daemon flag exempts a thread from (1) and from
     nothing else.

Each probe below is a separate subcommand so a run names exactly one
mechanism. Every probe arms ``faulthandler.dump_traceback_later(..., exit=True)``
so a hang self-reports with the holder's own stack instead of needing a
``kill -9`` and a guess.

Run:
    pixi run python tools/diagnose_u13_exit_holder.py pool-worker
    pixi run -e live-modal python tools/diagnose_u13_exit_holder.py modal-client
    pixi run -e live-modal python tools/diagnose_u13_exit_holder.py modal-deploy

``modal-deploy`` is the only probe that touches the network. It deploys a
CPU-only app with no function invocation, so no container runs and it books no
compute; stop it afterwards with
``modal app stop kinoforge-u13-exit-probe --yes``.
"""

from __future__ import annotations

import argparse
import faulthandler
import sys
import threading
import time


def _report_threads(label: str) -> None:
    """Print every live thread with its daemon flag.

    Args:
        label: Printed ahead of the listing so successive dumps are
            distinguishable in one log.
    """
    print(f"--- threads ({label}) ---")
    for t in threading.enumerate():
        print(f"  name={t.name!r} daemon={t.daemon} alive={t.is_alive()}")


def probe_pool_worker() -> None:
    """Park a DAEMON pool worker mid-item, then exit without shutting down.

    ``_DaemonThreadPoolExecutor``'s docstring claims that on an ungraceful
    exit "the workers now die with the process instead of blocking pytest's
    interpreter shutdown". That holds only while a worker is idle. Its
    ``_adjust_thread_count`` registers each worker in
    ``concurrent.futures.thread._threads_queues``, and ``_python_exit`` —
    installed via ``threading._register_atexit`` — puts a sentinel on every
    registered queue and then **joins every one of those threads**, daemon or
    not. A worker blocked inside its current work item never reaches the
    sentinel, so the join never returns.
    """
    from kinoforge.core.pool import _DaemonThreadPoolExecutor

    never_set = threading.Event()
    executor = _DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="u13probe")
    executor.submit(never_set.wait)
    time.sleep(0.3)
    _report_threads("after submit, before exit")
    print("exiting interpreter with the worker still mid-item, no shutdown() call")


def probe_modal_client() -> None:
    """Build a real Modal client read-only, then exit under an exception.

    Two things are under test at once, because they are cheap together: which
    threads the SDK leaves behind, and whether an UNCAUGHT exception changes
    the picture. It might: a traceback keeps every frame it names alive past
    ``main``, so a client that would otherwise have been collected survives
    into ``atexit`` — the one asymmetry that could make U13 failure-path-only.

    ``synchronicity`` (modal's async bridge) registers
    ``atexit -> _close_loop -> self._thread.join()`` with no timeout, so a
    loop that cannot stop blocks exit exactly like the pool worker above.

    Raises:
        UpscaleFailed: Always — deliberately uncaught, so the interpreter
            exits the way it does under U13.
    """
    from kinoforge.core.dotenv_loader import load_env_file

    load_env_file()

    import modal

    from kinoforge.core.errors import UpscaleFailed

    try:
        modal.App.lookup("kinoforge-u13-probe-does-not-exist", create_if_missing=False)
    except Exception as exc:  # noqa: BLE001 — a NotFound is the expected path
        print(f"lookup raised {type(exc).__name__} (expected)")
    _report_threads("after read-only client use")
    raise UpscaleFailed("job-u13-probe", "injected: server said no")


def probe_modal_deploy() -> None:
    """Deploy for real, then exit under an exception — the last live piece.

    ``kinoforge upscale`` on Modal calls ``app.deploy()`` in-process through
    ``default_deploy`` (inside ``modal.enable_output()``), and that is the only
    in-process SDK work on the path: the upscaler engines start no threads, and
    ``_pod_http`` is plain ``urllib`` with timeouts. A read-only lookup does
    not exercise the deploy machinery, so this probe does.

    Books no compute: the deployed function is CPU-only and never invoked, so
    no container starts. Stop it afterwards with
    ``modal app stop kinoforge-u13-exit-probe --yes``.

    Raises:
        UpscaleFailed: Always — deliberately uncaught, as above.
    """
    from kinoforge.core.dotenv_loader import load_env_file

    load_env_file()

    import modal

    from kinoforge.core.errors import UpscaleFailed

    app = modal.App("kinoforge-u13-exit-probe")

    @app.function(image=modal.Image.debian_slim(), serialized=True)  # type: ignore[untyped-decorator]
    def _noop() -> str:
        return "never invoked"

    t0 = time.time()
    with modal.enable_output():
        app.deploy()
    print(f"app.deploy() returned after {time.time() - t0:.2f}s")
    _report_threads("after real app.deploy()")
    raise UpscaleFailed("job-u13-probe", "injected: server said no")


_PROBES = {
    "pool-worker": probe_pool_worker,
    "modal-client": probe_modal_client,
    "modal-deploy": probe_modal_deploy,
}


def main(argv: list[str] | None = None) -> int:
    """Run one probe under a self-reporting hang timeout.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code. A probe that raises on purpose exits through its
        own traceback rather than through this return.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probe", choices=sorted(_PROBES))
    parser.add_argument(
        "--hang-timeout-s",
        type=float,
        default=30.0,
        help="dump every thread stack and abort if exit takes this long",
    )
    args = parser.parse_args(argv)

    faulthandler.dump_traceback_later(args.hang_timeout_s, exit=True)
    _PROBES[args.probe]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
