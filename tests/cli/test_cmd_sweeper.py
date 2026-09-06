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


def test_cmd_sweeper_start_installs_handlers_before_ledger_entry(
    tmp_path: Path,
) -> None:
    """SIGTERM handler must be live BEFORE the liveness ledger entry lands.

    Bug caught (CI ubuntu flake, run 28696410210, 2026-07-04): the
    xprocess test (and any supervisor) treats the sweeper:<host> ledger
    entry as the readiness signal and may SIGTERM immediately after
    seeing it. With ledger.record before signal.signal, the signal
    lands on the default handler and the daemon dies rc=-15 instead of
    the graceful exit-0 path.
    """
    ctx, cfg_path = _make_ctx(tmp_path)
    order: list[str] = []

    real_record = type(ctx.ledger()).record

    def _recording_record(self: object, entry: object) -> None:
        order.append("ledger.record")
        real_record(self, entry)  # type: ignore[arg-type]

    with (
        patch("kinoforge.core.sweeper.SweeperLoop.start", lambda self: None),
        patch("kinoforge.core.sweeper.SweeperLoop.stop", lambda self: None),
        patch("threading.Event.wait", return_value=True),
        patch("signal.signal", side_effect=lambda *_a: order.append("signal.signal")),
        patch.object(type(ctx.ledger()), "record", _recording_record),
    ):
        rc = _cmd_sweeper_start(_args(config=str(cfg_path), interval_s=None), ctx)
    assert rc == 0
    assert "ledger.record" in order and "signal.signal" in order
    first_signal = order.index("signal.signal")
    first_record = order.index("ledger.record")
    assert first_signal < first_record, (
        f"handlers installed after ledger entry — SIGTERM race window: {order}"
    )


# ---------------------------------------------------------------------------
# stop — the liveness row must not outlive the daemon (U10)
# ---------------------------------------------------------------------------


def test_cmd_sweeper_stop_leaves_ledger_clean(tmp_path: Path) -> None:
    """A start → stop cycle must leave the ledger with zero entries.

    Invariant: ``sweeper:<host>`` is the daemon's liveness signal, so it must
    not outlive the daemon that published it.

    Bug caught (U10, matrix cell T1-24, 2026-09-06): `_cmd_sweeper_stop`
    returned 0 on the stable-tick path without removing the row, so
    `Ledger.entries()` stayed non-empty and `kinoforge list` could never
    print `No instances recorded in ledger.` after any sweeper had ever
    run — clearing it required a manual `kinoforge forget --id sweeper:<host>`.
    Also fails if a later change removes the row only on the branch where
    the daemon happened to delete it itself.
    """
    ctx, cfg_path = _make_ctx(tmp_path)
    host = socket.gethostname()
    with (
        patch("kinoforge.core.sweeper.SweeperLoop.start", lambda self: None),
        patch("kinoforge.core.sweeper.SweeperLoop.stop", lambda self: None),
        patch("threading.Event.wait", return_value=True),
        patch("signal.signal"),
    ):
        assert (
            _cmd_sweeper_start(_args(config=str(cfg_path), interval_s=None), ctx) == 0
        )
    assert ctx.ledger().read(f"sweeper:{host}") is not None, "precondition: row exists"

    # Daemon is signalled and its heartbeat tick then stays frozen, which is
    # the real stop handshake.  os.kill / time.sleep are the only boundaries
    # mocked; the ledger is the real one on tmp_path.
    with patch("os.kill"), patch("time.sleep", lambda _s: None):
        rc = _cmd_sweeper_stop(_args(config=str(cfg_path)), ctx)

    assert rc == 0
    assert ctx.ledger().read(f"sweeper:{host}") is None, (
        "sweeper liveness row outlived the daemon"
    )
    assert ctx.ledger().entries() == [], (
        f"ledger not clean after stop: {ctx.ledger().entries()}"
    )


def test_cmd_sweeper_stop_keeps_liveness_row_when_daemon_will_not_die(
    tmp_path: Path,
) -> None:
    """A daemon that keeps ticking past the deadline keeps its liveness row.

    Bug caught: an over-broad U10 fix — `ledger.forget` unconditionally, or in
    a `finally` — would erase the liveness row of a daemon that is still alive
    and still sweeping.  `sweeper status` would then report `running=false`
    for a running daemon, and the next `sweeper stop` would exit 1 with
    "no sweeper running" with no pid left to signal.
    """
    ctx, cfg_path = _make_ctx(tmp_path)
    host = socket.gethostname()
    with (
        patch("kinoforge.core.sweeper.SweeperLoop.start", lambda self: None),
        patch("kinoforge.core.sweeper.SweeperLoop.stop", lambda self: None),
        patch("threading.Event.wait", return_value=True),
        patch("signal.signal"),
    ):
        assert (
            _cmd_sweeper_start(_args(config=str(cfg_path), interval_s=None), ctx) == 0
        )

    ledger = ctx.ledger()
    ticks = iter(range(1, 1000))
    clock = iter([float(n) * 10.0 for n in range(1000)])

    def _still_alive(_s: float) -> None:
        # The daemon is alive: every poll interval it advances its tick.
        ledger.touch(f"sweeper:{host}", heartbeat_thread_tick=float(next(ticks)))

    with (
        patch("os.kill"),
        patch("time.sleep", _still_alive),
        patch("time.monotonic", lambda: next(clock)),
    ):
        rc = _cmd_sweeper_stop(_args(config=str(cfg_path)), ctx)

    assert rc == 2
    assert ctx.ledger().read(f"sweeper:{host}") is not None, (
        "erased a live daemon's liveness row on the timeout path"
    )
