"""Unit tests for the managed_thread fixture and _ManagedThreadRegistrar.

These pin the registrar's contract independent of the pytest hook plumbing
so a refactor that breaks .spawn / .register / _teardown semantics surfaces
locally, not from an opaque L1 false-positive on an unrelated test.
"""

from __future__ import annotations

import threading

# Imported lazily inside each test for symmetry with the production import
# in tests/conftest.py — the registrar is a private symbol of conftest, so
# the import path is documented as a contract.
from tests.conftest import _ManagedThreadRegistrar  # noqa: PLC0415


def test_spawn_constructs_starts_and_registers_thread(
    managed_thread: object,
) -> None:
    """`.spawn(...)` returns a started, registered Thread and joins cleanly.

    Catches: a future refactor that forgets to call `.start()` (silent
    no-op test bodies), or forgets to append to `_threads` (so teardown
    skips join, leaking the thread).
    """
    flag = threading.Event()

    def _set_flag() -> None:
        flag.set()

    t = managed_thread.spawn(target=_set_flag, name="spawn-test")  # type: ignore[attr-defined]
    assert isinstance(t, threading.Thread)
    assert t.is_alive() or flag.is_set(), "spawn must call .start()"
    t.join(timeout=1.0)
    assert flag.is_set(), "target callable must have run"


def test_register_appends_and_returns_thread_for_fluent_chaining() -> None:
    """`.register(t)` returns the same object and stores it for teardown.

    Catches: accidental copy / wrap that breaks `t = .register(Thread(...))`
    chaining, or an off-by-one in the registry that drops the thread.
    """
    registrar = _ManagedThreadRegistrar()
    flag = threading.Event()
    t = threading.Thread(target=flag.set, name="register-test", daemon=False)
    t.start()
    returned = registrar.register(t)
    assert returned is t, "register must return the exact thread for chaining"
    assert registrar._threads == [t], "register must append to the registry"
    # Cleanup so this test does not itself leak.
    t.join(timeout=1.0)
    assert not t.is_alive()


def test_teardown_joins_all_registered_threads_within_timeout() -> None:
    """`_teardown` joins every registered thread; happy path returns empty.

    Catches: a future change that uses `Thread.daemon` to skip joins
    (which would defeat the whole fixture), or that exits the loop early.
    """
    registrar = _ManagedThreadRegistrar()
    n_targets = 3
    flags = [threading.Event() for _ in range(n_targets)]
    for i, flag in enumerate(flags):
        t = threading.Thread(target=flag.set, name=f"teardown-{i}", daemon=False)
        t.start()
        registrar.register(t)
    still_alive = registrar._teardown(join_timeout=1.0)
    assert still_alive == [], f"all threads should have joined; got {still_alive!r}"
    assert all(f.is_set() for f in flags), "every target must have run"


def test_teardown_returns_threads_that_did_not_join_within_timeout() -> None:
    """`_teardown` returns any stuck thread without raising.

    Catches: a future change that raises eagerly inside `_teardown`
    (which would skip joining the rest of the registry and leak threads
    into the next test), or that swallows the stuck-thread signal.
    """
    registrar = _ManagedThreadRegistrar()
    stop = threading.Event()  # intentionally never set inside the test
    stuck = threading.Thread(target=stop.wait, name="stuck", daemon=False)
    stuck.start()
    try:
        registrar.register(stuck)
        still_alive = registrar._teardown(join_timeout=0.05)
        assert still_alive == [stuck], (
            f"stuck thread must be returned, got {still_alive!r}"
        )
        assert stuck.is_alive(), "stuck thread must still be alive"
    finally:
        # Cleanup OUTSIDE the contract under test so the unit test does
        # not itself leak past its boundary.
        stop.set()
        stuck.join(timeout=1.0)
        assert not stuck.is_alive(), "test cleanup: stuck thread did not exit"
