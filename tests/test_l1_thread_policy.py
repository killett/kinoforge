"""Unit tests for the L1 thread-leak policy hookwrapper in tests/conftest.py.

These exercise the hook by direct invocation against synthetic Item /
CallInfo / TestReport stand-ins. Cheap, deterministic, no subprocess.
The e2e file `tests/test_l1_thread_policy_e2e.py` covers the full
pytest dispatch path.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# pytest's StashKey is the public stash type, but the conftest is the
# source of truth for the key instance — import it directly so a key
# rename surfaces here, not as a silent miss.
from tests.conftest import _L1_CALL_REPORT_KEY  # noqa: PLC0415


def _make_item(
    stash: pytest.Stash | None = None, nodeid: str = "tests/x.py::test_y"
) -> Any:
    """Build a duck-typed Item with the attributes the L1 hook reads."""
    return SimpleNamespace(nodeid=nodeid, stash=stash or pytest.Stash())


def _make_report(outcome: str = "passed", when: str = "call") -> pytest.TestReport:
    """Build a real TestReport — the hook flips `outcome` / `longrepr` on it."""
    return pytest.TestReport(
        nodeid="tests/x.py::test_y",
        location=("tests/x.py", 1, "test_y"),
        keywords={},
        outcome=outcome,  # type: ignore[arg-type]
        longrepr=None,
        when=when,  # type: ignore[arg-type]
    )


def _invoke_hook(item: Any, when: str, report: pytest.TestReport) -> pytest.TestReport:
    """Run the hookwrapper end-to-end against a synthetic `Outcome`.

    The hookwrapper is `pytest_runtest_makereport`. It receives an Item
    and a CallInfo; the wrapped hook produces the report. We synthesise
    the wrapped outcome by passing `report` through a fake Outcome
    namespace that mimics `_pytest.outcomes.OutcomeException`.
    """
    from tests.conftest import pytest_runtest_makereport  # noqa: PLC0415

    call = SimpleNamespace(when=when)
    # The hookwrapper protocol: it is a generator that yields once and
    # then receives the Outcome via `.send(outcome)`.
    gen = pytest_runtest_makereport(item, call)
    next(gen)  # advance to the yield point

    # Build a fake Outcome that returns our `report` from `get_result`
    # and lets the hook mutate the report in place if needed.
    class _FakeOutcome:
        def __init__(self, r: pytest.TestReport) -> None:
            self._r = r

        def get_result(self) -> pytest.TestReport:
            return self._r

    try:
        gen.send(_FakeOutcome(report))
    except StopIteration:
        pass
    return report


def test_call_phase_stashes_report_and_leaves_outcome_unchanged(tmp_path: Path) -> None:
    """`when="call"`: hook stashes the report; does not touch outcome."""
    item = _make_item()
    rep = _make_report(outcome="passed", when="call")
    _invoke_hook(item, when="call", report=rep)
    assert item.stash[_L1_CALL_REPORT_KEY] is rep
    assert rep.outcome == "passed"


def test_setup_phase_is_complete_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`when="setup"`: no stash, no inventory append, no outcome flip."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    item = _make_item()
    rep = _make_report(outcome="passed", when="setup")

    stop = threading.Event()
    leaker = threading.Thread(target=stop.wait, name="setup-noop-leaker", daemon=False)
    leaker.start()
    try:
        _invoke_hook(item, when="setup", report=rep)
        inventory = tmp_path / "tests" / "_l1_leakers_inventory.txt"
        assert not inventory.exists(), "setup phase must not write inventory"
        assert _L1_CALL_REPORT_KEY not in item.stash
        assert rep.outcome == "passed"
    finally:
        stop.set()
        leaker.join(timeout=1.0)


def test_warn_mode_teardown_appends_leaker_to_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WARN mode + passed teardown + leaker → one inventory line, outcome unchanged."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr("tests.conftest._L1_MODE", "warn")

    item = _make_item(nodeid="tests/x.py::test_warn_leak")
    # Pretend call already passed — stash it so the teardown branch reads passed.
    call_rep = _make_report(outcome="passed", when="call")
    item.stash[_L1_CALL_REPORT_KEY] = call_rep
    teardown_rep = _make_report(outcome="passed", when="teardown")

    stop = threading.Event()
    leaker = threading.Thread(target=stop.wait, name="warn-leaker", daemon=False)
    leaker.start()
    try:
        _invoke_hook(item, when="teardown", report=teardown_rep)
        inventory = tmp_path / "tests" / "_l1_leakers_inventory.txt"
        assert inventory.exists()
        line = inventory.read_text(encoding="utf-8")
        assert "tests/x.py::test_warn_leak" in line
        assert "name=warn-leaker" in line
        assert "daemon=False" in line
        assert teardown_rep.outcome == "passed", "WARN mode must not flip outcome"
    finally:
        stop.set()
        leaker.join(timeout=1.0)


def test_fail_mode_teardown_flips_outcome_and_writes_longrepr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL mode + passed teardown + leaker → outcome=failed, longrepr names leaker."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr("tests.conftest._L1_MODE", "fail")

    item = _make_item(nodeid="tests/x.py::test_fail_leak")
    item.stash[_L1_CALL_REPORT_KEY] = _make_report(outcome="passed", when="call")
    teardown_rep = _make_report(outcome="passed", when="teardown")

    stop = threading.Event()
    leaker = threading.Thread(target=stop.wait, name="fail-leaker", daemon=False)
    leaker.start()
    try:
        _invoke_hook(item, when="teardown", report=teardown_rep)
        assert teardown_rep.outcome == "failed"
        longrepr = str(teardown_rep.longrepr)
        assert "fail-leaker" in longrepr
        assert "managed_thread" in longrepr, (
            "longrepr must point fixer at managed_thread.spawn"
        )
    finally:
        stop.set()
        leaker.join(timeout=1.0)


def test_teardown_silent_when_call_already_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stashed call=failed + passed teardown + leaker → no append, no flip."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr("tests.conftest._L1_MODE", "fail")

    item = _make_item()
    item.stash[_L1_CALL_REPORT_KEY] = _make_report(outcome="failed", when="call")
    teardown_rep = _make_report(outcome="passed", when="teardown")

    stop = threading.Event()
    leaker = threading.Thread(target=stop.wait, name="silent-leaker", daemon=False)
    leaker.start()
    try:
        _invoke_hook(item, when="teardown", report=teardown_rep)
        inventory = tmp_path / "tests" / "_l1_leakers_inventory.txt"
        assert not inventory.exists()
        assert teardown_rep.outcome == "passed"
    finally:
        stop.set()
        leaker.join(timeout=1.0)


def test_teardown_silent_when_teardown_report_already_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No stash, teardown=failed + leaker → no append (managed_thread already failed loud)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr("tests.conftest._L1_MODE", "warn")

    item = _make_item()
    # No call stash — simulate managed_thread fixture pytest.fail in
    # teardown that produced a teardown-failed report directly.
    teardown_rep = _make_report(outcome="failed", when="teardown")

    stop = threading.Event()
    leaker = threading.Thread(target=stop.wait, name="post-fail-leaker", daemon=False)
    leaker.start()
    try:
        _invoke_hook(item, when="teardown", report=teardown_rep)
        inventory = tmp_path / "tests" / "_l1_leakers_inventory.txt"
        assert not inventory.exists()
        assert teardown_rep.outcome == "failed"
    finally:
        stop.set()
        leaker.join(timeout=1.0)


def test_hook_exempts_daemon_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon thread alive at teardown → no append, no flip."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr("tests.conftest._L1_MODE", "fail")

    item = _make_item()
    item.stash[_L1_CALL_REPORT_KEY] = _make_report(outcome="passed", when="call")
    teardown_rep = _make_report(outcome="passed", when="teardown")

    stop = threading.Event()
    daemon = threading.Thread(target=stop.wait, name="exempt-daemon", daemon=True)
    daemon.start()
    try:
        _invoke_hook(item, when="teardown", report=teardown_rep)
        inventory = tmp_path / "tests" / "_l1_leakers_inventory.txt"
        assert not inventory.exists()
        assert teardown_rep.outcome == "passed"
    finally:
        stop.set()
        daemon.join(timeout=1.0)


def test_hook_exempts_known_pytest_thread_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-daemon thread named in _KNOWN_PYTEST_THREADS is exempt.

    Catches regression in the exempt-set.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr("tests.conftest._L1_MODE", "fail")

    # Force enumerate() to return a fake non-daemon thread named "execnetMain"
    # — actually building an xdist worker thread is overkill for a unit test.
    main = threading.main_thread()
    fake_xdist = SimpleNamespace(
        name="execnetMain",
        ident=10**9,
        daemon=False,
        is_alive=lambda: True,
    )
    monkeypatch.setattr(threading, "enumerate", lambda: [main, fake_xdist])

    item = _make_item()
    item.stash[_L1_CALL_REPORT_KEY] = _make_report(outcome="passed", when="call")
    teardown_rep = _make_report(outcome="passed", when="teardown")

    _invoke_hook(item, when="teardown", report=teardown_rep)
    inventory = tmp_path / "tests" / "_l1_leakers_inventory.txt"
    assert not inventory.exists()
    assert teardown_rep.outcome == "passed"
