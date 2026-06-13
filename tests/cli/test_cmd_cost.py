"""Tests for the B2 ``kinoforge cost`` CLI subcommand."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.cli._commands import _cmd_cost
from kinoforge.cli.context import SessionContext
from kinoforge.core.balance_endpoints import TransportError
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture()
def fake_ctx(tmp_path: Path) -> tuple[Any, Any]:
    """SessionContext stub with stub cfg + in-memory store; ledger patched per test."""
    store = LocalArtifactStore(tmp_path / "store")
    cfg = MagicMock()
    cfg.compute.provider = "runpod"
    cfg.engine.kind = "comfyui"
    # cfg.lifecycle() is a method returning an InterfaceLifecycle-like object
    cfg.lifecycle.return_value.idle_timeout_s = 600.0
    cfg.lifecycle.return_value.max_lifetime_s = 3600.0
    cfg.lifecycle.return_value.heartbeat_interval_s = None
    cfg.lifecycle.return_value.grace_after_session_s = 300.0

    ctx = MagicMock(spec=SessionContext)
    ctx.cfg = cfg
    ctx.state_dir = tmp_path
    ctx.store.return_value = store
    return ctx, store


def _args(**overrides: Any) -> argparse.Namespace:
    return argparse.Namespace(
        json=overrides.get("json", False),
        prom=overrides.get("prom", False),
        no_cache=overrides.get("no_cache", True),
        cache_ttl=overrides.get("cache_ttl", 15.0),
    )


def _ledger_entry(
    *,
    instance_id: str = "pod-abc",
    provider: str = "runpod",
    rate: float = 0.79,
    hours_ago: float = 1.0,
) -> dict[str, Any]:
    return {
        "id": instance_id,
        "provider": provider,
        "cost_rate_usd_per_hr": rate,
        "created_at": (datetime.now() - timedelta(hours=hours_ago)).timestamp(),
        "tags": {},
    }


def test_empty_ledger_human_table(
    fake_ctx: tuple[Any, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, _ = fake_ctx
    ctx.ledger.return_value.entries.return_value = []
    rc = _cmd_cost(_args(), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Burn rate" in out
    assert "0.00" in out


def test_json_mode_emits_stable_schema(
    fake_ctx: tuple[Any, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, _ = fake_ctx
    ctx.ledger.return_value.entries.return_value = []
    rc = _cmd_cost(_args(json=True), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    for key in (
        "as_of",
        "burn_rate_usd_per_hr",
        "per_provider",
        "balance",
        "balance_errors",
        "heartbeat_partial_truth",
        "hosted_spend_pending",
        "throttle_warnings",
    ):
        assert key in payload, f"missing key {key!r}"
    assert payload["hosted_spend_pending"] is True
    assert payload["throttle_warnings"] == []


def test_json_and_prom_mutex() -> None:
    """BUG CATCH: argparse MUST reject --json + --prom together at parse time."""
    from kinoforge.cli._main import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["cost", "--json", "--prom"])
    assert exc.value.code == 2


def test_prom_mode_emits_all_gauges_and_help(
    fake_ctx: tuple[Any, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """All 5 gauges + 1 counter present with HELP+TYPE lines; 8 Verdict labels emitted."""
    ctx, _ = fake_ctx
    entry = _ledger_entry(hours_ago=2.0)
    ctx.ledger.return_value.entries.return_value = [entry]
    with patch("kinoforge.core.registry.get_provider") as get_prov:
        prov_inst = MagicMock()
        live_inst = MagicMock()
        live_inst.id = "pod-abc"
        prov_inst.list_instances.return_value = [live_inst]
        get_prov.return_value = lambda: prov_inst
        rc = _cmd_cost(_args(prom=True), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    for metric in (
        "kinoforge_burn_rate_usd_per_hr",
        "kinoforge_balance_usd",
        "kinoforge_balance_as_of_seconds",
        "kinoforge_pod_count",
        "kinoforge_spend_usd_total",
        "kinoforge_cost_scrape_errors_total",
    ):
        assert f"# HELP {metric} " in out, f"missing HELP for {metric}"
        assert f"# TYPE {metric} " in out, f"missing TYPE for {metric}"
    for verdict in (
        "LIVE",
        "IDLE_REAP",
        "OVERAGE_REAP",
        "ORPHAN_REAP",
        "STALE_LEDGER",
        "HEARTBEAT_UNKNOWN",
        "HEARTBEAT_SUBSTRATE_MISSING",
        "UNROUTABLE",
    ):
        assert f'verdict="{verdict}"' in out


def test_balance_failure_does_not_block_burn_render(
    fake_ctx: tuple[Any, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Critical invariant per spec §12: transport / schema / cred failures
    NEVER raise from the render path; burn rate still renders from ledger."""
    ctx, _ = fake_ctx
    entry = _ledger_entry()
    ctx.ledger.return_value.entries.return_value = [entry]
    with patch("kinoforge.core.registry.get_provider") as get_prov:
        prov_inst = MagicMock()
        live_inst = MagicMock()
        live_inst.id = "pod-abc"
        prov_inst.list_instances.return_value = [live_inst]
        get_prov.return_value = lambda: prov_inst
        with patch("kinoforge._adapters.build_balance_endpoint_for") as build_bal:
            ep = MagicMock()
            ep.read.side_effect = TransportError("simulated")
            build_bal.return_value = ep
            rc = _cmd_cost(_args(), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    assert "transport" in out.lower()
    assert "0.79" in out


def test_list_instances_failure_fallback(
    fake_ctx: tuple[Any, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """When provider.list_instances raises, fall back to assume-up; do NOT raise."""
    ctx, _ = fake_ctx
    entry = _ledger_entry()
    ctx.ledger.return_value.entries.return_value = [entry]
    with patch("kinoforge.core.registry.get_provider") as get_prov:
        prov_inst = MagicMock()
        prov_inst.list_instances.side_effect = RuntimeError("provider broken")
        get_prov.return_value = lambda: prov_inst
        with caplog.at_level("WARNING"):
            rc = _cmd_cost(_args(), ctx)
    assert rc == 0
    assert any(
        "list_instances" in rec.message or "provider broken" in rec.message
        for rec in caplog.records
    )


def test_replicate_throttle_stub_footer(
    fake_ctx: tuple[Any, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-var set, zero Replicate ledger entries → empty throttle_warnings."""
    monkeypatch.setenv("KINOFORGE_REPLICATE_THROTTLE_AT_USD", "4.50")
    ctx, _ = fake_ctx
    ctx.ledger.return_value.entries.return_value = []
    rc = _cmd_cost(_args(json=True), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["throttle_warnings"] == []


def test_replicate_throttle_disabled_zero(
    fake_ctx: tuple[Any, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KINOFORGE_REPLICATE_THROTTLE_AT_USD=0 → no warning ever."""
    monkeypatch.setenv("KINOFORGE_REPLICATE_THROTTLE_AT_USD", "0")
    ctx, _ = fake_ctx
    ctx.ledger.return_value.entries.return_value = []
    rc = _cmd_cost(_args(), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    assert "approaching $5" not in out
