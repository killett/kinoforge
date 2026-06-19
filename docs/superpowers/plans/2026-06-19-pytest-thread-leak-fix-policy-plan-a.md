# Pytest thread-leak FIX policy — Plan A (fixture + L1 WARN + harvest)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Phases 1-2 of the thread-leak FIX policy: the `managed_thread` fixture, the L1 `pytest_runtest_makereport(when="teardown")` hook in WARN mode, full test coverage of both, and a frozen harvest of every leaker in the existing suite.

**Architecture:** Add a new function-scoped `managed_thread` fixture (sanctioned escape hatch for non-daemon test threads) and a new pytest hook layer L1 that fires at teardown phase (after fixture finalizers) to flag unregistered non-daemon non-main threads. L1 starts in WARN mode (file append, no test failure) so the rollout phase can harvest the existing suite's leaker inventory without breaking CI. Phases 3-5 (per-leaker fixes + WARN→FAIL flip) are deferred to a follow-up plan written FROM the harvest data, not in advance of it.

**Tech Stack:** Python 3.13, pytest, stdlib `threading`, pytest's `Stash` / `StashKey` API for cross-phase report sharing. No new deps.

**User decisions (already made):**
- "Defensive hardening / policy + lint" — chose runtime policy + fixture over targeted speculative fix.
- "Runtime: pytest fails any test that leaks" — no static-analysis layer.
- "Lenient: daemon=True OR registered with cleanup fixture" — sanctioned escape hatch.
- "Both: .spawn(...) + .register(existing)" — fixture exposes both surfaces.
- "Big-bang: identify + fix all existing leakers, then enable enforcement" — WARN-harvest-fix-flip rollout, no permanent allowlist.

**Out of scope for this plan (deferred to Plan B, written post-harvest):**
- Per-leaker fix tasks (N unknown until Task 4 produces the harvest).
- The WARN→FAIL mode flip (Phase 5 of the spec).
- Deletion of `tests/_l1_leakers_inventory.txt` and the WARN-only code path.

---

## File structure

| Path | New / Modify | Responsibility |
|---|---|---|
| `tests/conftest.py` | **MODIFY** | Add `_KNOWN_PYTEST_THREADS` frozenset, `_L1_MODE` constant (defaults `"warn"`), `_L1_CALL_REPORT_KEY` stash key, `_ManagedThreadRegistrar` class, `managed_thread` fixture, `pytest_runtest_makereport` hookwrapper. Existing `pytest_configure` (L0) and `pytest_sessionfinish` (L2) blocks unchanged. |
| `tests/test_managed_thread_fixture.py` | **NEW** (~120 LOC) | 4 fixture unit tests. |
| `tests/test_l1_thread_policy.py` | **NEW** (~200 LOC) | 8 L1 hook unit tests (direct invocation, no subprocess). |
| `tests/test_l1_thread_policy_e2e.py` | **NEW** (~80 LOC) | 1 subprocess e2e covering both managed and unmanaged paths. |
| `tests/_subprocess_pytest_helper.py` | **NEW** (~50 LOC) | Tiny `_run_subprocess_pytest(tmp_path, test_body)` extracted from the existing `tests/test_post_session_dump_e2e.py` Popen+TimeoutExpired pattern so the L1 e2e doesn't copy-paste. |
| `tests/test_post_session_dump_e2e.py` | **MODIFY** | Replace its inline Popen block with a call into the new helper. Behaviour-preserving. |
| `.gitignore` | **MODIFY** | Add `tests/_l1_leakers_inventory.txt`. |
| `docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md` | **NEW** (Task 4 output) | Frozen leaker inventory: one row per unique `(name, daemon)` tuple with the count of distinct test nodeids that triggered it. |

---

## Task 1: `managed_thread` fixture + 4 unit tests

**Goal:** Add the `_ManagedThreadRegistrar` class + `managed_thread` fixture to `tests/conftest.py` with `.spawn()` and `.register()` surfaces and a `_teardown(join_timeout=2.0)` joiner. Cover with 4 unit tests pinning the contract.

**Files:**
- Modify: `tests/conftest.py` — insert new fixture block above the existing L0 `pytest_configure` block.
- Create: `tests/test_managed_thread_fixture.py`

**Acceptance Criteria:**
- [ ] `_ManagedThreadRegistrar.spawn(target, *, name, daemon=False, args=(), kwargs=None)` constructs, starts, and registers a thread; returns the `Thread` instance; `name` is required keyword-only.
- [ ] `_ManagedThreadRegistrar.register(thread)` appends and returns the thread for fluent chaining (`assert .register(t) is t`).
- [ ] `_ManagedThreadRegistrar._teardown(join_timeout)` joins every registered thread; returns the list of any still alive after the timeout.
- [ ] `managed_thread` fixture is function-scoped; on test exit, runs `_teardown(2.0)`; if any thread is still alive, calls `pytest.fail(..., pytrace=False)` with a terse message naming the stuck thread(s).
- [ ] All 4 tests in `tests/test_managed_thread_fixture.py` pass under `pixi run pytest tests/test_managed_thread_fixture.py -v`.
- [ ] `pixi run ruff check tests/_thread_dump_helper.py tests/conftest.py tests/test_managed_thread_fixture.py` zero new findings.
- [ ] `pixi run mypy tests/conftest.py tests/test_managed_thread_fixture.py` zero new findings.

