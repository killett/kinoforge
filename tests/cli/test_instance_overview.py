"""Tests for _print_instance_overview reconcile + honest labelling."""

from __future__ import annotations

import argparse
import io
import time
from typing import Any

import pytest

from kinoforge.cli import _main

_Row = dict[str, Any]


class _FakeLedger:
    """In-memory ledger stub exposing entries()/forget()."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = list(rows)

    def entries(self) -> list[_Row]:
        return list(self._rows)

    def forget(self, iid: str) -> None:
        self._rows = [r for r in self._rows if str(r.get("id")) != iid]


class _Ctx:
    def __init__(self, ledger: _FakeLedger) -> None:
        self._ledger = ledger

    def ledger_safe(self) -> tuple[_FakeLedger, None]:
        return self._ledger, None


def _run(
    ctx: Any,
    monkeypatch: pytest.MonkeyPatch,
    resolver: Any,
) -> str:
    """Invoke the overview with an injected provider resolver, capture stdout."""
    monkeypatch.setattr(_main, "_overview_get_provider", resolver, raising=False)
    buf = io.StringIO()
    _main._print_instance_overview(ctx, file=buf)
    return buf.getvalue()


def test_young_entry_is_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row younger than max_age_s must never hit the provider.

    Bug caught: an unconditional reconcile that probes every row would add a
    network round-trip to the hot warm-reuse path and call the resolver here.
    """
    now = time.time()
    ledger = _FakeLedger(
        [
            {
                "id": "young1",
                "provider": "runpod",
                "created_at": now - 60,
                "max_age_s": 3600,
                "cost_rate_usd_per_hr": 1.0,
            }
        ]
    )
    called: list[str] = []

    def resolver(name: str) -> Any:
        called.append(name)
        raise AssertionError("young row must not be probed")

    out = _run(_Ctx(ledger), monkeypatch, resolver)
    assert called == []
    assert "young1" in out


def test_suspect_gone_entry_is_forgotten_and_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suspect row the provider 404s on is forgotten and not printed.

    Bug caught: overview prints a dead pod's inflating est_spend forever.
    """
    now = time.time()
    ledger = _FakeLedger(
        [
            {
                "id": "ghost1",
                "provider": "runpod",
                "created_at": now - 200 * 3600,
                "max_age_s": 3600,
                "cost_rate_usd_per_hr": 1.19,
            }
        ]
    )

    class _GoneProvider:
        def get_instance(self, iid: str) -> Any:
            raise KeyError(iid)

    out = _run(_Ctx(ledger), monkeypatch, lambda name: lambda: _GoneProvider())
    assert "ghost1" not in out
    assert "No running instances" in out


def test_reconcile_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver/network explosion must not crash the overview.

    Bug caught: a bare provider error at the top of every command aborts the
    whole CLI invocation instead of degrading to a printed row.
    """
    now = time.time()
    ledger = _FakeLedger(
        [
            {
                "id": "ghost2",
                "provider": "runpod",
                "created_at": now - 200 * 3600,
                "max_age_s": 3600,
                "cost_rate_usd_per_hr": 1.0,
            }
        ]
    )

    def boom(name: str) -> Any:
        raise RuntimeError("network down")

    out = _run(_Ctx(ledger), monkeypatch, boom)
    assert "ghost2" in out  # kept, printed, no crash


def test_est_spend_is_labelled_as_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spend figure prints as an explicit upper-bound estimate.

    Bug caught: a bare 'est_spend=$225' reads as a real charge and alarms the
    operator, which is the whole defect this feature fixes.
    """
    now = time.time()
    ledger = _FakeLedger(
        [
            {
                "id": "live1",
                "provider": "runpod",
                "created_at": now - 60,
                "max_age_s": 3600,
                "cost_rate_usd_per_hr": 1.0,
            }
        ]
    )

    def resolver(name: str) -> Any:
        raise AssertionError("young row must not be probed")

    out = _run(_Ctx(ledger), monkeypatch, resolver)
    assert "est" in out
    assert "$0 if pod" in out  # honest caveat present


def test_offline_suspect_row_marked_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suspect row that reconcile could not confirm gone is flagged.

    Bug caught: with the provider unreachable, a real ghost is printed with a
    confident number and no hint that it may be dead.
    """
    now = time.time()
    ledger = _FakeLedger(
        [
            {
                "id": "maybe_ghost",
                "provider": "runpod",
                "created_at": now - 200 * 3600,
                "max_age_s": 3600,
                "cost_rate_usd_per_hr": 1.0,
            }
        ]
    )

    def boom(name: str) -> Any:
        raise RuntimeError("network down")

    out = _run(_Ctx(ledger), monkeypatch, boom)
    assert "maybe_ghost" in out
    assert "unverified" in out


