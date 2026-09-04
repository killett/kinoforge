"""B7 T5: cross-process subprocess integration of session-claim lock.

End-to-end lockdown of the race B7 closes. Subprocess A enters
hold_until_first_tick and holds; subprocess B runs the same
_probe_session_claim_holder helper used by act_on_verdict and asserts it
sees A's PID while A holds. After A releases, B's second probe sees the
lock free.

**A's hold is bounded by a condition, not a clock** (2026-09-04). The
original shape had A ``time.sleep(2.5)`` and required B to cold-start an
interpreter, import kinoforge and probe inside that window. Under CPU
contention B's startup alone exceeds 2.5s, A releases first, and the test
fails with ``B-probe=None`` — a pure harness artefact that looks exactly
like the production bug this test exists to catch. A now holds until the
test writes a release flag, so the window is as long as B needs and the
race being tested is the only race left in the test.

Mirrors the Layer U cross-process visibility test shape at
test_ledger_touch::test_touch_visible_across_process_boundary.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

#: Wall-clock ceilings, not timing mechanism. Every one of these is a
#: "something is genuinely wrong" bound, deliberately far above what a
#: loaded machine needs, because a ceiling that doubles as the mechanism is
#: what made these tests flaky in the first place.
_FLAG_TIMEOUT_S = 120.0
_SUBPROCESS_TIMEOUT_S = 120.0
#: Cap inside the held-lock subprocess so an abandoned child (test crashed
#: before writing the release flag) cannot wedge a CI run forever.
_HOLD_CAP_S = 300.0


def _wait_for_flag(
    flag: Path, proc: subprocess.Popen[str], *, what: str, timeout_s: float
) -> None:
    """Block until *flag* appears, failing fast if *proc* dies first.

    Args:
        flag: Path the helper process creates once it reaches the state
            under test.
        proc: The helper process, polled each iteration so an early exit
            surfaces its stderr instead of timing out blind.
        what: Description used in the failure message.
        timeout_s: Ceiling before giving up.

    Raises:
        AssertionError: If *proc* exits before creating *flag*, or the flag
            does not appear within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if flag.exists():
            return
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(
                f"{what}: helper process exited early with rc={proc.returncode}; "
                f"stdout={out!r} stderr={err!r}"
            )
        time.sleep(0.02)
    raise AssertionError(f"{what}: {flag} did not appear within {timeout_s}s")


def test_reaper_defers_while_orchestrator_mid_provision(tmp_path: Path) -> None:
    """End-to-end race: subprocess A enters hold_until_first_tick and holds
    until released; subprocess B's _probe_session_claim_holder returns A's
    pid; after A releases, B's second probe returns None.

    Bug catch: if `provision:<id>` were file-lock-per-process-only or
    the probe lifted holder_pid from the wrong sidecar path, B would
    see no holder and run the destroy path.
    """
    store_root = tmp_path / "store"
    store_root.mkdir()
    flag_file = tmp_path / "a_entered_lock.flag"
    release_file = tmp_path / "a_may_release.flag"

    a_script = textwrap.dedent(f"""
        import time
        from pathlib import Path

        from kinoforge.core.interfaces import Instance
        from kinoforge.core.lifecycle import Ledger
        from kinoforge.core.session_claim import hold_until_first_tick
        from kinoforge.stores.local import LocalArtifactStore

        store = LocalArtifactStore(Path({str(store_root)!r}))
        ledger = Ledger(store=store)
        ledger.record(Instance(
            id="i-xproc",
            provider="local",
            status="ready",
            created_at=time.time() - 7200.0,
            cost_rate_usd_per_hr=0.01,
            tags={{}},
        ))

        flag = Path({str(flag_file)!r})
        release = Path({str(release_file)!r})

        with hold_until_first_tick(
            store=store,
            instance_id="i-xproc",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=60.0,
            poll_interval_s=0.05,
        ):
            flag.write_text("entered")
            # Hold until the test says B is done probing. The cap only
            # exists so an abandoned child cannot wedge a CI run.
            cap = time.monotonic() + {_HOLD_CAP_S!r}
            while not release.exists() and time.monotonic() < cap:
                time.sleep(0.02)
            ledger.touch("i-xproc", heartbeat_thread_tick=time.time())
        print("A: released", flush=True)
    """)

    b_script = textwrap.dedent(f"""
        from pathlib import Path
        from kinoforge.core.reaper_actor import _probe_session_claim_holder
        from kinoforge.stores.local import LocalArtifactStore

        store = LocalArtifactStore(Path({str(store_root)!r}))
        result = _probe_session_claim_holder(store, "i-xproc")
        print(f"B-probe={{result}}", flush=True)
    """)

    proc_a = subprocess.Popen(
        [sys.executable, "-c", a_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_for_flag(
            flag_file,
            proc_a,
            what="subprocess A never entered hold_until_first_tick",
            timeout_s=_FLAG_TIMEOUT_S,
        )

        b_result = subprocess.run(
            [sys.executable, "-c", b_script],
            capture_output=True,
            text=True,
            check=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        assert "B-probe=" in b_result.stdout
        assert "B-probe=None" not in b_result.stdout, (
            f"B's probe found no holder while A was holding; stdout={b_result.stdout!r}"
        )
        expected_pid_marker = f"B-probe={proc_a.pid}"
        assert (
            expected_pid_marker in b_result.stdout or "B-probe=-1" in b_result.stdout
        ), f"B's probe did not capture A's pid {proc_a.pid}; stdout={b_result.stdout!r}"
    finally:
        # Release A whatever happened above, so a failed assertion never
        # leaves the child holding the lock for the full cap.
        release_file.write_text("release")

    a_stdout, a_stderr = proc_a.communicate(timeout=_SUBPROCESS_TIMEOUT_S)
    assert proc_a.returncode == 0, f"A failed: stderr={a_stderr!r}"
    assert "A: released" in a_stdout

    b_result_2 = subprocess.run(
        [sys.executable, "-c", b_script],
        capture_output=True,
        text=True,
        check=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    assert "B-probe=None" in b_result_2.stdout, (
        f"B's second probe should find lock free after A released; "
        f"stdout={b_result_2.stdout!r}"
    )
