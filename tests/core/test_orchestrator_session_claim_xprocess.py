"""B7 T5: cross-process subprocess integration of session-claim lock.

End-to-end lockdown of the race B7 closes. Subprocess A enters
hold_until_first_tick and sleeps ~2.5s (simulating engine.provision);
subprocess B runs the same _probe_session_claim_holder helper used by
act_on_verdict and asserts it sees A's PID while A holds. After A
releases, B's second probe sees the lock free.

Mirrors the Layer U cross-process visibility test shape at
test_ledger_touch::test_touch_visible_across_process_boundary.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path


def test_reaper_defers_while_orchestrator_mid_provision(tmp_path: Path) -> None:
    """End-to-end race: subprocess A enters hold_until_first_tick and
    holds for ~2.5s; subprocess B's _probe_session_claim_holder returns
    A's pid; after A releases, B's second probe returns None.

    Bug catch: if `provision:<id>` were file-lock-per-process-only or
    the probe lifted holder_pid from the wrong sidecar path, B would
    see no holder and run the destroy path.
    """
    store_root = tmp_path / "store"
    store_root.mkdir()
    flag_file = tmp_path / "a_entered_lock.flag"

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

        with hold_until_first_tick(
            store=store,
            instance_id="i-xproc",
            ledger=ledger,
            ttl_s=60.0,
            timeout_s=60.0,
            poll_interval_s=0.05,
        ):
            flag.write_text("entered")
            time.sleep(2.5)
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

    for _ in range(50):
        if flag_file.exists():
            break
        time.sleep(0.05)
    assert flag_file.exists(), "subprocess A never entered hold_until_first_tick"

    b_result = subprocess.run(
        [sys.executable, "-c", b_script],
        capture_output=True,
        text=True,
        check=True,
        timeout=10.0,
    )
    assert "B-probe=" in b_result.stdout
    assert "B-probe=None" not in b_result.stdout, (
        f"B's probe found no holder while A was holding; stdout={b_result.stdout!r}"
    )
    expected_pid_marker = f"B-probe={proc_a.pid}"
    assert expected_pid_marker in b_result.stdout or "B-probe=-1" in b_result.stdout, (
        f"B's probe did not capture A's pid {proc_a.pid}; stdout={b_result.stdout!r}"
    )

    a_stdout, a_stderr = proc_a.communicate(timeout=15.0)
    assert proc_a.returncode == 0, f"A failed: stderr={a_stderr!r}"
    assert "A: released" in a_stdout

    b_result_2 = subprocess.run(
        [sys.executable, "-c", b_script],
        capture_output=True,
        text=True,
        check=True,
        timeout=10.0,
    )
    assert "B-probe=None" in b_result_2.stdout, (
        f"B's second probe should find lock free after A released; "
        f"stdout={b_result_2.stdout!r}"
    )
