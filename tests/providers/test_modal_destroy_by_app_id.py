"""destroy_instance resolves the identifier Modal actually accepts (U17).

Modal registers an App's NAME only once its deploy completes. A deploy
killed mid-flight (SIGKILL'd `kinoforge provision`) leaves a live app that
is addressable *solely* by its `app_id` — `modal app stop kinoforge-<run_id>`
404s with "No App with name ... found". `destroy_instance` used to build
that name and hand it straight to the stopper, so a mid-deploy kill left
the operator with only the raw provider CLI to recover.

These tests map onto the fix's acceptance criteria:
  1. a mid-deploy app (name unregistered) is stopped BY ITS app_id
  2. a normally-deployed app (no id resolvable) still stops BY NAME
  3. a stopper failure is reported with the app id it found, not raised raw
  4. an unexpected `modal app list --json` shape fails with a message
     naming what could not be parsed, not a raw TypeError/AttributeError

plus the round-2 review findings, which all concern the lookup NOT making a
destroy that used to work worse than it was:
  5. a malformed record EARLIER in the listing than the match must not
     abort the destroy (fail-fast would strand a billing app)
  6. a listing that cannot be read at all must degrade to the name path
  7. a stopped namesake must not be preferred over the live app
  8. `default_stop`'s own CalledProcessError wrapper is exercised

All of them inject `lister=`/`stopper=` (the same seams the existing
`test_provider.py` destroy tests use) or monkeypatch `subprocess.run` — no
`modal` binary, no credentials, no live call.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from kinoforge.core.errors import TeardownError
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.modal._app import default_stop

# The real app_id shape confirmed live during the U7 proof (PROGRESS.md,
# U17 entry): `modal app stop ap-UieraQfT1GhxX3v4etyrEA --yes` -> rc 0.
# `modal/cli/app.py:73-95` accepts `^ap-[a-zA-Z0-9]{22}$` as an id, so every
# synthetic id below is 22 body characters too.
_APP_ID = "ap-UieraQfT1GhxX3v4etyrEA"


def _never_sleep(reason: str) -> Any:  # noqa: ANN401 — test double factory
    """A `sleep` seam that fails the test if the poll loop ever waits."""

    def _sleep(_seconds: float) -> None:
        raise AssertionError(f"should not sleep: {reason}")

    return _sleep


def test_destroy_stops_a_mid_deploy_app_by_its_app_id() -> None:
    """A deploy killed before the name registered is stopped by app_id.

    Bug caught: `destroy_instance` building `kinoforge-<run_id>` and handing
    THAT to the stopper — the exact defect this task fixes. A name-only
    implementation would call the stopper with "kinoforge-run1", not the
    app_id, so `stop_calls == [_APP_ID]` fails against it.

    `lister` is stateful: before the stop call it reports the app stuck at
    "initializing..." (the app_id-resolution scenario this test targets);
    after the app_id-directed stop actually lands, the app disappears from
    the listing (the real-world U17 outcome — PROGRESS.md's live proof was
    `modal app stop ap-... --yes` -> rc 0). Post whole-branch-review Finding
    2, the confirmation poll no longer treats "initializing..." itself as
    proof of success (see test_provider.py::
    test_destroy_keeps_polling_when_app_is_stuck_initializing) — a lister
    that never changed state would now poll all 40 bounded iterations,
    which is correct but orthogonal to what THIS test verifies (the stop
    target). Simulating the stop actually taking effect keeps that
    assertion isolated to app_id resolution.
    """
    records = [
        {
            "app_id": _APP_ID,
            "description": "kinoforge-run1",
            "state": "initializing...",  # never resolved past this (U17 symptom)
            # `modal/cli/app.py:128` emits `str(app_stats.n_running_tasks)`.
            "tasks": "0",
        }
    ]
    stop_calls: list[str] = []
    sleeps: list[float] = []
    provider = ModalProvider(
        lister=lambda: [] if stop_calls else records,
        stopper=lambda name: stop_calls.append(name),
        sleep=sleeps.append,
    )
    # No `_deployments` entry: this is the cross-process recovery case —
    # the destroying process never ran the create_instance that would have
    # cached the name.
    provider.destroy_instance("run1")

    assert sleeps == []  # gone on the very first poll after the stop landed

    assert stop_calls == [_APP_ID]


def test_destroy_falls_back_to_name_when_no_app_id_is_found() -> None:
    """No matching listing record -> stop by name, today's behaviour.

    Bug caught: a fix that ALWAYS requires an app_id match (e.g. raising or
    silently no-op'ing when the listing doesn't name the app) would break
    every normally-deployed destroy — `stop_calls` would be `[]`, not
    `["kinoforge-run2"]`.
    """
    records = [
        # A different app in the same account — proves the match is by
        # description, not "first record wins".
        {"app_id": "ap-someoneElse00000000000", "description": "kinoforge-other"},
    ]
    stop_calls: list[str] = []
    provider = ModalProvider(
        lister=lambda: records,
        stopper=lambda name: stop_calls.append(name),
        sleep=_never_sleep("app absent from the listing"),
    )
    provider._deployments["run2"] = {"url": "u", "name": "kinoforge-run2"}
    provider.destroy_instance("run2")

    assert stop_calls == ["kinoforge-run2"]


def test_destroy_reports_provider_error_with_the_app_id_it_found() -> None:
    """A stopper failure is wrapped with the resolved app_id, not raised raw.

    Bug caught: today's bare `check=True` lets `subprocess.CalledProcessError`
    (or whatever the stopper raises) escape `destroy_instance` unhandled —
    the operator gets a traceback instead of a diagnosable message.

    The injected failure's message is deliberately id-FREE ("boom"): the id
    can only reach `str(excinfo.value)` if the wrapper itself put it there,
    so `raise TeardownError(f"failed to stop app: {exc}")` — a wrapper that
    catches but drops the id — fails this test.
    """
    records = [
        {"app_id": _APP_ID, "description": "kinoforge-run3", "state": "deployed"}
    ]

    def failing_stopper(name: str) -> None:
        raise RuntimeError("boom")

    provider = ModalProvider(lister=lambda: records, stopper=failing_stopper)
    provider._deployments["run3"] = {"url": "u", "name": "kinoforge-run3"}

    with pytest.raises(TeardownError) as excinfo:
        provider.destroy_instance("run3")

    assert _APP_ID in str(excinfo.value)


def test_destroy_does_not_re_wrap_a_teardown_error_from_the_stopper() -> None:
    """An already-diagnosable stopper failure is propagated, not nested.

    Bug caught: a blanket `except Exception` around `self._stopper(...)` also
    catches a `TeardownError` the stopper itself raised, producing the
    doubled "failed to stop modal app ...: failed to stop ..." message an
    operator then has to read twice. The identity assertion is what
    discriminates: a re-wrap builds a NEW TeardownError, so `is` fails even
    though the text would still contain the original.
    """
    records = [
        {"app_id": _APP_ID, "description": "kinoforge-run8", "state": "deployed"}
    ]
    original = TeardownError("modal app stop failed: not authenticated")

    def failing_stopper(name: str) -> None:
        raise original

    provider = ModalProvider(lister=lambda: records, stopper=failing_stopper)
    provider._deployments["run8"] = {"url": "u", "name": "kinoforge-run8"}

    with pytest.raises(TeardownError) as excinfo:
        provider.destroy_instance("run8")

    assert excinfo.value is original


def test_destroy_raises_diagnosable_error_on_malformed_listing() -> None:
    """An unexpected `modal app list --json` shape fails with a named cause.

    Bug caught: `_rec_name`'s `rec.get(...)` (or an unguarded iteration)
    raising a bare `AttributeError`/`TypeError` when a listing entry isn't a
    dict — the exact "raising a parse error" this criterion forbids. The
    fix must convert that into a `TeardownError` whose message says what
    shape it expected, not merely "some exception occurred": this test
    would pass a fix that raised `TeardownError("nope")` unless it also
    checks the message names the malformed part.

    The target app is absent from the listing, so there is nothing to
    resolve AND the listing is bad — the only combination that may abort.
    """
    # One well-formed sibling record (proves a stray malformed entry, not
    # just an empty/absent listing, is what triggers this) plus one entry
    # that isn't a dict at all — e.g. a stray log line mis-parsed as a list
    # item.
    records: list[Any] = [
        {"app_id": "ap-fine000000000000000000", "description": "kinoforge-other"},
        "not-a-record",
    ]
    stop_calls: list[str] = []
    provider = ModalProvider(
        lister=lambda: records,
        stopper=lambda name: stop_calls.append(name),
    )
    provider._deployments["run4"] = {"url": "u", "name": "kinoforge-run4"}

    with pytest.raises(TeardownError) as excinfo:
        provider.destroy_instance("run4")

    message = str(excinfo.value)
    assert "dict" in message  # names the shape it expected, not just "failed"
    assert "str" in message  # names what it actually got
    # The malformed listing must abort BEFORE any stop attempt — a fix that
    # stops first and then chokes on the poll-loop listing would still
    # raise, but would also have already called the stopper.
    assert stop_calls == []


def test_destroy_stops_by_app_id_despite_an_earlier_malformed_record() -> None:
    """A bad record BEFORE the match must not strand a resolvable app.

    Bug caught: `_find_app_id` raising `TeardownError` on the first non-dict
    entry it meets. With the stray entry ordered first, that fail-fast scan
    never reaches the matching record, so the destroy aborts with NO stop
    attempt at all — worse than the pre-U17 by-name path, and the app keeps
    billing. Against fail-fast this reports `TeardownError` instead of
    `stop_calls == [_APP_ID]`.

    `lister` is stateful, same rationale as
    `test_destroy_stops_a_mid_deploy_app_by_its_app_id` above: the app
    disappears once the app_id-directed stop actually lands, so the
    (correct, post whole-branch-review Finding 2) confirmation poll
    resolves on the first post-stop check rather than exhausting all 40
    bounded iterations for an unrelated reason.
    """
    records: list[Any] = [
        "stray log line on stdout",  # malformed, and FIRST
        {
            "app_id": _APP_ID,
            "description": "kinoforge-run5",
            "state": "initializing...",
            "tasks": "0",
        },
    ]
    stop_calls: list[str] = []
    sleeps: list[float] = []
    provider = ModalProvider(
        lister=lambda: [] if stop_calls else records,
        stopper=lambda name: stop_calls.append(name),
        sleep=sleeps.append,
    )

    provider.destroy_instance("run5")

    assert stop_calls == [_APP_ID]
    assert sleeps == []


def test_destroy_falls_back_to_name_when_the_listing_cannot_be_read() -> None:
    """A listing that cannot be read degrades to the by-name stop.

    Bug caught: the U17 lookup sits on the critical path BEFORE the stop, so
    an unguarded `self._lister()` turns an absent `modal` binary
    (`FileNotFoundError`), an unauthenticated CLI (`CalledProcessError`) or
    a `json.loads` failure into an aborted destroy — a raw traceback, and a
    live app left billing, where pre-U17 the by-name stop simply worked.
    Against an unguarded lookup this test errors with `FileNotFoundError`
    rather than asserting; against a lookup that re-raises as
    `TeardownError` without falling back it fails with `TeardownError` and
    `stop_calls == []`.
    """

    def exploding_lister() -> list[dict[str, Any]]:
        raise FileNotFoundError("[Errno 2] No such file or directory: 'modal'")

    stop_calls: list[str] = []
    provider = ModalProvider(
        lister=exploding_lister,
        stopper=lambda name: stop_calls.append(name),
        sleep=_never_sleep("liveness cannot be polled without a listing"),
    )
    provider._deployments["run6"] = {"url": "u", "name": "kinoforge-run6"}

    provider.destroy_instance("run6")

    assert stop_calls == ["kinoforge-run6"]


def test_destroy_prefers_the_live_record_over_a_stopped_namesake() -> None:
    """Two records share a description -> the one still running is stopped.

    `modal app list` deliberately includes recently-stopped apps, so a reused
    or redeployed `kinoforge-<run_id>` description can appear twice. Modal's
    own by-name resolution prefers the currently-deployed app
    (`modal/cli/app.py:84-91`).

    Bug caught: a state-blind "first description match wins" scan. With the
    stopped record ordered first it returns the stopped id;
    `modal app stop <stopped id>` then exits non-zero
    (`modal/cli/app.py:513-520`, "App is already stopped.") and the LIVE app
    keeps billing. Against first-match-wins this asserts
    `["ap-stopped000000000000000"] == ["ap-live000000000000000000"]`.
    """
    live_id = "ap-live000000000000000000"
    records: list[dict[str, Any]] = [
        {
            "app_id": "ap-stopped000000000000000",
            "description": "kinoforge-run7",
            "state": "stopped",
            "tasks": "0",
        },
        {
            "app_id": live_id,
            "description": "kinoforge-run7",
            "state": "deployed",
            "tasks": "1",
        },
    ]
    stop_calls: list[str] = []
    sleeps: list[float] = []

    def stopper(app_id: str) -> None:
        """Record the call and reflect the stop back into the listing."""
        stop_calls.append(app_id)
        for rec in records:
            if rec["app_id"] == app_id:
                rec["state"] = "stopped"

    provider = ModalProvider(
        lister=lambda: records,
        stopper=stopper,
        sleep=lambda s: sleeps.append(s),
    )

    provider.destroy_instance("run7")

    assert stop_calls == [live_id]
    # Having stopped the right one, the poll loop sees no active namesake
    # and converges immediately; picking the stopped id would leave the live
    # record 'deployed' and burn the whole bounded poll budget.
    assert sleeps == []


def test_default_stop_wraps_a_nonzero_exit_with_the_identifier_and_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`default_stop` reports a CLI failure, it does not leak the raw error.

    Bug caught: dropping the `try/except` around `subprocess.run(...,
    check=True)` — a `modal app stop` that exits non-zero (already stopped,
    unauthenticated, unknown identifier) then reaches the operator as a bare
    `subprocess.CalledProcessError` traceback, which is half of the "no
    unhandled CalledProcessError" acceptance criterion. The exit code and
    the identifier are what make the message diagnosable, so both are
    asserted; `pytest.raises(RuntimeError)` alone would also match a leaked
    `CalledProcessError` subclass, hence the explicit not-isinstance check.
    """

    def fake_run(argv: list[str], **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(returncode=3, cmd=argv)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        default_stop(_APP_ID)

    assert not isinstance(excinfo.value, subprocess.CalledProcessError)
    message = str(excinfo.value)
    assert _APP_ID in message
    # Bug caught (whole-branch review Finding 4): "3" in message is vacuous
    # — _APP_ID ("ap-UieraQfT1GhxX3v4etyrEA") already contains a "3"
    # independent of the exit code, so a `default_stop` that dropped the
    # exit code from the message entirely would still pass this assertion.
    # Pin the actual exit-code substring instead.
    assert "exit 3" in message


def test_default_stop_invokes_the_cli_with_the_identifier_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw `ap-...` id reaches `modal app stop` unaltered, with `--yes`.

    Bug caught: a `default_stop` that re-derives or decorates the identifier
    (e.g. prefixing `kinoforge-`) would defeat the whole U17 fix — the
    caller resolved the id precisely because the name is unresolvable. A
    dropped `--yes` is the other regression: the CLI then blocks on a
    confirmation prompt with no tty and only surfaces at the 120 s timeout.
    Expected argv is the invocation recorded live in the task brief
    (`modal app stop ap-UieraQfT1GhxX3v4etyrEA --yes` -> rc 0).
    """
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> None:
        seen.append(argv)

    monkeypatch.setattr(subprocess, "run", fake_run)

    default_stop(_APP_ID)

    assert seen == [["modal", "app", "stop", _APP_ID, "--yes"]]
