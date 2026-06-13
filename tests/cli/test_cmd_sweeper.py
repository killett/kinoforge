"""Layer W: offline CLI tests for `kinoforge sweeper`.

All paths exercised against LocalArtifactStore + LocalProvider on tmp_path.
SweeperLoop.start is patched to no-op so `start` exits without spawning
the background thread (xprocess tests cover the live spawn path).
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import socket
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

import kinoforge._adapters  # noqa: F401 — side-effect: register builtins
from kinoforge.cli._commands import (
    _cmd_sweeper_metrics,
    _cmd_sweeper_start,
    _cmd_sweeper_status,
    _cmd_sweeper_stop,
)
from kinoforge.cli.context import SessionContext

_CFG_TEMPLATE = (
    "compute:\n"
    "  provider: local\n"
    "  image: dummy\n"
    "engine:\n"
    "  kind: fake\n"
    "  precision: fp16\n"
    "models:\n"
    "  - ref: hf:org/m\n"
    "    kind: base\n"
    "    target: checkpoints\n"
)


def _make_ctx(
    tmp_path: Path, *, sweeper_block: str = ""
) -> tuple[SessionContext, Path]:
    """Build a real SessionContext on LocalArtifactStore in tmp_path."""
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(_CFG_TEMPLATE + sweeper_block)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=cfg_path)
    return ctx, cfg_path


def _args(**overrides: object) -> argparse.Namespace:
    base = argparse.Namespace(json=False, prom=False, config=None, interval_s=None)
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ---------------------------------------------------------------------------
# status / metrics
# ---------------------------------------------------------------------------


def test_cmd_sweeper_status_no_entry(tmp_path: Path) -> None:
    """No sweeper running → running=false, exit 0."""
    ctx, _ = _make_ctx(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = _cmd_sweeper_status(_args(json=False), ctx)
    assert rc == 0
    assert "running=false" in out.getvalue()


def test_cmd_sweeper_status_json_shape(tmp_path: Path) -> None:
    """--json parses; every required key present."""
    ctx, _ = _make_ctx(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = _cmd_sweeper_status(_args(json=True), ctx)
    assert rc == 0
    body = json.loads(out.getvalue())
    for k in (
        "host",
        "pid",
        "running",
        "last_sweep_ts",
        "last_sweep_age_s",
        "interval_s",
        "stale",
        "sweeps_total",
        "destroys_total",
        "deferred_total",
        "errors_total",
    ):
        assert k in body, f"missing key {k!r}"


def test_cmd_sweeper_metrics_prom_format(tmp_path: Path) -> None:
    """--prom output contains all required series + LF-only line endings."""
    ctx, _ = _make_ctx(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = _cmd_sweeper_metrics(_args(prom=True), ctx)
    assert rc == 0
    body = out.getvalue()
    assert "\r" not in body
    for series in (
        "kinoforge_sweeper_sweeps_total",
        "kinoforge_sweeper_destroys_total",
        "kinoforge_sweeper_deferred_total",
        "kinoforge_sweeper_errors_total",
        "kinoforge_sweeper_interval_s",
    ):
        assert series in body


def test_cmd_sweeper_stop_no_entry(tmp_path: Path) -> None:
    """No sweeper running → stderr message + exit 1."""
    ctx, _ = _make_ctx(tmp_path)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = _cmd_sweeper_stop(_args(), ctx)
    assert rc == 1
    assert "no sweeper running" in err.getvalue()


def test_cmd_sweeper_status_stale_flag(tmp_path: Path) -> None:
    """Entry with heartbeat_thread_tick > 3 * interval_s in the past → stale=true."""
    import time as _t

    from kinoforge.core.interfaces import Instance

    host = "test-host"
    ctx, _ = _make_ctx(
        tmp_path, sweeper_block=f"sweeper:\n  interval_s: 1\n  host: {host}\n"
    )
    ledger = ctx.ledger()
    ledger.record(
        Instance(
            id=f"sweeper:{host}",
            provider="_sweeper",
            status="ready",
            created_at=_t.time() - 100.0,
            cost_rate_usd_per_hr=0.0,
        )
    )
    ledger.touch(
        f"sweeper:{host}",
        last_heartbeat=_t.time() - 100.0,
        heartbeat_thread_tick=_t.time() - 100.0,
        pid=os.getpid(),
    )
    out = io.StringIO()
    with redirect_stdout(out):
        rc = _cmd_sweeper_status(_args(json=True), ctx)
    assert rc == 0
    body = json.loads(out.getvalue())
    assert body["stale"] is True


def test_banner_emitted_on_start_dry_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`start` emits the §4.7 banner at INFO before installing handlers."""
    ctx, cfg_path = _make_ctx(tmp_path)
    with (
        patch("kinoforge.core.sweeper.SweeperLoop.start", lambda self: None),
        patch("kinoforge.core.sweeper.SweeperLoop.stop", lambda self: None),
        patch("threading.Event.wait", return_value=True),
        patch("signal.signal"),
        caplog.at_level(logging.INFO, logger="kinoforge.cli._commands"),
    ):
        rc = _cmd_sweeper_start(_args(config=str(cfg_path), interval_s=None), ctx)
    assert rc == 0
    joined = " ".join(rec.message for rec in caplog.records)
    assert "kinoforge sweeper starting" in joined
    assert "B5a heartbeat-substrate gate is ACTIVE" in joined
    assert "B7 cooperative session-claim probe is ACTIVE" in joined


def test_cmd_sweeper_start_records_synthetic_entry(tmp_path: Path) -> None:
    """Start materialises sweeper:<host> via Ledger.record + sets pid via touch."""
    ctx, cfg_path = _make_ctx(tmp_path)
    host = socket.gethostname()
    with (
        patch("kinoforge.core.sweeper.SweeperLoop.start", lambda self: None),
        patch("kinoforge.core.sweeper.SweeperLoop.stop", lambda self: None),
        patch("threading.Event.wait", return_value=True),
        patch("signal.signal"),
    ):
        rc = _cmd_sweeper_start(_args(config=str(cfg_path), interval_s=None), ctx)
    assert rc == 0
    entry = ctx.ledger().read(f"sweeper:{host}")
    assert entry is not None
    assert entry["provider"] == "_sweeper"
    assert int(entry["pid"]) == os.getpid()
    assert entry["cost_rate_usd_per_hr"] == 0.0