**Verify:** `pixi run pytest tests/test_managed_thread_fixture.py -v` → 4 passed in <2 s.

**Steps:**

- [ ] **Step 1.1: Write the 4 failing unit tests first.**

Create `tests/test_managed_thread_fixture.py`:

```python
"""Unit tests for the managed_thread fixture and _ManagedThreadRegistrar.

These pin the registrar's contract independent of the pytest hook plumbing
so a refactor that breaks .spawn / .register / _teardown semantics surfaces
locally, not from an opaque L1 false-positive on an unrelated test.
"""

from __future__ import annotations

import threading
import time

import pytest

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
```

- [ ] **Step 1.2: Run tests — expect ImportError.**

```bash
pixi run pytest tests/test_managed_thread_fixture.py -v
```
Expected: 4 collection errors with `ImportError: cannot import name '_ManagedThreadRegistrar'` from `tests.conftest`.

- [ ] **Step 1.3: Add the registrar + fixture to `tests/conftest.py`.**

Insert the following block in `tests/conftest.py` immediately ABOVE the existing `# ---------------------------------------------------------------------------\n# Faulthandler safety net for pytest hangs ...` block (which currently starts the L0 / L2 region). Keep the L0 + L2 blocks below unchanged.

```python
# ---------------------------------------------------------------------------
# managed_thread fixture (spec
# docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md).
#
# Sanctioned escape hatch for non-daemon test threads. Threads registered
# here are joined (with timeout) in fixture teardown BEFORE the L1
# pytest_runtest_makereport(when="teardown") hook enumerates leakers, so
# registered threads never appear as L1 leakers on the happy path. A
# registered thread that does not join within the timeout fails the
# test loudly via pytest.fail.
# ---------------------------------------------------------------------------


class _ManagedThreadRegistrar:
    """Registry of threads owned by the current test, joined at teardown."""

    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []

    def spawn(
        self,
        target: Callable[..., Any],
        *,
        name: str,
        daemon: bool = False,
        args: tuple[Any, ...] = (),
        kwargs: Mapping[str, Any] | None = None,
    ) -> threading.Thread:
        """Construct, start, and register a thread in one call.

        Args:
            target: The callable the thread will run.
            name: Human-readable thread name. Required keyword-only so an
                anonymous leaker is impossible to ship.
            daemon: Default ``False`` — opting into this fixture means
                opting into non-daemon by default. Override allowed.
            args: Positional args for ``target``.
            kwargs: Keyword args for ``target``.

        Returns:
            The started ``threading.Thread`` instance.
        """
        t = threading.Thread(
            target=target,
            name=name,
            daemon=daemon,
            args=args,
            kwargs=dict(kwargs or {}),
        )
        t.start()
        self._threads.append(t)
        return t

    def register(self, thread: threading.Thread) -> threading.Thread:
        """Register a pre-started thread for managed teardown.

        Use when a library / SDK constructs the thread and hands it back
        to test code — ``.spawn`` is the right surface only when the test
        owns the construction.

        Args:
            thread: A ``threading.Thread`` that has already been started.

        Returns:
            The same ``thread`` for fluent chaining
            (``t = managed_thread.register(threading.Thread(...))``).
        """
        self._threads.append(thread)
        return thread

    def _teardown(self, join_timeout: float = 2.0) -> list[threading.Thread]:
        """Join every registered thread; return any still alive."""
        still_alive: list[threading.Thread] = []
        for t in self._threads:
            t.join(timeout=join_timeout)
            if t.is_alive():
                still_alive.append(t)
        return still_alive


@pytest.fixture
def managed_thread() -> Iterator[_ManagedThreadRegistrar]:
    """Function-scoped registrar for non-daemon test threads.

    Yields:
        ``_ManagedThreadRegistrar`` instance. Teardown joins every
        registered thread with a 2.0 s timeout; any thread still alive
        triggers ``pytest.fail`` with a terse message naming it.
    """
    registrar = _ManagedThreadRegistrar()
    try:
        yield registrar
    finally:
        still_alive = registrar._teardown(join_timeout=2.0)
        if still_alive:
            names = ", ".join(
                f"{t.name!r}(ident={t.ident})" for t in still_alive
            )
            pytest.fail(
                f"managed_thread: {len(still_alive)} registered thread(s) "
                f"did not join within 2.0s: {names}",
                pytrace=False,
            )
```

You will also need to add to the top-of-file imports (if not already present):

```python
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest
```

Verify which of those are already imported before adding; do not duplicate.

- [ ] **Step 1.4: Run tests — expect them to pass.**

