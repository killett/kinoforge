"""Layer W: cross-process xprocess tests for `kinoforge sweeper`.

Spawn `python -m kinoforge sweeper start -c <cfg> --state-dir <dir>` in a
real subprocess against LocalArtifactStore on tmp_path; verify the
synthetic `sweeper:<host>` ledger entry materialises, that SIGTERM
drains within a bounded window, and that SIGHUP shortens the cadence.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _yaml(interval_s: float, *, host: str | None = None) -> str:
    host_line = f"  host: {host}\n" if host else ""
    return (
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
        "sweeper:\n"
        f"  interval_s: {interval_s}\n"
        f"{host_line}"
    )


def _wait_for_entry(
    ledger_path: Path, host: str, *, timeout_s: float = 15.0
) -> dict[str, Any] | None:
    """Poll the on-disk ledger.json until sweeper:<host> appears."""
    deadline = time.monotonic() + timeout_s
    key = f"sweeper:{host}"
    while time.monotonic() < deadline:
        if ledger_path.exists():
            try:
                data = json.loads(ledger_path.read_text())
            except json.JSONDecodeError:
                time.sleep(0.1)
                continue
            for e in data.get("entries", []):
                if e.get("id") == key:
                    return e
        time.sleep(0.1)
    return None


def _spawn(cfg_path: Path, state_dir: Path) -> subprocess.Popen[bytes]:
    """Spawn `kinoforge sweeper start` against the given cfg and state dir."""
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "kinoforge",
            "--state-dir",
            str(state_dir),
            "sweeper",
            "start",
            "-c",
            str(cfg_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_start_then_sigterm(tmp_path: Path) -> None:
    """Subprocess: start daemon; verify entry; SIGTERM; verify exit 0."""
    host = "xprocess-host-a"
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(_yaml(interval_s=1.0, host=host))
    state = tmp_path / "state"
    state.mkdir()

    proc = _spawn(cfg, state)
    try:
        ledger_path = state / "_lifecycle" / "ledger.json"
        entry = _wait_for_entry(ledger_path, host, timeout_s=15.0)
        assert entry is not None, (
            f"sweeper liveness entry never appeared; "
            f"stderr={proc.stderr.read().decode() if proc.stderr else ''}"
        )
        assert entry["provider"] == "_sweeper"
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("daemon did not exit within 15s of SIGTERM") from None
        assert rc == 0, f"non-zero exit {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_status_after_clean_stop(tmp_path: Path) -> None:
    """After SIGTERM, `sweeper status` reports last_sweep_age_s > 0."""
    host = "xprocess-host-b"
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(_yaml(interval_s=1.0, host=host))
    state = tmp_path / "state"
    state.mkdir()

    proc = _spawn(cfg, state)
    try:
        ledger_path = state / "_lifecycle" / "ledger.json"
        assert _wait_for_entry(ledger_path, host, timeout_s=15.0) is not None
        time.sleep(2.0)
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15.0)
        assert rc == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    time.sleep(1.5)  # ensure non-zero (rounded-down-int) age post-stop
    status = subprocess.run(
        [
            sys.executable,
            "-m",
            "kinoforge",
            "--state-dir",
            str(state),
            "sweeper",
            "status",
            "-c",
            str(cfg),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    assert status.returncode == 0, status.stderr
    # main() writes [instance overview] preamble to stdout; --json output is
    # the LAST '{' … '}' block on stdout.
    payload = status.stdout.strip().splitlines()[-1]
    body = json.loads(payload)
    assert body["last_sweep_age_s"] is not None
    assert body["last_sweep_age_s"] > 0


def test_sighup_reloads_interval(tmp_path: Path) -> None:
    """Daemon at 5s; SIGHUP after YAML edit → next ticks ≤ ~2s apart."""
    host = "xprocess-host-c"
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(_yaml(interval_s=5.0, host=host))
    state = tmp_path / "state"
    state.mkdir()

    proc = _spawn(cfg, state)
    try:
        ledger_path = state / "_lifecycle" / "ledger.json"
        first = _wait_for_entry(ledger_path, host, timeout_s=15.0)
        assert first is not None
        cfg.write_text(_yaml(interval_s=1.0, host=host))
        proc.send_signal(signal.SIGHUP)
        ticks: list[float] = []
        deadline = time.monotonic() + 20.0
        last_tick = float(first.get("heartbeat_thread_tick", 0.0))
        while time.monotonic() < deadline and len(ticks) < 3:
            try:
                data = json.loads(ledger_path.read_text())
            except json.JSONDecodeError:
                time.sleep(0.1)
                continue
            for e in data.get("entries", []):
                if e.get("id") == f"sweeper:{host}":
                    t = float(e.get("heartbeat_thread_tick", 0.0))
                    if t > last_tick:
                        ticks.append(t)
                        last_tick = t
                    break
            time.sleep(0.2)
        assert len(ticks) >= 3, f"only saw {len(ticks)} post-SIGHUP ticks"
        deltas = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
        avg = sum(deltas) / len(deltas)
        assert avg <= 2.5, f"avg gap {avg:.2f}s — SIGHUP did not shorten interval"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
