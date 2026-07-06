"""_reconcile_dead_ledger_entries — auto-forget pods the provider confirms gone.

Fixes the 2026-07-06 trap: two 7-day-old ledger entries whose RunPod pods were
long dead kept inflating ``est_spend`` (age×rate) because nothing reconciled the
ledger against live provider state. ``kinoforge list`` now forgets entries the
provider reports as gone (``get_instance`` raises ``KeyError``), while leaving
UNCERTAIN entries (auth/transport errors) and non-cloud providers untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kinoforge.cli._commands import _reconcile_dead_ledger_entries


class _FakeLedger:
    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)


class _FakeProvider:
    """get_instance routes by id prefix: gone-* → KeyError, err-* → error."""

    def get_instance(self, instance_id: str) -> Any:
        if instance_id.startswith("gone"):
            raise KeyError(f"no pod: {instance_id!r}")
        if instance_id.startswith("err"):
            raise RuntimeError("transport blew up")
        return object()  # live


def _runpod_factory() -> Callable[[str], Callable[[], _FakeProvider]]:
    return lambda name: lambda: _FakeProvider()


def test_gone_forgotten_live_and_errors_kept() -> None:
    # Bug caught: without reconciliation a dead pod's entry lingers forever and
    # its est_spend grows without bound. Only KeyError (definitively gone) may
    # trigger a forget; live pods, transient errors, and incomplete rows stay.
    ledger = _FakeLedger()
    entries = [
        {"id": "gone-1", "provider": "runpod"},
        {"id": "live-1", "provider": "runpod"},
        {"id": "err-1", "provider": "runpod"},
        {"id": "", "provider": "runpod"},  # missing id → skipped
        {"id": "x-1"},  # missing provider → skipped
    ]

    gone = _reconcile_dead_ledger_entries(
        ledger, entries, get_provider=_runpod_factory()
    )

    assert gone == ["gone-1"]
    assert ledger.forgotten == ["gone-1"]  # live/error/incomplete all kept


def test_non_cloud_provider_never_reconciled() -> None:
    # Bug caught: a `local` provider's get_instance is in-process — a fresh CLI
    # KeyErrors on a perfectly valid pod. Auto-reconcile must skip it entirely
    # (regression: forgot real `local` ledger rows in list tests).
    ledger = _FakeLedger()
    entries = [{"id": "gone-1", "provider": "local"}]

    gone = _reconcile_dead_ledger_entries(
        ledger, entries, get_provider=_runpod_factory()
    )

    assert gone == []
    assert ledger.forgotten == []


def test_never_raises_on_bad_provider() -> None:
    # Best-effort: an unresolvable provider must not break `list`.
    ledger = _FakeLedger()

    def get_provider(name: str) -> Callable[[], _FakeProvider]:
        raise KeyError(f"unknown provider {name}")

    gone = _reconcile_dead_ledger_entries(
        ledger, [{"id": "gone-1", "provider": "runpod"}], get_provider=get_provider
    )
    assert gone == []
    assert ledger.forgotten == []