```bash
pixi run pytest tests/test_managed_thread_fixture.py -v
```
Expected: 4 passed in <2 s.

- [ ] **Step 1.5: Lint / typecheck.**

```bash
pixi run ruff check tests/conftest.py tests/test_managed_thread_fixture.py
pixi run mypy tests/conftest.py tests/test_managed_thread_fixture.py
```
Expected: zero new findings.

- [ ] **Step 1.6: Commit.**

```bash
git add tests/conftest.py tests/test_managed_thread_fixture.py
git commit -m "feat(tests): add managed_thread fixture + registrar (Plan A Task 1)

Sanctioned escape hatch for legitimate non-daemon test threads. Two
surfaces:
 - .spawn(target, *, name, daemon=False, args, kwargs) → construct,
   start, register in one call. name is keyword-only so anonymous
   leakers are impossible to ship.
 - .register(thread) → accept a pre-started Thread (the SDK-handed
   case) and return it for fluent chaining.

Function-scoped fixture; teardown joins all registered threads with a
2.0s timeout, pytest.fail (pytrace=False) on stuck threads.

Companion: docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md"
```

---

## Task 2: L1 hook in WARN mode + 8 unit tests

**Goal:** Add the L1 `pytest_runtest_makereport` hookwrapper to `tests/conftest.py` in WARN mode, with cross-phase report stashing for silent-on-already-failed semantics. Cover with 8 unit tests pinning the contract.

**Files:**
- Modify: `tests/conftest.py` — insert L1 hook block above the L0 `pytest_configure` block (already moved adjacent by Task 1) or below the `managed_thread` fixture block, but above L0. Add `_L1_MODE`, `_KNOWN_PYTEST_THREADS`, `_L1_CALL_REPORT_KEY` module-level symbols.
- Modify: `.gitignore` — add `tests/_l1_leakers_inventory.txt`.
- Create: `tests/test_l1_thread_policy.py`

**Acceptance Criteria:**
- [ ] `_L1_MODE: Literal["warn", "fail"] = "warn"` module-level constant present in `tests/conftest.py`.
- [ ] `_KNOWN_PYTEST_THREADS = frozenset({"MainThread", "execnetMain"})` present.
- [ ] `_L1_CALL_REPORT_KEY: StashKey[pytest.TestReport] = pytest.StashKey()` present.
- [ ] `pytest_runtest_makereport(item, call)` hookwrapper registered with `trylast=True`.
- [ ] On `when="call"`: stashes the resulting `TestReport` into `item.stash[_L1_CALL_REPORT_KEY]`. Does not modify outcome.
- [ ] On `when="setup"`: no-op (no stash, no append, no flip).
- [ ] On `when="teardown"`: if EITHER the stashed call report OR the teardown report has `outcome == "failed"`, no-op. Otherwise enumerates `threading.enumerate()`, filters to non-daemon non-main non-known leakers, and:
  - WARN mode (`_L1_MODE == "warn"`): append one tab-separated line per leaker to `tests/_l1_leakers_inventory.txt` in the format `<nodeid>\tname=<thread name>\tdaemon=<bool>\tident=<int>\n`. Do not modify outcome.
  - FAIL mode (`_L1_MODE == "fail"`): flip the teardown report's `outcome` to `"failed"`; set its `longrepr` to a multiline string naming the test, leaker count, per-leaker details, and the fix recipe pointing at `managed_thread.spawn`.
- [ ] Inventory-file write `OSError` is swallowed; emits a one-time `sys.stderr.write("L1 inventory write failed: ...")` and lets outcome pass through unchanged.
- [ ] `tests/_l1_leakers_inventory.txt` added to `.gitignore`.
- [ ] All 8 tests in `tests/test_l1_thread_policy.py` pass under `pixi run pytest tests/test_l1_thread_policy.py -v`.
- [ ] `pixi run ruff check tests/conftest.py tests/test_l1_thread_policy.py` zero new findings.
- [ ] `pixi run mypy tests/conftest.py tests/test_l1_thread_policy.py` zero new findings.

**Verify:** `pixi run pytest tests/test_l1_thread_policy.py -v` → 8 passed in <2 s.

**Steps:**

- [ ] **Step 2.1: Write all 8 failing unit tests first.**

Create `tests/test_l1_thread_policy.py`:

```python
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


def _make_item(stash: pytest.Stash | None = None, nodeid: str = "tests/x.py::test_y") -> Any:
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


def test_setup_phase_is_complete_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        name="execnetMain", ident=10**9, daemon=False, is_alive=lambda: True,
    )
    monkeypatch.setattr(threading, "enumerate", lambda: [main, fake_xdist])

    item = _make_item()
    item.stash[_L1_CALL_REPORT_KEY] = _make_report(outcome="passed", when="call")
    teardown_rep = _make_report(outcome="passed", when="teardown")

    _invoke_hook(item, when="teardown", report=teardown_rep)
    inventory = tmp_path / "tests" / "_l1_leakers_inventory.txt"
    assert not inventory.exists()
    assert teardown_rep.outcome == "passed"
```

