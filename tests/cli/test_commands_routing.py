"""Per-cmd lockdown — every handler reads ledger/store via ctx, never via removed helpers.

These tests construct a SessionContext directly (no subprocess), seed it with
in-memory state, and assert each _cmd_* handler behaves correctly through the
ctx.ledger() / ctx.store() paths.  They also verify that _print_instance_overview
accepts a SessionContext (not a Path), exercising the T7 signature contract.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kinoforge.cli import _commands
from kinoforge.cli._main import _print_instance_overview
from kinoforge.cli.context import SessionContext
from kinoforge.core.interfaces import Instance


def _ns(**kwargs: object) -> argparse.Namespace:
    """Build an argparse.Namespace from keyword arguments."""
    ns = argparse.Namespace()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx_no_cfg(tmp_path: Path) -> SessionContext:
    """Build a minimal SessionContext with no config and no sidecar."""
    return SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)


def _seed_ledger(ctx: SessionContext, iid: str = "i-1") -> None:
    """Write one Instance entry into ctx's ledger."""
    ctx.ledger().record(
        Instance(
            id=iid,
            provider="local",
            status="ready",
            tags={},
            created_at=0.0,
            cost_rate_usd_per_hr=0.0,
        )
    )


# ---------------------------------------------------------------------------
# _cmd_list
# ---------------------------------------------------------------------------


