"""destroy_instance resolves the identifier Modal actually accepts (U17).

Modal registers an App's NAME only once its deploy completes. A deploy
killed mid-flight (SIGKILL'd `kinoforge provision`) leaves a live app that
is addressable *solely* by its `app_id` — `modal app stop kinoforge-<run_id>`
404s with "No App with name ... found". `destroy_instance` used to build
that name and hand it straight to the stopper, so a mid-deploy kill left
the operator with only the raw provider CLI to recover.

These four tests map 1:1 to the fix's acceptance criteria:
  1. a mid-deploy app (name unregistered) is stopped BY ITS app_id
  2. a normally-deployed app (no id resolvable) still stops BY NAME
  3. a stopper failure is reported with the app id it found, not raised raw
  4. an unexpected `modal app list --json` shape fails with a message
     naming what could not be parsed, not a raw TypeError/AttributeError

All four inject `lister=`/`stopper=` (the same seams the existing
`test_provider.py` destroy tests use) — no subprocess, no `modal` binary,
no credentials, no live call.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import TeardownError
from kinoforge.providers.modal import ModalProvider

# The real app_id shape confirmed live during the U7 proof (PROGRESS.md,
# U17 entry): `modal app stop ap-UieraQfT1GhxX3v4etyrEA --yes` -> rc 0.
_APP_ID = "ap-UieraQfT1GhxX3v4etyrEA"


def test_destroy_stops_a_mid_deploy_app_by_its_app_id() -> None:
    """A deploy killed before the name registered is stopped by app_id.

    Bug caught: `destroy_instance` building `kinoforge-<run_id>` and handing
    THAT to the stopper — the exact defect this task fixes. A name-only
    implementation would call the stopper with "kinoforge-run1", not the
    app_id, so `stop_calls == [_APP_ID]` fails against it.
    """
    records = [
        {
            "app_id": _APP_ID,
            "description": "kinoforge-run1",
            "state": "initializing...",  # never resolved past this (U17 symptom)
            "tasks": 0,
        }
    ]
    stop_calls: list[str] = []
    provider = ModalProvider(
        lister=lambda: records,
        stopper=lambda name: stop_calls.append(name),
        sleep=lambda s: (_ for _ in ()).throw(
            AssertionError("should not sleep: app is not 'deployed'/'running'")
        ),
    )
    # No `_deployments` entry: this is the cross-process recovery case —
    # the destroying process never ran the create_instance that would have
    # cached the name.
    provider.destroy_instance("run1")

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
        sleep=lambda s: (_ for _ in ()).throw(
            AssertionError("should not sleep: app absent from the listing")
        ),
    )
    provider._deployments["run2"] = {"url": "u", "name": "kinoforge-run2"}
    provider.destroy_instance("run2")

    assert stop_calls == ["kinoforge-run2"]


def test_destroy_reports_provider_error_with_the_app_id_it_found() -> None:
    """A stopper failure is wrapped with the resolved app_id, not raised raw.

    Bug caught: today's bare `check=True` lets `subprocess.CalledProcessError`
    (or whatever the stopper raises) escape `destroy_instance` unhandled —
    the operator gets a traceback instead of a diagnosable message. A fix
    that catches the error but drops the id (e.g. "failed to stop app")
    would still fail the `_APP_ID in str(...)` assertion below.
    """
    records = [
        {"app_id": _APP_ID, "description": "kinoforge-run3", "state": "deployed"}
    ]

    def failing_stopper(name: str) -> None:
        raise RuntimeError(f"modal app stop {name!r} failed (exit 1)")

    provider = ModalProvider(lister=lambda: records, stopper=failing_stopper)
    provider._deployments["run3"] = {"url": "u", "name": "kinoforge-run3"}

    with pytest.raises(TeardownError) as excinfo:
        provider.destroy_instance("run3")

    assert _APP_ID in str(excinfo.value)


def test_destroy_raises_diagnosable_error_on_malformed_listing() -> None:
    """An unexpected `modal app list --json` shape fails with a named cause.

    Bug caught: `_rec_name`'s `rec.get(...)` (or an unguarded iteration)
    raising a bare `AttributeError`/`TypeError` when a listing entry isn't a
    dict — the exact "raising a parse error" this criterion forbids. The
    fix must convert that into a `TeardownError` whose message says what
    shape it expected, not merely "some exception occurred": this test
    would pass a fix that raised `TeardownError("nope")` unless it also
    checks the message names the malformed part.
    """
    # One well-formed sibling record (proves a stray malformed entry, not
    # just an empty/absent listing, is what triggers this) plus one entry
    # that isn't a dict at all — e.g. a stray log line mis-parsed as a list
    # item.
    records: list[Any] = [
        {"app_id": "ap-fine0000000000000000", "description": "kinoforge-other"},
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