def test_young_row_not_marked_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A young/live row must NOT carry the unverified marker.

    Bug caught: marking every row unverified would defeat the signal — the
    marker must fire only for suspect rows that survived reconcile.
    """
    now = time.time()
    ledger = _FakeLedger(
        [
            {
                "id": "young1",
                "provider": "runpod",
                "created_at": now - 60,
                "max_age_s": 3600,
                "cost_rate_usd_per_hr": 1.0,
            }
        ]
    )

    def resolver(name: str) -> Any:
        raise AssertionError("young row must not be probed")

    out = _run(_Ctx(ledger), monkeypatch, resolver)
    assert "young1" in out
    assert "unverified" not in out


# ---------------------------------------------------------------------------
# U61 — a `launching` row must not read as a confirmed pod.
#
# Filed 2026-09-24 off a live reproduction: an over-ceiling create was refused
# with a raw HTTP 500 and booked NO hardware, yet `kinoforge list` afterwards
# showed row `upscale-20260923-165600` indistinguishable from a real pod
# (`tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt`).
#
# The row is KEPT on purpose — compute-seam S5 ruling C1, and
# `RunPodProvider.nothing_booked_errors()` returns `()` naming this exact
# HTTP-500 shape: a transport failure reading the create response cannot be
# told from one sending it, so a pod may exist behind it. Deleting the row on
# every raise is the F12 orphan hole in reverse ("$210 phantom pod").
#
# What is actually broken is the DISPLAY. CLAUDE.md's live-smoke teardown rule
# makes `kinoforge list` the authoritative proof a run left nothing billing;
# an unlabelled provisional row means an operator cannot tell a phantom from a
# real leak, and either wastes time chasing it or learns to discount the check.
# ---------------------------------------------------------------------------

_LAUNCHING_TAGS = {"kf_launch_phase": "launching"}


def test_overview_labels_a_launching_row_as_not_yet_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-launch provisional row is marked, not printed as a live pod.

    Bug caught: reading the phase at the entry's top level instead of under
    ``tags`` (where the orchestrator writes it, and where
    ``_reconcile._is_launching`` reads it) leaves the marker off every real
    provisional row — reproducing the live symptom exactly.
    """
    now = time.time()
    ledger = _FakeLedger(
        [
            {
                "id": "upscale-20260923-165600",
                "provider": "runpod",
                "created_at": now - 9,
                "max_age_s": 3600,
                "cost_rate_usd_per_hr": 0.0,
                "tags": dict(_LAUNCHING_TAGS),
            }
        ]
    )

    out = _run(_Ctx(ledger), monkeypatch, _no_probe_resolver())

    assert "upscale-20260923-165600" in out
    assert "not confirmed" in out


def test_overview_does_not_label_a_confirmed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real instance row carries no provisional marker.

    Bug caught: an always-true predicate (e.g. testing ``entry.get("tags")``
    for truthiness) would stamp every live pod as unconfirmed, training the
    operator to ignore the one marker that matters.
    """
    now = time.time()
    ledger = _FakeLedger(
        [
            {
                "id": "realpod1",
                "provider": "runpod",
                "created_at": now - 60,
                "max_age_s": 3600,
                "cost_rate_usd_per_hr": 1.0,
                "tags": {"kinoforge_key": "wan-t2v", "kf_launch_phase": "running"},
            }
        ]
    )

    out = _run(_Ctx(ledger), monkeypatch, _no_probe_resolver())

    assert "realpod1" in out
    assert "not confirmed" not in out


def _no_probe_resolver() -> Any:
    """Resolver that fails loudly — these rows are young and must not be probed."""

    def resolver(name: str) -> Any:
        raise AssertionError(f"young row must not be probed (provider {name})")

    return resolver


def test_cmd_list_labels_a_launching_row_as_not_yet_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """U61. ``kinoforge list`` is the command CLAUDE.md's teardown rule names.

    Bug caught: labelling only the top-of-command overview and leaving
    ``_cmd_list`` alone. The teardown rule sends the operator to
    ``kinoforge list`` specifically, and that is the printer the live
    reproduction captured — so an unlabelled row there is the whole defect,
    fixed nowhere.
    """
    from kinoforge.cli import _commands

    ledger = _FakeLedger(
        [
            {
                "id": "upscale-20260923-165600",
                "provider": "runpod",
                "tags": dict(_LAUNCHING_TAGS),
            }
        ]
    )

    class _ListCtx:
        def ledger(self) -> _FakeLedger:
            return ledger

    monkeypatch.setattr(_commands, "_reconcile_dead_ledger_entries", lambda *a, **k: [])
    rc = _commands._cmd_list(argparse.Namespace(), _ListCtx())  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert rc == 0
    assert "upscale-20260923-165600" in out
    assert "not confirmed" in out


def test_cmd_list_does_not_label_a_confirmed_row(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """U61 negative control for the ``list`` printer.

    Bug caught: a marker stamped on every row, which makes the signal
    worthless in exactly the check it was added to serve.
    """
    from kinoforge.cli import _commands

    ledger = _FakeLedger(
        [
            {
                "id": "realpod1",
                "provider": "runpod",
                # Carries the phase tag with a NON-launching value on purpose:
                # a predicate testing `"kf_launch_phase" in tags` rather than
                # its value passes a control that merely omits the tag.
                "tags": {"kinoforge_key": "wan-t2v", "kf_launch_phase": "running"},
            }
        ]
    )

    class _ListCtx:
        def ledger(self) -> _FakeLedger:
            return ledger

    monkeypatch.setattr(_commands, "_reconcile_dead_ledger_entries", lambda *a, **k: [])
    _commands._cmd_list(argparse.Namespace(), _ListCtx())  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "realpod1" in out
    assert "not confirmed" not in out


def test_overview_launching_marker_wins_over_the_unverified_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U61. An AGED launching row reads as launching, not merely unverified.

    Bug caught: ordering the two branches the other way. A launching row old
    enough to be suspect would then print ``⚠ unverified — run 'kinoforge
    list'`` and send the operator to a printer that (before this fix) told
    them nothing new — while suppressing the one fact that explains the row.
    "Which pod is this?" has to be answered before "is its est_spend real?".
    """
    now = time.time()
    ledger = _FakeLedger(
        [
            {
                "id": "aged-launching",
                "provider": "runpod",
                "created_at": now - 7200,
                "max_age_s": 3600,
                "cost_rate_usd_per_hr": 1.0,
                "tags": dict(_LAUNCHING_TAGS),
            }
        ]
    )

    # Suspect, so it IS reconciled — an uncertain probe keeps the row.
    def resolver(name: str) -> Any:  # noqa: ARG001
        raise RuntimeError("transport uncertain")

    out = _run(_Ctx(ledger), monkeypatch, resolver)

    assert "launching" in out
    assert "unverified" not in out


