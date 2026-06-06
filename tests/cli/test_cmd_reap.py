"""Layer V T6: CLI `kinoforge reap` integration tests.

Covers AC13–AC16 of spec §4.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.cli._commands import _cmd_reap

if TYPE_CHECKING:
    from kinoforge.cli.context import SessionContext


def _args(**overrides: Any) -> argparse.Namespace:
    """Default flags = dry-run, no opts."""
    base = dict(
        apply=False,
        include_orphans=False,
        force_forget=False,
        strict=False,
        id=None,
        format="human",
        config=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class _FakeCtx:
    """Minimal SessionContext stand-in for CLI tests."""

    def __init__(self, entries: list[dict[str, Any]], cfg: Any = None) -> None:
        self._entries = entries
        self.cfg = cfg
        self._ledger = MagicMock()
        self._ledger.entries.return_value = entries
        self._ledger.forget = MagicMock()
        self._store = MagicMock()

        # acquire_lock returns a context manager
        class _L:
            def __enter__(self) -> _L:
                return self

            def __exit__(self, *_: object) -> None:
                return None

        self._store.acquire_lock = MagicMock(return_value=_L())

    def ledger(self) -> MagicMock:
        """Return the fake ledger."""
        return self._ledger

    def store(self) -> MagicMock:
        """Return the fake store."""
        return self._store


def _ctx(entries: list[dict[str, Any]], cfg: Any = None) -> SessionContext:
    """Build a typed _FakeCtx cast to SessionContext."""
    return cast("SessionContext", _FakeCtx(entries, cfg))


# ---------------------------------------------------------------------------
# Dry-run default
# ---------------------------------------------------------------------------


def test_reap_dry_run_default_does_not_destroy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --apply → no destructive calls; sweep called with policy=None."""
    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        from kinoforge.core.reaper_actor import SweepReport

        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        code = _cmd_reap(_args(), ctx)
    assert code == 0
    # sweep called with policy=None for dry-run
    assert mock_sweep.call_args.kwargs["policy"] is None


def test_reap_empty_ledger_prints_message_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty ledger → exit 0 with informational message."""
    ctx = _ctx([])
    code = _cmd_reap(_args(), ctx)
    out = capsys.readouterr().out + capsys.readouterr().err
    assert code == 0
    assert "empty" in out.lower() or "no" in out.lower()


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------


def test_reap_include_orphans_without_apply_exit_4(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--include-orphans without --apply → exit 4 + stderr."""
    ctx = _ctx([])
    code = _cmd_reap(_args(include_orphans=True), ctx)
    err = capsys.readouterr().err
    assert code == 4
    assert "--apply" in err


def test_reap_force_forget_without_apply_exit_4() -> None:
    """--force-forget without --apply → exit 4."""
    ctx = _ctx([])
    code = _cmd_reap(_args(force_forget=True), ctx)
    assert code == 4


# ---------------------------------------------------------------------------
# --apply path
# ---------------------------------------------------------------------------


def test_reap_apply_routes_default_policy_to_sweep() -> None:
    """sweep is called with DEFAULT_APPLY_POLICY when --apply set."""
    from kinoforge.core.reaper import DEFAULT_APPLY_POLICY
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        _cmd_reap(_args(apply=True), ctx)
    assert (
        mock_sweep.call_args.kwargs["policy"].act_verdicts
        == DEFAULT_APPLY_POLICY.act_verdicts
    )


def test_reap_apply_include_orphans_extends_policy() -> None:
    """--include-orphans with --apply adds ORPHAN_REAP to policy."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        _cmd_reap(_args(apply=True, include_orphans=True), ctx)
    assert Verdict.ORPHAN_REAP in mock_sweep.call_args.kwargs["policy"].act_verdicts


# ---------------------------------------------------------------------------
# --strict
# ---------------------------------------------------------------------------


def test_reap_strict_with_unroutable_present_exits_3() -> None:
    """--strict with UNROUTABLE verdict present → exit 3."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "broken"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "broken"}, Verdict.UNROUTABLE)}
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=[])
        code = _cmd_reap(_args(strict=True), ctx)
    assert code == 3


def test_reap_strict_no_uncertainty_exits_0() -> None:
    """--strict with only LIVE verdicts → exit 0."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "fake"}, Verdict.LIVE)}
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=[])
        code = _cmd_reap(_args(strict=True), ctx)
    assert code == 0


# ---------------------------------------------------------------------------
# --format json
# ---------------------------------------------------------------------------


def test_reap_format_json_emits_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """--format json emits valid JSONL (one record per line)."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    snapshot = {
        "i-1": ({"id": "i-1", "provider": "fake", "created_at": 0.0}, Verdict.LIVE)
    }
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=[])
        code = _cmd_reap(_args(format="json"), ctx)

    out = capsys.readouterr().out.strip().splitlines()
    # Every line must be parseable JSON.
    for line in out:
        json.loads(line)
    assert code == 0


# ---------------------------------------------------------------------------
# action="failed" → exit 2
# ---------------------------------------------------------------------------


def test_reap_apply_with_failed_action_exits_2() -> None:
    """One action=failed under --apply → exit 2."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import ActionResult, SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "fake"}, Verdict.IDLE_REAP)}
    actions = [
        ActionResult(
            instance_id="i-1",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="failed",
            reason="simulated",
        )
    ]
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=actions)
        code = _cmd_reap(_args(apply=True), ctx)
    assert code == 2
