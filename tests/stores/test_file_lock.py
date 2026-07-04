"""FileLock unit + subprocess integration tests.

Unit path uses a spy `flock_fn` so the OS lock is never taken — keeps
the suite hermetic and reproducible on any POSIX runner.  One subprocess
integration test exercises the real fcntl.flock for cross-process
correctness.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import LockTimeout
from kinoforge.stores.local import LocalArtifactStore
from kinoforge.stores.local_lock import FileLock

# ---------------------------------------------------------------------------
# Helpers — flock_fn spies
# ---------------------------------------------------------------------------


class _AlwaysSucceedFlock:
    """flock_fn spy that records calls and never blocks."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, fd: int, flags: int) -> None:
        self.calls.append((fd, flags))


class _AlwaysBlockFlock:
    """flock_fn spy that always raises BlockingIOError for LOCK_NB."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, fd: int, flags: int) -> None:
        import fcntl as _fcntl

        self.calls.append((fd, flags))
        if flags & _fcntl.LOCK_NB:
            raise BlockingIOError("would block")


# ---------------------------------------------------------------------------
# Path layout
# ---------------------------------------------------------------------------


def test_lock_file_path_under_locks_directory(tmp_path: Path) -> None:
    """Lock files must live under `<root>/_locks/`, isolated from artifacts.

    Fails if the path layout collides with the `_profiles` or `_lifecycle`
    namespaces used by JsonProfileCache and Ledger.
    """
    lock = FileLock(
        path=tmp_path / "_locks" / "profiles__abc.lock",
        key="profiles/abc",
        ttl_s=10.0,
        clock=FakeClock(start=0.0),
        flock_fn=_AlwaysSucceedFlock(),
        sleep=lambda _: None,
    )
    token = lock.acquire(blocking=False)
    assert token is not None
    sidecar = tmp_path / "_locks" / "profiles__abc.lock"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text())
    assert payload["nonce"] == token.nonce
    assert payload["holder_pid"] == os.getpid()
    # expires_at = clock.now() (0.0) + ttl_s (10.0)
    assert payload["expires_at"] == 10.0
    lock.release(token)


def test_context_manager_acquires_and_releases(tmp_path: Path) -> None:
    """`with FileLock(...) as token:` must acquire on enter and release on exit.

    Fails if __enter__ forgets to return the token or __exit__ leaves the
    sidecar behind.
    """
    sidecar = tmp_path / "_locks" / "k.lock"
    spy = _AlwaysSucceedFlock()
    with FileLock(
        path=sidecar,
        key="k",
        ttl_s=5.0,
        clock=FakeClock(start=0.0),
        flock_fn=spy,
        sleep=lambda _: None,
    ) as token:
        assert token.key == "k"
        assert sidecar.exists()
    # Sidecar must PERSIST after exit (unlinking it re-introduces the
    # split-brain inode race — see test_release_does_not_unlink_sidecar);
    # only the OS lock state is released.
    assert sidecar.exists()
    # Spy must have observed an UN unlock call (matches LOCK_UN flag).
    import fcntl as _fcntl

    assert any(flags & _fcntl.LOCK_UN for _fd, flags in spy.calls), (
        "release must call flock with LOCK_UN"
    )


def test_local_store_acquire_lock_returns_file_lock(tmp_path: Path) -> None:
    """LocalArtifactStore.acquire_lock must wire keys to `<root>/_locks/`."""
    store = LocalArtifactStore(tmp_path)
    lock = store.acquire_lock("profiles/abc", ttl_s=5.0)
    assert isinstance(lock, FileLock)


# ---------------------------------------------------------------------------
# Acquire / release semantics with spy flock
# ---------------------------------------------------------------------------


def test_nonblocking_acquire_when_flock_blocks_returns_none(tmp_path: Path) -> None:
    """When fcntl raises BlockingIOError, non-blocking acquire returns None."""
    spy = _AlwaysBlockFlock()
    lock = FileLock(
        path=tmp_path / "_locks" / "k.lock",
        key="k",
        ttl_s=5.0,
        clock=FakeClock(start=0.0),
        flock_fn=spy,
        sleep=lambda _: None,
    )
    assert lock.acquire(blocking=False) is None


def test_blocking_with_timeout_raises_lock_timeout(tmp_path: Path) -> None:
    """Eventual LockTimeout must wrap a flock that never succeeds."""
    spy = _AlwaysBlockFlock()
    clock = FakeClock(start=0.0)
    lock = FileLock(
        path=tmp_path / "_locks" / "k.lock",
        key="k",
        ttl_s=5.0,
        clock=clock,
        flock_fn=spy,
        sleep=lambda _: clock.advance(0.6),
        poll_interval_s=0.5,
    )
    with pytest.raises(LockTimeout):
        lock.acquire(blocking=True, timeout_s=1.0)


def test_release_keeps_sidecar_but_empties_payload(tmp_path: Path) -> None:
    """release() keeps the sidecar (inode stability) and truncates the payload.

    Bug caught: unlinking on release orphans the inode under any waiter's
    already-open fd — two holders on two inodes, lost updates (CI run
    28700621336). The stale-payload truncate keeps diagnostics honest:
    an empty file means "not held", a JSON payload means "held".
    """
    sidecar = tmp_path / "_locks" / "k.lock"
    lock = FileLock(
        path=sidecar,
        key="k",
        ttl_s=5.0,
        clock=FakeClock(start=0.0),
        flock_fn=_AlwaysSucceedFlock(),
        sleep=lambda _: None,
    )
    token = lock.acquire()
    assert token is not None
    assert sidecar.exists() and sidecar.stat().st_size > 0
    lock.release(token)
    assert sidecar.exists(), "sidecar must persist across release"
    assert sidecar.stat().st_size == 0, "payload must truncate on release"


# ---------------------------------------------------------------------------
# Subprocess integration — real fcntl.flock
# ---------------------------------------------------------------------------


def test_real_fcntl_blocks_cross_process(tmp_path: Path) -> None:
    """Two processes contending for the same FileLock — second must see contention.

    Spawns a child that takes the lock and holds it via sleep(); the parent
    then attempts a non-blocking acquire and asserts it returns None.
    """
    holder_script = textwrap.dedent(
        f"""
        import sys, time, fcntl, json
        from pathlib import Path
        from kinoforge.core.clock import RealClock
        from kinoforge.stores.local import LocalArtifactStore

        store = LocalArtifactStore(Path({str(tmp_path)!r}))
        lock = store.acquire_lock("k", ttl_s=30.0)
        token = lock.acquire()
        print("HELD", flush=True)
        time.sleep(3.0)
        lock.release(token)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for child to print HELD so we know it has taken the lock.
        # Use select with a 10s wall-clock budget so a crashed child fails
        # the test fast instead of hanging the suite.
        import select

        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], 10.0)
        if not ready:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"child did not signal HELD within 10s: stderr={stderr!r}"
            )
        line = proc.stdout.readline().strip()
        assert line == "HELD", (
            f"child did not take lock: stderr={proc.stderr.read() if proc.stderr else ''}"
        )

        store = LocalArtifactStore(tmp_path)
        contender = store.acquire_lock("k", ttl_s=30.0)
        token = contender.acquire(blocking=False)
        assert token is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_release_does_not_unlink_sidecar_no_split_brain(tmp_path: Path) -> None:
    """Two holders must never coexist across a release/re-acquire cycle.

    Bug caught (CI run 28700621336: ephemeral-index lost an add under
    contention): ``release()`` unlinked the sidecar, so a waiter still
    holding a pre-unlink fd (inode A) and a fresh acquirer creating the
    path anew (inode B) both took "the" lock on DIFFERENT inodes —
    split-brain, lost update. Release must leave the sidecar in place
    so every open() forever resolves to the same inode.
    """
    import fcntl
    import os

    from kinoforge.stores.local_lock import FileLock

    path = tmp_path / "k.lock"

    lock_a = FileLock(path=path, key="k", ttl_s=5.0)
    token_a = lock_a.acquire()
    assert token_a is not None

    # Simulate a waiter that opened the sidecar BEFORE release. With the
    # unlink in place this fd points at an inode that becomes orphaned.
    waiter_fd = os.open(str(path), os.O_RDWR)
    try:
        lock_a.release(token_a)
        assert path.exists(), "release must not unlink the sidecar"

        lock_b = FileLock(path=path, key="k", ttl_s=5.0)
        token_b = lock_b.acquire()
        assert token_b is not None

        # The stale-fd waiter must STILL be excluded — same inode as B.
        with pytest.raises(BlockingIOError):
            fcntl.flock(waiter_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_b.release(token_b)
    finally:
        os.close(waiter_fd)
