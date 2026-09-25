"""Behavior: ``kinoforge status`` must not call a booting pod's row debris.

U64, second consumer. ``_cmd_status`` resolves a row by calling
``provider.get_instance(entry["id"])``. For a pre-launch provisional row that
id is the CLIENT-side ``run_id`` — the pod NAME on RunPod, the app run id on
Modal — which ``get_instance`` can never resolve. The resulting ``KeyError`` is
therefore GUARANTEED, not diagnostic, yet it was read as proof of staleness:
status printed ``verdict=STALE_LEDGER`` and ``advisory: ledger entry is stale —
run 'kinoforge forget --id <id>'``.

That fires during every normal RunPod cold boot, for the whole multi-minute
window in which the row is the only durable handle on a pod that may already be
billing — and it tells the operator to delete it, which is what compute-seam S5
ruling C1 exists to forbid. ``kinoforge forget`` also matches on id ALONE, so on
the same-key shape (SkyPilot: the cluster name IS the run_id) following that
advice can delete a live cluster's row.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

import pytest

from kinoforge.core.lifecycle import (
    LAUNCH_PHASE_LAUNCHING,
    LAUNCH_PHASE_TAG,
    LAUNCHING_GRACE_S,
)


class _AbsentProvider:
    """A provider that cannot resolve the id — the guaranteed launching case."""

    name = "runpod"

    def get_instance(self, instance_id: str) -> Any:
        raise KeyError(instance_id)


class _FakeLedger:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def entries(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _Ctx:
    cfg = None
    state_dir = None

    def __init__(self, ledger: _FakeLedger) -> None:
        self._ledger = ledger

    def ledger(self) -> _FakeLedger:
        return self._ledger


def _row(*, launching: bool, age_s: float) -> dict[str, Any]:
    tags: dict[str, str] = {"kinoforge_key": "wan-t2v"}
    if launching:
        tags[LAUNCH_PHASE_TAG] = LAUNCH_PHASE_LAUNCHING
    return {
        "id": "upscale-20260923-165600",
        "provider": "runpod",
        "tags": tags,
        "created_at": time.time() - age_s,
        "cost_rate_usd_per_hr": 1.0,
    }


def _run_status(
    row: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    from kinoforge.core import registry

    monkeypatch.setattr(registry, "get_provider", lambda _n: _AbsentProvider)
    from kinoforge.cli._commands import _cmd_status

    rc = _cmd_status(
        argparse.Namespace(id=row["id"], config=None),
        _Ctx(_FakeLedger([row])),  # type: ignore[arg-type]
    )
    return rc, capsys.readouterr().out


def test_status_on_a_booting_row_does_not_call_it_stale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A guaranteed KeyError is not evidence of staleness.

    Bug caught: the KeyError handler treating a launching row like any other
    unresolvable id. On RunPod this verdict is reached on every cold boot —
    the row is young, the pod is very likely alive, and the operator is being
    told the opposite.
    """
    _, out = _run_status(_row(launching=True, age_s=120.0), monkeypatch, capsys)

    assert "STALE_LEDGER" not in out
    assert "not confirmed" in out


def test_status_on_a_booting_row_does_not_advise_forgetting_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The advisory is the actionable half, and it pointed at a destructive op.

    Bug caught: fixing the verdict string while leaving the advisory, so the
    operator is still told to run the one command that deletes the handle —
    and `kinoforge forget` matches on id alone, so on SkyPilot's same-key
    shape it can take a live cluster's row with it.
    """
    _, out = _run_status(_row(launching=True, age_s=120.0), monkeypatch, capsys)

    assert "kinoforge forget" not in out


def test_status_still_reports_a_genuinely_stale_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Negative control — the real stale-ledger signal must survive.

    Bug caught: suppressing the verdict or the advisory for every row rather
    than only phase-tagged ones, which would hide a genuinely dead pod whose
    ``est_spend`` is inflating (the 2026-07-06 ~$210 rows).
    """
    rc, out = _run_status(_row(launching=False, age_s=120.0), monkeypatch, capsys)

    assert rc == 0
    assert "STALE_LEDGER" in out
    assert "kinoforge forget" in out


def test_status_on_an_aged_out_launching_row_does_not_advise_the_bare_forget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Past the grace window the row is debris — but ``forget`` is still id-only.

    Bug caught: falling back to the unmodified stale advisory once the window
    passes. The row is genuinely clearable by then, but the command named is
    still the unscoped one, and ``kinoforge list`` already clears it safely
    via the phase-scoped delete. Point the operator at the safe door.
    """
    _, out = _run_status(
        _row(launching=True, age_s=LAUNCHING_GRACE_S + 60.0), monkeypatch, capsys
    )

    assert "kinoforge list" in out
    assert "kinoforge forget" not in out