def test_cmd_list_routes_via_ctx(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """_cmd_list prints the instance id it got from ctx.ledger()."""
    ctx = _ctx_no_cfg(tmp_path)
    _seed_ledger(ctx, "i-list-1")
    args = _ns()
    assert _commands._cmd_list(args, ctx) == 0
    out = capsys.readouterr().out
    assert "i-list-1" in out


def test_cmd_list_empty_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """_cmd_list on empty ledger prints the 'No instances' message."""
    ctx = _ctx_no_cfg(tmp_path)
    args = _ns()
    assert _commands._cmd_list(args, ctx) == 0
    out = capsys.readouterr().out
    assert "No instances" in out


# ---------------------------------------------------------------------------
# _cmd_forget
# ---------------------------------------------------------------------------


def test_cmd_forget_routes_via_ctx(tmp_path: Path) -> None:
    """_cmd_forget removes the seeded instance from ctx.ledger()."""
    ctx = _ctx_no_cfg(tmp_path)
    _seed_ledger(ctx, "i-forget-1")
    args = _ns(id="i-forget-1")
    assert _commands._cmd_forget(args, ctx) == 0
    assert ctx.ledger().entries() == []


def test_cmd_forget_missing_id_returns_1(tmp_path: Path) -> None:
    """_cmd_forget returns 1 when the id is not in the ledger."""
    ctx = _ctx_no_cfg(tmp_path)
    args = _ns(id="i-absent")
    assert _commands._cmd_forget(args, ctx) == 1


# ---------------------------------------------------------------------------
# _cmd_stop
# ---------------------------------------------------------------------------


def test_cmd_stop_unknown_id_returns_1(tmp_path: Path) -> None:
    """_cmd_stop returns 1 when the id is absent from the ledger."""
    ctx = _ctx_no_cfg(tmp_path)
    args = _ns(id="i-absent")
    assert _commands._cmd_stop(args, ctx) == 1


# ---------------------------------------------------------------------------
# _cmd_destroy
# ---------------------------------------------------------------------------


def test_cmd_destroy_unknown_id_returns_1(tmp_path: Path) -> None:
    """_cmd_destroy returns 1 when the id is absent from the ledger."""
    ctx = _ctx_no_cfg(tmp_path)
    args = _ns(id="i-absent")
    assert _commands._cmd_destroy(args, ctx) == 1


# ---------------------------------------------------------------------------
# _cmd_reap
# ---------------------------------------------------------------------------


def test_cmd_reap_returns_0_with_empty_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """_cmd_reap on an empty ledger exits 0 and prints an informational message."""
    ctx = _ctx_no_cfg(tmp_path)
    args = _ns()
    assert _commands._cmd_reap(args, ctx) == 0
    out = capsys.readouterr().out
    assert "empty" in out.lower() or "no" in out.lower()


# ---------------------------------------------------------------------------
# _cmd_status
# ---------------------------------------------------------------------------


def test_cmd_status_missing_id_returns_1(tmp_path: Path) -> None:
    """_cmd_status returns 1 when the id is absent from the ledger."""
    ctx = _ctx_no_cfg(tmp_path)
    args = _ns(id="i-absent", config=None)
    assert _commands._cmd_status(args, ctx) == 1


# ---------------------------------------------------------------------------
# _print_instance_overview — accepts ctx (not state_dir)
# ---------------------------------------------------------------------------


def test_print_instance_overview_works_via_ctx(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug-catch: a future _print_instance_overview that takes state_dir
    instead of ctx breaks the no-config CLI path."""
    ctx = _ctx_no_cfg(tmp_path)
    _print_instance_overview(ctx)
    out = capsys.readouterr().out
    assert "instance overview" in out.lower()


def test_print_instance_overview_lists_instances(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """_print_instance_overview shows seeded instance id."""
    ctx = _ctx_no_cfg(tmp_path)
    _seed_ledger(ctx, "i-overview-1")
    _print_instance_overview(ctx)
    out = capsys.readouterr().out
    assert "i-overview-1" in out


def test_overview_degrades_when_ledger_safe_returns_none(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ledger_safe failure is surfaced in the overview as 'unavailable'."""
    ctx = _ctx_no_cfg(tmp_path)

    def _broken_ledger_safe(self: SessionContext) -> tuple[None, str]:  # noqa: ARG001
        return None, "RuntimeError: simulated"

    monkeypatch.setattr(SessionContext, "ledger_safe", _broken_ledger_safe)
    _print_instance_overview(ctx)
    out = capsys.readouterr().out
    assert "unavailable" in out
    assert "RuntimeError: simulated" in out


# ---------------------------------------------------------------------------
# Verify _ledger helper has been removed from _commands
# ---------------------------------------------------------------------------


def test_ledger_helper_removed_from_commands() -> None:
    """_ledger(state_dir) must no longer exist as a callable in _commands."""
    assert not hasattr(_commands, "_ledger"), (
        "_ledger helper was not deleted from _commands — ctx.ledger() must be used instead"
    )


# ---------------------------------------------------------------------------
# Layer V — kinoforge status verdict line
# ---------------------------------------------------------------------------


def test_cmd_status_surfaces_verdict_line_for_live_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When pod_up + sentinel-fresh + hb-fresh → verdict=LIVE printed."""
    import time as _t

    from kinoforge.cli import _commands
    from kinoforge.cli.context import SessionContext
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import Instance
    from kinoforge.providers.local import LocalProvider

    _CFG_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
    heartbeat_interval_s: 30
"""
    state_dir = tmp_path / "state"
    cfg = load_config(_CFG_YAML)
    ctx = SessionContext(state_dir=state_dir, cfg=cfg, sidecar=None)
    ledger = ctx.ledger()

    now = _t.time()
    instance = Instance(
        id="i-1",
        provider="local",
        created_at=now,
        status="ready",
        cost_rate_usd_per_hr=0.0,
        tags={},
    )
    ledger.record(instance)
    ledger.touch("i-1", last_heartbeat=now, heartbeat_thread_tick=now)

    # Force registry to return a LocalProvider with our pre-stocked instance.
    fake_provider = LocalProvider()
    fake_provider._instances = {"i-1": instance}

    def _factory() -> LocalProvider:
        return fake_provider

    monkeypatch.setattr("kinoforge.core.registry.get_provider", lambda _name: _factory)

    args = argparse.Namespace(id="i-1", config=None)
    code = _commands._cmd_status(args, ctx)
    out = capsys.readouterr().out
    assert code == 0
    assert "verdict=LIVE" in out


def test_cmd_status_surfaces_verdict_unroutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the registry has no factory → verdict=UNROUTABLE printed."""
    import time as _t

    from kinoforge.cli import _commands
    from kinoforge.cli.context import SessionContext
    from kinoforge.core.errors import UnknownAdapter
    from kinoforge.core.interfaces import Instance

    state_dir = tmp_path / "state"
    ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=None)
    ledger = ctx.ledger()
    now = _t.time()
    instance = Instance(
        id="i-1",
        provider="bogus",
        created_at=now,
        status="ready",
        cost_rate_usd_per_hr=0.0,
        tags={},
    )
    ledger.record(instance)

    def _raise(_name: str) -> object:
        raise UnknownAdapter("bogus")

    monkeypatch.setattr("kinoforge.core.registry.get_provider", _raise)

    args = argparse.Namespace(id="i-1", config=None)
    code = _commands._cmd_status(args, ctx)
    out = capsys.readouterr().out
    assert code == 2
    assert "verdict=UNROUTABLE" in out


def test_cmd_status_verdict_heartbeat_unknown_on_list_instances_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """list_instances() failure surfaces verdict=HEARTBEAT_UNKNOWN.

    Honest fallback: if the provider can't enumerate instances we can
    no longer compute classify reliably, so we don't bias toward LIVE.
    """
    import time as _t

    from kinoforge.cli import _commands
    from kinoforge.cli.context import SessionContext
    from kinoforge.core.interfaces import Instance
    from kinoforge.providers.local import LocalProvider

    state_dir = tmp_path / "state"
    ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=None)
    ledger = ctx.ledger()
    now = _t.time()
    instance = Instance(
        id="i-1",
        provider="local",
        created_at=now,
        status="ready",
        cost_rate_usd_per_hr=0.0,
        tags={},
    )
    ledger.record(instance)

    class _BrokenLocal(LocalProvider):
        def list_instances(self) -> list[Instance]:
            raise RuntimeError("simulated list failure")

    broken = _BrokenLocal()
    broken._instances = {"i-1": instance}

    monkeypatch.setattr(
        "kinoforge.core.registry.get_provider", lambda _name: lambda: broken
    )

    args = argparse.Namespace(id="i-1", config=None)
    code = _commands._cmd_status(args, ctx)
    out = capsys.readouterr().out
    assert code == 0
    assert "verdict=HEARTBEAT_UNKNOWN" in out


# ---------------------------------------------------------------------------
# B4 — _cmd_list capability_key column
# ---------------------------------------------------------------------------


def test_cmd_list_includes_capability_key_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each list row shows `capability_key=<hash>` sourced from tags."""
    ctx = _ctx_no_cfg(tmp_path)
    ctx.ledger().record(
        Instance(
            id="i-1",
            provider="local",
            status="ready",
            tags={"kinoforge_key": "ab12cd34ef56"},
            created_at=1.0,
            cost_rate_usd_per_hr=0.0,
        )
    )
    args = _ns()
    assert _commands._cmd_list(args, ctx) == 0
    out = capsys.readouterr().out
    assert "capability_key=ab12cd34ef56" in out


def test_cmd_list_prints_unknown_for_legacy_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Legacy entry without tags.kinoforge_key → capability_key=<unknown>."""
    ctx = _ctx_no_cfg(tmp_path)
    ctx.ledger().record(
        Instance(
            id="i-legacy",
            provider="local",
            status="ready",
            tags={},
            created_at=1.0,
            cost_rate_usd_per_hr=0.0,
        )
    )
    args = _ns()
    assert _commands._cmd_list(args, ctx) == 0
    out = capsys.readouterr().out
    assert "capability_key=<unknown>" in out
