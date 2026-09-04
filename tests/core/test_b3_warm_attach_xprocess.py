"""B3 Task g — cross-process subprocess tests for warm-attach primitives.

Mandatory test surface per spec §1.1 risk frame. Mirrors B7's xprocess
shape at ``test_orchestrator_session_claim_xprocess.py``: inline-Python
subprocesses exercise the cross-process correctness of B3 primitives
(``_probe_lock_held``, ``is_session_busy``, ``_scan_warm_candidates``,
ledger session_start/end persistence) without requiring a full kinoforge
CLI invocation against a never-implemented FakeProvider.

Each test stages a small Python script per subprocess, captures
stdout/stderr, and asserts the behaviour observable across the process
boundary.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

#: Wall-clock ceilings, not timing mechanism. Each is a "something is
#: genuinely wrong" bound set far above what a loaded machine needs — see
#: the module docstring of test_orchestrator_session_claim_xprocess.py for
#: why a ceiling must never double as the mechanism.
_FLAG_TIMEOUT_S = 120.0
_SUBPROCESS_TIMEOUT_S = 120.0
#: Cap inside a lock-holding subprocess so an abandoned child (test crashed
#: before writing the release flag) cannot wedge a CI run forever.
_HOLD_CAP_S = 300.0


def _run_python(
    script: str, *, timeout: float = _SUBPROCESS_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _hold_until_released(release: Path, *, indent: int = 12) -> str:
    """Return the inline-Python snippet that blocks until *release* exists.

    Replaces a bare ``time.sleep(N)`` inside a lock-holding subprocess. The
    sleep form required the probing process to cold-start an interpreter,
    import kinoforge and finish its work inside N seconds; under CPU
    contention that startup alone overruns N, the lock is released early,
    and the probe reports "no holder" — indistinguishable from the
    production bug the test exists to catch.

    Args:
        release: Flag path the test creates once the probe is done.
        indent: Column the snippet is interpolated at. The first line is
            left bare (the template supplies its indent); every later line
            carries *indent* spaces, so ``textwrap.dedent`` on the enclosing
            template still sees the template's own lines as the common
            prefix and does not flatten the block into a syntax error.

    Returns:
        Python source ready to interpolate inside a ``with`` block.
    """
    pad = " " * indent
    lines = [
        f"release = Path({str(release)!r})",
        f"cap = time.monotonic() + {_HOLD_CAP_S!r}",
        "while not release.exists() and time.monotonic() < cap:",
        "    time.sleep(0.02)",
    ]
    return f"\n{pad}".join(lines)


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


# ---------------------------------------------------------------------------
# 1. _probe_lock_held sees a hold from another process
# ---------------------------------------------------------------------------


def test_probe_lock_held_observes_cross_process_hold(tmp_path: Path) -> None:
    """Bug: if FileLock were per-process only, B3's reaper-held skip would
    silently never fire when B1 sweeper holds the lock from another PID."""
    store_root = tmp_path / "store"
    store_root.mkdir()
    flag = tmp_path / "a_entered.flag"
    done = tmp_path / "a_done.flag"
    release = tmp_path / "a_may_release.flag"

    a_script = textwrap.dedent(f"""
        from pathlib import Path
        import time
        from kinoforge.stores.local import LocalArtifactStore
        store = LocalArtifactStore(Path({str(store_root)!r}))
        with store.acquire_lock("reaper/pod-1", ttl_s=30.0):
            Path({str(flag)!r}).write_text("entered")
            {_hold_until_released(release)}
        Path({str(done)!r}).write_text("done")
    """)

    p_a = subprocess.Popen(
        [sys.executable, "-c", a_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_flag(
            flag,
            p_a,
            what="A never grabbed the reaper lock",
            timeout_s=_FLAG_TIMEOUT_S,
        )

        b_script = textwrap.dedent(f"""
            from pathlib import Path
            from kinoforge.cli._commands import _probe_lock_held
            from kinoforge.stores.local import LocalArtifactStore
            store = LocalArtifactStore(Path({str(store_root)!r}))
            print("HELD=" + str(_probe_lock_held(store, "reaper/pod-1")))
        """)
        r_b = _run_python(b_script)
        assert "HELD=True" in r_b.stdout, (
            f"probe did not see A's hold; B stdout={r_b.stdout!r} stderr={r_b.stderr!r}"
        )
    finally:
        release.write_text("release")
        p_a.wait(timeout=_SUBPROCESS_TIMEOUT_S)


# ---------------------------------------------------------------------------
# 2. session_start / session_end written by A visible to B's is_session_busy
# ---------------------------------------------------------------------------


def test_session_start_visible_across_process_boundary(tmp_path: Path) -> None:
    """Bug: ledger writes not flushed cross-process would leave the busy
    marker invisible to a concurrent CLI's scan."""
    store_root = tmp_path / "store"
    store_root.mkdir()

    a_script = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from kinoforge.core.interfaces import Instance
        from kinoforge.core.lifecycle import Ledger
        from kinoforge.stores.local import LocalArtifactStore
        store = LocalArtifactStore(Path({str(store_root)!r}))
        ledger = Ledger(store=store)
        ledger.record(Instance(
            id="pod-1", provider="local", status="ready",
            created_at=time.time(), cost_rate_usd_per_hr=0.0,
            tags={{"kinoforge_key": "abc123abc123"}},
        ))
        now = time.time()
        ledger.touch("pod-1", session_start=now, heartbeat_thread_tick=now)
    """)
    r_a = _run_python(a_script)
    assert r_a.returncode == 0, r_a.stderr

    b_script = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from kinoforge.core.lifecycle import Ledger, is_session_busy
        from kinoforge.stores.local import LocalArtifactStore
        store = LocalArtifactStore(Path({str(store_root)!r}))
        entry = Ledger(store=store).read("pod-1")
        print("BUSY=" + str(is_session_busy(
            entry, now=time.time(), heartbeat_interval_s=30.0,
        )))
    """)
    r_b = _run_python(b_script)
    assert "BUSY=True" in r_b.stdout, (
        f"B did not see A's session_start; B stdout={r_b.stdout!r} "
        f"stderr={r_b.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 3. Stale-busy clears across processes via HB freshness gate
# ---------------------------------------------------------------------------


def test_stale_session_busy_clears_after_heartbeat_window(tmp_path: Path) -> None:
    """Bug: KILL -9 stale-busy not clearing across processes would forever
    block warm-reuse for the entry."""
    store_root = tmp_path / "store"
    store_root.mkdir()

    # Seed an entry from A with session_start = 100s ago AND a tick = 100s ago
    # (intentionally stale — simulates KILL -9 mid-session).
    a_script = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from kinoforge.core.interfaces import Instance
        from kinoforge.core.lifecycle import Ledger
        from kinoforge.stores.local import LocalArtifactStore
        store = LocalArtifactStore(Path({str(store_root)!r}))
        ledger = Ledger(store=store)
        ledger.record(Instance(
            id="pod-1", provider="local", status="ready",
            created_at=time.time() - 200.0, cost_rate_usd_per_hr=0.0,
            tags={{"kinoforge_key": "abc123abc123"}},
        ))
        # session_start more recent than session_end (no session_end) AND a
        # tick that is 200s old → stale per 3 * heartbeat_interval=30s window.
        stale = time.time() - 200.0
        ledger.touch("pod-1", session_start=stale, heartbeat_thread_tick=stale)
    """)
    r_a = _run_python(a_script)
    assert r_a.returncode == 0, r_a.stderr

    b_script = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from kinoforge.core.lifecycle import Ledger, is_session_busy
        from kinoforge.stores.local import LocalArtifactStore
        store = LocalArtifactStore(Path({str(store_root)!r}))
        entry = Ledger(store=store).read("pod-1")
        # heartbeat_interval=30s → window=90s; tick is 200s old → stale.
        print("BUSY=" + str(is_session_busy(
            entry, now=time.time(), heartbeat_interval_s=30.0,
        )))
    """)
    r_b = _run_python(b_script)
    assert "BUSY=False" in r_b.stdout, (
        f"B saw stale entry as busy; B stdout={r_b.stdout!r} stderr={r_b.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 4. session_end written by A clears busy state for B
# ---------------------------------------------------------------------------


def test_session_end_clears_busy_across_process_boundary(tmp_path: Path) -> None:
    """Bug: session_end not visible cross-process would leave entries
    permanently busy after a clean exit."""
    store_root = tmp_path / "store"
    store_root.mkdir()

    a_script = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from kinoforge.core.interfaces import Instance
        from kinoforge.core.lifecycle import Ledger
        from kinoforge.stores.local import LocalArtifactStore
        store = LocalArtifactStore(Path({str(store_root)!r}))
        ledger = Ledger(store=store)
        ledger.record(Instance(
            id="pod-1", provider="local", status="ready",
            created_at=time.time(), cost_rate_usd_per_hr=0.0,
            tags={{"kinoforge_key": "abc123abc123"}},
        ))
        now = time.time()
        ledger.touch("pod-1", session_start=now, heartbeat_thread_tick=now)
        # Clean exit:
        ledger.touch("pod-1", session_end=time.time())
    """)
    r_a = _run_python(a_script)
    assert r_a.returncode == 0, r_a.stderr

    b_script = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from kinoforge.core.lifecycle import Ledger, is_session_busy
        from kinoforge.stores.local import LocalArtifactStore
        store = LocalArtifactStore(Path({str(store_root)!r}))
        entry = Ledger(store=store).read("pod-1")
        print("BUSY=" + str(is_session_busy(
            entry, now=time.time(), heartbeat_interval_s=30.0,
        )))
    """)
    r_b = _run_python(b_script)
    assert "BUSY=False" in r_b.stdout, (
        f"B saw cleanly-closed entry as busy; B stdout={r_b.stdout!r} "
        f"stderr={r_b.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 5. _scan_warm_candidates skips reaper-held entry from another process
# ---------------------------------------------------------------------------


def test_scan_records_reaper_held_skip_for_cross_process_lock(
    tmp_path: Path,
) -> None:
    """Bug: scan attaching to a pod another process is mid-destroying would
    HTTP-fail at first submit."""
    store_root = tmp_path / "store"
    store_root.mkdir()
    flag = tmp_path / "a_entered.flag"
    release = tmp_path / "a_may_release.flag"

    # Seed entry with matching provider + cap_key + fresh tick (so it passes
    # coarse + classify gates) before A grabs the reaper lock.
    seed = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from kinoforge.core.interfaces import Instance
        from kinoforge.core.lifecycle import Ledger
        from kinoforge.stores.local import LocalArtifactStore
        store = LocalArtifactStore(Path({str(store_root)!r}))
        ledger = Ledger(store=store)
        ledger.record(Instance(
            id="pod-1", provider="local", status="ready",
            created_at=time.time(), cost_rate_usd_per_hr=0.0,
            tags={{"kinoforge_key": "abc123abc123"}},
        ))
        now = time.time()
        ledger.touch("pod-1", heartbeat_thread_tick=now)
    """)
    r_seed = _run_python(seed)
    assert r_seed.returncode == 0, r_seed.stderr

    a_script = textwrap.dedent(f"""
        from pathlib import Path
        import time
        from kinoforge.stores.local import LocalArtifactStore
        store = LocalArtifactStore(Path({str(store_root)!r}))
        with store.acquire_lock("reaper/pod-1", ttl_s=30.0):
            Path({str(flag)!r}).write_text("entered")
            {_hold_until_released(release)}
    """)
    p_a = subprocess.Popen(
        [sys.executable, "-c", a_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_flag(
            flag,
            p_a,
            what="A never grabbed the reaper lock",
            timeout_s=_FLAG_TIMEOUT_S,
        )

        b_script = textwrap.dedent(f"""
            from pathlib import Path
            from typing import Any
            from unittest.mock import MagicMock
            from kinoforge.cli._commands import _scan_warm_candidates
            from kinoforge.cli.context import SessionContext
            from kinoforge.core.lifecycle import Ledger
            from kinoforge.stores.local import LocalArtifactStore

            store = LocalArtifactStore(Path({str(store_root)!r}))
            ledger = Ledger(store=store)

            class _Compute:
                provider = "local"
            class _Cfg:
                compute = _Compute()
                def capability_key(self):
                    class K:
                        def derive(self_):
                            return "abc123abc123XX"
                    return K()
                def lifecycle(self):
                    from kinoforge.core.interfaces import Lifecycle
                    return Lifecycle(heartbeat_interval_s=30.0)

            class _Ctx:
                def __init__(self, store, ledger):
                    self._store = store
                    self._ledger = ledger
                def store(self):
                    return self._store
                def ledger(self):
                    return self._ledger

            ctx = _Ctx(store, ledger)
            instance, report = _scan_warm_candidates(ctx, _Cfg())
            print("ATTACHED=" + str(report.attached))
            print("SKIPPED=" + str(report.skipped))
        """)
        r_b = _run_python(b_script)
        assert "ATTACHED=None" in r_b.stdout
        assert "('pod-1', 'reaper-held')" in r_b.stdout, (
            f"scan did not record reaper-held skip; B stdout={r_b.stdout!r} "
            f"stderr={r_b.stderr!r}"
        )
    finally:
        release.write_text("release")
        p_a.wait(timeout=_SUBPROCESS_TIMEOUT_S)