def test_cmd_list_prints_the_launching_note_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """U61. Two launching rows get one explanation, not two.

    Bug caught: printing the note inside the row loop. The note is nine lines
    of prose about a destructive command; repeated per row it buries the very
    row listing an operator ran ``kinoforge list`` to read.
    """
    from kinoforge.cli import _commands

    ledger = _FakeLedger(
        [
            {"id": "launch-a", "provider": "runpod", "tags": dict(_LAUNCHING_TAGS)},
            {"id": "launch-b", "provider": "runpod", "tags": dict(_LAUNCHING_TAGS)},
        ]
    )

    class _ListCtx:
        def ledger(self) -> _FakeLedger:
            return ledger

    monkeypatch.setattr(_commands, "_reconcile_dead_ledger_entries", lambda *a, **k: [])
    _commands._cmd_list(argparse.Namespace(), _ListCtx())  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert out.count("pre-launch placeholder") == 1


def test_cmd_list_omits_the_launching_note_when_no_row_is_launching(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """U61. The note is absent from an ordinary listing.

    Bug caught: printing it unconditionally. Then every routine ``kinoforge
    list`` carries a warning about deleting live resources, which is how a
    real warning stops being read.
    """
    from kinoforge.cli import _commands

    ledger = _FakeLedger(
        [{"id": "realpod1", "provider": "runpod", "tags": {"kinoforge_key": "k"}}]
    )

    class _ListCtx:
        def ledger(self) -> _FakeLedger:
            return ledger

    monkeypatch.setattr(_commands, "_reconcile_dead_ledger_entries", lambda *a, **k: [])
    _commands._cmd_list(argparse.Namespace(), _ListCtx())  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "pre-launch placeholder" not in out


def test_the_launching_note_does_not_recommend_an_unscoped_delete(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """U61 review finding #1. ``kinoforge forget`` matches on id ALONE.

    ``_cmd_forget`` calls ``Ledger.forget(id)``, not the phase-scoped
    ``forget_provisional``. On the same-key shape (SkyPilot: the cluster name
    IS the run_id) a real row can sit under the same id as the launching one
    — ``_collapse_provisional_row`` logs that outcome as reachable — so an
    unqualified "run kinoforge forget" deletes a live cluster's only handle.

    Bug caught: a note that names ``forget`` without the id-scoping warning,
    i.e. re-introducing as prose the hazard ``forget_provisional`` exists to
    prevent.
    """
    from kinoforge.cli import _commands

    ledger = _FakeLedger(
        [{"id": "launch-a", "provider": "runpod", "tags": dict(_LAUNCHING_TAGS)}]
    )

    class _ListCtx:
        def ledger(self) -> _FakeLedger:
            return ledger

    monkeypatch.setattr(_commands, "_reconcile_dead_ledger_entries", lambda *a, **k: [])
    _commands._cmd_list(argparse.Namespace(), _ListCtx())  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "kinoforge forget" in out
    assert "id ALONE" in out
    assert "ages out" in out.lower()
