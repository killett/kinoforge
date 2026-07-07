"""Tests for _print_instance_overview reconcile + honest labelling."""

from __future__ import annotations

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