- [ ] **Step 2.2: Run tests — expect ImportError on `_L1_CALL_REPORT_KEY`.**

```bash
pixi run pytest tests/test_l1_thread_policy.py -v
```
Expected: 8 collection errors with `ImportError: cannot import name '_L1_CALL_REPORT_KEY'`.

- [ ] **Step 2.3: Add the L1 hook + symbols to `tests/conftest.py`.**

Insert the following block in `tests/conftest.py` immediately ABOVE the existing `# ---\n# Faulthandler safety net for pytest hangs ...` block. This places L1 between the new `managed_thread` block (Task 1) and the L0 block (shipped). Keep L0 + L2 unchanged.

```python
# ---------------------------------------------------------------------------
# L1 thread-leak policy (spec
# docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md).
#
# Per-test enforcement. Fires at pytest_runtest_makereport(when="teardown")
# AFTER fixture finalizers have run, so managed_thread-registered threads
# have already been joined and only unregistered leakers remain. Cross-phase
# silence-on-already-failed is implemented via the call-phase stash hop.
# ---------------------------------------------------------------------------


_KNOWN_PYTEST_THREADS: frozenset[str] = frozenset({"MainThread", "execnetMain"})

# WARN: append one line per (test, leaker) to tests/_l1_leakers_inventory.txt;
#       leave the test outcome unchanged. Used during Phase 2 harvest.
# FAIL: flip the teardown-phase report's outcome to failed; set longrepr with
#       the leaker inventory + fix recipe.
_L1_MODE: Literal["warn", "fail"] = "warn"

_L1_CALL_REPORT_KEY: pytest.StashKey[pytest.TestReport] = pytest.StashKey()


def _l1_collect_leakers() -> list[threading.Thread]:
    """Enumerate non-daemon non-main non-known threads currently alive."""
    main = threading.main_thread()
    return [
        t
        for t in threading.enumerate()
        if t is not main
        and not t.daemon
        and t.is_alive()
        and t.name not in _KNOWN_PYTEST_THREADS
    ]


def _l1_append_warn(nodeid: str, leakers: list[threading.Thread]) -> None:
    """Append one tab-separated line per leaker to the inventory file.

    Format pinned for downstream Phase 2 sort+uniq grep:
        <nodeid>\\tname=<thread name>\\tdaemon=<bool>\\tident=<int>\\n
    """
    inventory = Path("tests/_l1_leakers_inventory.txt")
    lines = [
        f"{nodeid}\tname={t.name}\tdaemon={t.daemon}\tident={t.ident}\n"
        for t in leakers
    ]
    try:
        inventory.parent.mkdir(parents=True, exist_ok=True)
        with inventory.open("a", encoding="utf-8") as fp:
            fp.writelines(lines)
    except OSError as exc:
        sys.stderr.write(f"L1 inventory write failed: {exc}\n")


def _l1_build_longrepr(nodeid: str, leakers: list[threading.Thread]) -> str:
    """Compose the FAIL-mode longrepr text."""
    lines = "\n".join(
        f"  • name={t.name!r} ident={t.ident} daemon={t.daemon} alive={t.is_alive()}"
        for t in leakers
    )
    return (
        f"L1 thread-leak policy: test {nodeid!r} exited with "
        f"{len(leakers)} non-daemon non-main non-managed thread(s) still alive.\n"
        f"{lines}\n"
        f"Fix: spawn via `managed_thread.spawn(target, name=...)` or pass "
        f"`daemon=True`. See "
        f"docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md."
    )


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):  # noqa: ANN001
    """L1 per-test enforcement; see module-level block comment for contract."""
    outcome = yield
    if call.when == "call":
        # Stash the call-phase report so the teardown pass can cross-reference
        # it for silent-on-already-failed semantics. No mutation here.
        item.stash[_L1_CALL_REPORT_KEY] = outcome.get_result()
        return
    if call.when != "teardown":
        return  # setup phase is fully no-op

    teardown_rep = outcome.get_result()
    if teardown_rep.outcome == "failed":
        return  # managed_thread fixture already failed loudly
    call_rep = item.stash.get(_L1_CALL_REPORT_KEY, None)
    if call_rep is not None and call_rep.outcome == "failed":
        return  # genuine assert failure surfaces first

    leakers = _l1_collect_leakers()
    if not leakers:
        return

    if _L1_MODE == "warn":
        _l1_append_warn(item.nodeid, leakers)
        return

    # _L1_MODE == "fail"
    teardown_rep.outcome = "failed"
    teardown_rep.longrepr = _l1_build_longrepr(item.nodeid, leakers)
```

Add to imports at the top of `tests/conftest.py` (verify which are already present):

```python
import sys
import threading
from pathlib import Path
from typing import Literal

import pytest
```

- [ ] **Step 2.4: Add the inventory file to `.gitignore`.**

Append a single line to `.gitignore`:

```
tests/_l1_leakers_inventory.txt
```

- [ ] **Step 2.5: Run tests — expect 8 passes.**

```bash
pixi run pytest tests/test_l1_thread_policy.py -v
```
Expected: 8 passed in <2 s.

- [ ] **Step 2.6: Run the existing suites to confirm no regression.**

```bash
pixi run pytest tests/test_post_session_dump.py tests/test_post_session_dump_e2e.py tests/test_conftest_post_session_dump.py tests/test_managed_thread_fixture.py tests/test_l1_thread_policy.py -v
```
Expected: 17 passed (3 + 1 + 5 + 4 + 8 + the existing tests' own count if different). All previously green stays green.

- [ ] **Step 2.7: Lint / typecheck.**

```bash
pixi run ruff check tests/conftest.py tests/test_l1_thread_policy.py
pixi run mypy tests/conftest.py tests/test_l1_thread_policy.py
```
Expected: zero new findings.

- [ ] **Step 2.8: Commit.**

```bash
git add tests/conftest.py tests/test_l1_thread_policy.py .gitignore
git commit -m "feat(tests): add L1 thread-leak policy hook in WARN mode (Plan A Task 2)

pytest_runtest_makereport(hookwrapper=True, trylast=True) fires per
test, no-ops on setup, stashes the call-phase report (for cross-phase
silence-on-already-failed), and enforces on teardown.

Teardown branch:
 - silent if EITHER the stashed call report OR the teardown report is
   already failed (genuine assert failure surfaces first; managed_thread
   fixture pytest.fail already named the stuck thread).
 - enumerate non-daemon non-main non-known leakers via
   _l1_collect_leakers().
 - WARN: append tab-separated line per leaker to
   tests/_l1_leakers_inventory.txt (gitignored).
 - FAIL: flip teardown outcome to failed, set longrepr to leaker
   inventory + managed_thread.spawn fix recipe.

_L1_MODE starts at \"warn\" — Plan B will flip to \"fail\" after
per-leaker fixes land (Phase 5 of the spec).

Companion: docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md"
```

---

## Task 3: Subprocess helper extraction + L1 e2e smoke

**Goal:** Extract the Popen+TimeoutExpired+PYTHONPATH pattern from `tests/test_post_session_dump_e2e.py` into a tiny shared helper, then write one e2e test that spawns pytest as a subprocess on a temp test module exercising both the managed and unmanaged paths.

**Files:**
- Create: `tests/_subprocess_pytest_helper.py` (~50 LOC)
- Modify: `tests/test_post_session_dump_e2e.py` — replace its inline Popen block with a call into the helper. Behaviour-preserving.
- Create: `tests/test_l1_thread_policy_e2e.py` (~80 LOC)

**Acceptance Criteria:**
- [ ] `tests/_subprocess_pytest_helper.py` defines `_run_subprocess_pytest(tmp_path, test_body, *, timeout=10.0) -> tuple[str, str, int | None]` returning `(stdout, stderr, returncode)`.
- [ ] The helper preserves the existing e2e's behaviour: PYTHONPATH injection (tmp + project src + project root), Popen + communicate with timeout, TimeoutExpired-handling with byte-decode fallback.
- [ ] `tests/test_post_session_dump_e2e.py` calls the helper and still passes (`pixi run pytest tests/test_post_session_dump_e2e.py -v` → 1 passed).
- [ ] `tests/test_l1_thread_policy_e2e.py::test_unmanaged_leaker_renders_error_and_managed_renders_pass` PASSES under `pixi run pytest tests/test_l1_thread_policy_e2e.py -v` in <30 s.
- [ ] The e2e asserts: subprocess exit code is non-zero, combined stdout+stderr contains `ERROR` near `test_unmanaged_leaker_fails`, contains `L1 thread-leak policy` somewhere in the output, AND `PASSED` near `test_managed_leaker_passes`.
- [ ] `pixi run ruff check tests/_subprocess_pytest_helper.py tests/test_l1_thread_policy_e2e.py tests/test_post_session_dump_e2e.py` zero new findings.
- [ ] `pixi run mypy tests/_subprocess_pytest_helper.py tests/test_l1_thread_policy_e2e.py tests/test_post_session_dump_e2e.py` zero new findings.

**Verify:** `pixi run pytest tests/test_post_session_dump_e2e.py tests/test_l1_thread_policy_e2e.py -v` → 2 passed in <30 s.

**Steps:**

- [ ] **Step 3.1: Extract the helper.**

Create `tests/_subprocess_pytest_helper.py`:

```python
"""Shared subprocess-pytest harness for thread-diagnostic e2e tests.

The post-session dump e2e and the L1 policy e2e both spawn pytest as a
subprocess against a temp test module, with kinoforge's pyproject
``pythonpath = ["src"]`` config bypassed (subprocess cwd is the tmp
dir). They both also need TimeoutExpired-handling because a leaked
non-daemon thread blocks the subprocess past summary.

Single helper here keeps both tests honest and prevents drift.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

_TIMEOUT_DEFAULT: Final[float] = 10.0


def _as_str(payload: str | bytes | None) -> str:
    """subprocess.TimeoutExpired carries raw bytes regardless of text=True."""
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def _run_subprocess_pytest(
    tmp_path: Path,
    test_body: str,
    *,
    test_filename: str = "test_leaky.py",
    timeout: float = _TIMEOUT_DEFAULT,
) -> tuple[str, str, int | None]:
    """Spawn pytest in `tmp_path` against `test_body` written to `test_filename`.

    Copies the project's ``tests/conftest.py`` + ``tests/_thread_dump_helper.py``
    into a sibling ``tests/`` directory under ``tmp_path`` so the
    subprocess picks up the same diagnostic + policy stack. Injects
    ``PYTHONPATH=tmp:project/src:project`` so the conftest's
    ``kinoforge.core.errors`` import resolves.

    Args:
        tmp_path: pytest's per-test tmp dir.
        test_body: Source code of the test module to run.
        test_filename: Filename under ``tmp_path/tests/``.
        timeout: Seconds before the subprocess is killed. Default 10s
            — leaked threads block shutdown indefinitely so we never
            want to wait for natural exit.

    Returns:
        Tuple of (stdout, stderr, returncode). returncode is ``None``
        when the subprocess was killed for timeout.
    """
    project_root = Path(__file__).resolve().parents[1]
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    for name in ("conftest.py", "_thread_dump_helper.py"):
        (tests_dir / name).write_text(
            (project_root / "tests" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (tests_dir / test_filename).write_text(test_body, encoding="utf-8")

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(tmp_path), str(project_root / "src"), str(project_root)]
        ),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tests_dir / test_filename),
            "-v",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    returncode: int | None
    try:
        stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
        stdout, stderr = _as_str(stdout_raw), _as_str(stderr_raw)
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        post_stdout, post_stderr = proc.communicate()
        stdout = _as_str(exc.stdout) or _as_str(post_stdout)
        stderr = _as_str(exc.stderr) or _as_str(post_stderr)
        returncode = None
    return stdout, stderr, returncode
```

- [ ] **Step 3.2: Refactor `tests/test_post_session_dump_e2e.py` to call the helper.**

Replace the entire body of `test_session_dump_fires_on_leaked_non_daemon_thread` (everything inside the `def`) with:

```python
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
```

Drop the imports at the top of `tests/test_post_session_dump_e2e.py` that the inline Popen needed but the helper-using version does not (`os`, `subprocess`, `sys`). Keep `textwrap` and `pathlib.Path`.

- [ ] **Step 3.3: Run the post-session e2e — confirm still green.**

```bash
pixi run pytest tests/test_post_session_dump_e2e.py -v
```
Expected: 1 passed in <12 s.

If it fails, the refactor was not behaviour-preserving — debug before continuing.

- [ ] **Step 3.4: Write the L1 e2e test.**

Create `tests/test_l1_thread_policy_e2e.py`:

```python
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
```

- [ ] **Step 3.5: Run the L1 e2e — expect green.**

```bash
pixi run pytest tests/test_l1_thread_policy_e2e.py -v
```
Expected: 1 passed in <20 s.

- [ ] **Step 3.6: Full thread-stack regression run.**

```bash
pixi run pytest tests/test_post_session_dump.py tests/test_post_session_dump_e2e.py tests/test_conftest_post_session_dump.py tests/test_managed_thread_fixture.py tests/test_l1_thread_policy.py tests/test_l1_thread_policy_e2e.py -v
```
Expected: 22 passed total (3+1+5+4+8+1) in <40 s.

- [ ] **Step 3.7: Lint / typecheck.**

```bash
pixi run ruff check tests/_subprocess_pytest_helper.py tests/test_l1_thread_policy_e2e.py tests/test_post_session_dump_e2e.py
pixi run mypy tests/_subprocess_pytest_helper.py tests/test_l1_thread_policy_e2e.py tests/test_post_session_dump_e2e.py
```
Expected: zero new findings.

- [ ] **Step 3.8: Commit.**

```bash
git add tests/_subprocess_pytest_helper.py tests/test_l1_thread_policy_e2e.py tests/test_post_session_dump_e2e.py
git commit -m "feat(tests): L1 policy e2e smoke + shared subprocess helper (Plan A Task 3)

tests/_subprocess_pytest_helper.py extracts the Popen+TimeoutExpired
+PYTHONPATH pattern (originally inline in
tests/test_post_session_dump_e2e.py) into a shared helper used by both
the post-session dump e2e and the new L1 policy e2e. Behaviour-
preserving for the post-session dump e2e (same assertions, same
timeouts).

tests/test_l1_thread_policy_e2e.py spawns pytest on a temp module
with two tests:
 - test_unmanaged_leaker_fails: bare threading.Thread(daemon=False)
   that the test does not join. Must render as ERROR with the L1
   longrepr.
 - test_managed_leaker_passes: managed_thread.spawn(...) for the
   same shape thread. Fixture teardown joins; test must render as
   PASSED.

The subprocess flips _L1_MODE='fail' inline at the top of the temp
test module so the outer pytest run stays in WARN mode."
```

---

## Task 4: Harvest existing-suite leakers + write the frozen inventory

**Goal:** Run the full kinoforge test suite under L1 WARN mode, harvest every leaker into `tests/_l1_leakers_inventory.txt`, then translate that raw file into a curated, committable harvest document for Plan B to consume.

**Files:**
- Generate (gitignored, not committed): `tests/_l1_leakers_inventory.txt`
- Create: `docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md`

**Acceptance Criteria:**
- [ ] `tests/_l1_leakers_inventory.txt` is regenerated from scratch (delete any prior contents first) by running the full suite.
- [ ] The harvest doc lists, for every unique `(thread name, daemon)` pair found in the raw inventory: the thread name, the daemon flag, the count of distinct test nodeids that triggered it, and the first 3 test nodeids alphabetically (for grepability — full list lives in the raw inventory).
- [ ] The harvest doc is sorted descending by count (most-frequent leaker first).
- [ ] The harvest doc has a preamble citing the spec, the L1 WARN mode commit SHA (Task 2), and the date the harvest was captured.
- [ ] If the raw inventory file is empty after the suite run, the harvest doc records "No leakers detected" — Plan B then degenerates to a single "flip mode to fail" task.

**Verify:** Manual — `cat docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md` shows a sorted table with at least the column headers `name`, `daemon`, `count`, `first 3 nodeids` (rows present if leakers were found).

**Steps:**

- [ ] **Step 4.1: Truncate any pre-existing inventory.**

```bash
rm -f tests/_l1_leakers_inventory.txt
```

- [ ] **Step 4.2: Run the FULL suite (not a subset) under L1 WARN mode.**

```bash
pixi run pytest -q
```
Expected: the suite finishes. Some tests may show as FAILED for unrelated reasons (live smokes that require credentials, etc.) — that is acceptable. The L1 WARN side-effect (file append) runs regardless of test outcome.

The full suite may take several minutes. Run with `--maxfail=0` (the kinoforge default in `pyproject.toml`) so a single test failure does not halt collection of the inventory.

If the suite hangs past ~10 minutes, the L0 faulthandler timer will have produced a C-side dump — investigate that hang BEFORE moving on. A hang during harvest invalidates the inventory because not every test got to run.

- [ ] **Step 4.3: Inspect the raw inventory.**

```bash
wc -l tests/_l1_leakers_inventory.txt
sort -u tests/_l1_leakers_inventory.txt | head -50
```

If the file does not exist, no test in the suite leaked — skip to Step 4.5 and record "No leakers detected".

- [ ] **Step 4.4: Produce the curated harvest document.**

Run this Python snippet (one-off — do NOT commit a script for it; the harvest is a one-time act):

```bash
pixi run python - <<'PY'
import collections
from pathlib import Path

src = Path("tests/_l1_leakers_inventory.txt")
if not src.exists() or src.stat().st_size == 0:
    Path("docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md").write_text(
        "# Plan A Task 4 — L1 leaker harvest\n\n"
        "**Captured:** $(date -I)\n"
        "**Spec:** docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md\n\n"
        "**Result:** No leakers detected. Plan B degenerates to a single Phase 5 task.\n",
        encoding="utf-8",
    )
    print("No leakers — harvest doc written as empty.")
    raise SystemExit

per_key = collections.defaultdict(list)
for line in src.read_text(encoding="utf-8").splitlines():
    parts = dict(p.split("=", 1) for p in line.split("\t")[1:] if "=" in p)
    nodeid = line.split("\t", 1)[0]
    key = (parts.get("name", "?"), parts.get("daemon", "?"))
    per_key[key].append(nodeid)

rows = sorted(
    ((name, daemon, len(set(nodeids)), sorted(set(nodeids))[:3])
     for (name, daemon), nodeids in per_key.items()),
    key=lambda r: -r[2],
)

lines = [
    "# Plan A Task 4 — L1 leaker harvest",
    "",
    "**Spec:** `docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md`",
    "",
    "**Raw inventory (gitignored):** `tests/_l1_leakers_inventory.txt`",
    "",
    "| name | daemon | count | first 3 nodeids |",
    "|---|---|---|---|",
]
for name, daemon, count, sample in rows:
    sample_str = "<br>".join(sample)
    lines.append(f"| `{name}` | {daemon} | {count} | {sample_str} |")
Path("docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md").write_text(
    "\n".join(lines) + "\n", encoding="utf-8"
)
print(f"Harvest doc written with {len(rows)} unique leaker entries.")
PY
```

- [ ] **Step 4.5: Edit the harvest doc preamble.**

Open `docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md` and replace the literal `$(date -I)` placeholder (if present from the no-leakers branch) with today's local date in `YYYY-MM-DD` form. If the with-leakers branch ran, prepend a `**Captured:**` line right after the title using today's local date and `git rev-parse HEAD` for the SHA.

```bash
SHA=$(git rev-parse HEAD)
DATE=$(date +%Y-%m-%d)
# Manually edit the preamble; do not commit a sed script.
```

- [ ] **Step 4.6: Commit the harvest doc.**

```bash
git add docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-harvest.md
git commit -m "docs: capture L1 leaker harvest from existing suite (Plan A Task 4)

Frozen inventory: <N> unique leaker entries (or 'no leakers
detected') captured by running the full kinoforge suite under
L1 WARN mode against HEAD <sha>.

Plan B is now derivable from this document — one fix task per
unique leaker entry, then the WARN-FAIL flip + dead-code delete.

Companion: docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md"
```

Substitute the actual entry count and SHA at commit time.

---

## Self-Review

**Spec coverage.**
- Architecture L0/L1/L2 table — L1 (this plan) Task 2 ships the hook; L0 + L2 left untouched per spec. ✓
- L1 policy contract (daemon / managed / known names) — Task 2 implements `_l1_collect_leakers()` exactly as specced. ✓
- `_L1_MODE` warn/fail semantics — Task 2 ships WARN as default; FAIL deferred to Plan B per the "out of scope" note in the plan header. ✓
- Phase ordering (`when="teardown"` after fixture finalization) — Task 2 hook fires on teardown; Task 3 e2e exercises the ordering. ✓
- Cross-phase silence via `item.stash` — Task 2 implements; Task 2 unit tests #5 and #6 pin both halves. ✓
- `managed_thread` fixture API (`.spawn` + `.register` + `_teardown`) — Task 1 implements; Task 1's 4 tests cover all four contracts. ✓
- E2e covers both managed and unmanaged paths — Task 3. ✓
- Big-bang rollout's Phase 2 harvest — Task 4. ✓
- Phases 3-5 deferred to Plan B — documented in plan header "Out of scope" + Task 4 commit message. ✓
- Error handling table — `OSError` swallow on inventory write is in Task 2 step 2.3; `managed_thread.spawn` failures are covered by registrar test #4; hook-internal exceptions are NOT explicitly covered (the spec named them as a mitigation; the unit tests do not exercise this). **Gap acknowledged**: the unit tests assume the hook never raises. If a future change introduces an exception path, the unit tests will catch it as a test failure (`gen.send` raises the exception), which surfaces it loudly — acceptable for Plan A.

**Placeholder scan.** No "TBD", "TODO", "implement later", "similar to Task N". Task 4 has one parameterised value (the harvest count) — written as `<N>` in the commit message template, to be substituted at execution time. Task 4 also has `$(date -I)` and `$SHA` shell substitutions — those are LITERAL shell expansions, not placeholder text. ✓

**Type consistency.**
- `_ManagedThreadRegistrar` referenced in Tasks 1, 2 (via spec), 3 (e2e). Signature `spawn(target, *, name, daemon=False, args=(), kwargs=None) -> Thread` consistent. `register(thread) -> Thread` consistent. ✓
- `_L1_MODE`, `_KNOWN_PYTEST_THREADS`, `_L1_CALL_REPORT_KEY` defined in Task 2; referenced symbolically by Task 3 e2e (which overrides `_L1_MODE` at runtime). ✓
- `_l1_collect_leakers()`, `_l1_append_warn()`, `_l1_build_longrepr()` defined in Task 2; not referenced by tests directly (they are private helpers); covered indirectly via the hookwrapper invocation. ✓
- `_run_subprocess_pytest(tmp_path, test_body, *, test_filename, timeout) -> tuple[str, str, int|None]` defined in Task 3; used by both Task 3's L1 e2e and Task 3's refactor of the post-session-dump e2e. Return tuple unpacked consistently. ✓

**User-gate scan.** No `Verbs+Scope` or `Verbs+Proof` co-occurrence in the brief; no Nouns matches ("smoke test" appears in step descriptions but not as an acceptance criterion phrasing); the user-memory `Run autonomously — no user-gates` standing instruction further suppresses tagging. No tasks tagged `userGate: true`. ✓

No issues found.

---

## Task persistence

After plan landing, the corresponding `.tasks.json` carries 4 tasks:

- Task 1: managed_thread fixture + 4 unit tests. `blockedBy: []`.
- Task 2: L1 hook in WARN mode + 8 unit tests. `blockedBy: [<Task 1 id>]` (the silence-on-already-failed tests need the fixture path to exist).
- Task 3: subprocess helper + L1 e2e. `blockedBy: [<Task 2 id>]`.
- Task 4: harvest run + harvest doc. `blockedBy: [<Task 3 id>]` (full L1 must verify before running against the real suite).

All four tagged `modelTier: "mechanical"` — the plan is exhaustive enough that an executing agent does not need design judgement.
