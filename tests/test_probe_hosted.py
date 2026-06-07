"""Unit tests for tools/probe_hosted.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._fixtures.fake_auth import FakeAuthStrategy
from tools.probe_hosted import (
    ProbeResult,
    probe_strategies,
    write_snapshot,
)


def test_probe_strategies_all_pass() -> None:
    strategies = [
        ("hosted", FakeAuthStrategy(fake_identity="id-1")),
        ("veo", FakeAuthStrategy(fake_identity="id-2")),
    ]
    results = probe_strategies(strategies)
    assert all(r.ok for r in results)
    assert [r.name for r in results] == ["hosted", "veo"]
    assert [r.identity for r in results] == ["id-1", "id-2"]


def test_probe_strategies_fails_on_missing_creds() -> None:
    strategies = [
        ("hosted", FakeAuthStrategy(credentials_ok=False)),
        ("veo", FakeAuthStrategy(fake_identity="id-2")),
    ]
    results = probe_strategies(strategies)
    assert results[0].ok is False
    assert (
        "missing" in (results[0].reason or "").lower()
        or "disabled" in (results[0].reason or "").lower()
    )
    assert results[1].ok is True


def test_write_snapshot_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [
        ProbeResult(name="hosted", ok=True, identity="id-1", reason=None),
        ProbeResult(name="veo", ok=False, identity=None, reason="boom"),
    ]
    snap_path = tmp_path / "probe-test.json"
    monkeypatch.setattr("tools.probe_hosted._git_sha", lambda: "deadbeef")
    write_snapshot(snap_path, results)
    body = json.loads(snap_path.read_text())
    assert body["git_sha"] == "deadbeef"
    assert body["strategies"] == [
        {"name": "hosted", "ok": True, "identity": "id-1", "reason": None},
        {"name": "veo", "ok": False, "identity": None, "reason": "boom"},
    ]
    assert "captured_at" in body


def test_write_snapshot_uses_tmp_then_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No partial snapshot survives a crash mid-write."""
    results = [ProbeResult(name="hosted", ok=True, identity="id", reason=None)]
    snap_path = tmp_path / "probe-test.json"
    monkeypatch.setattr("tools.probe_hosted._git_sha", lambda: "deadbeef")
    write_snapshot(snap_path, results)
    # Tmp file must have been removed (rename = atomic).
    tmp_files = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert tmp_files == []


def test_probe_exit_code_zero_on_all_pass(tmp_path: Path) -> None:
    """Run the tool entrypoint with FakeAuthStrategy injected."""
    from tools.probe_hosted import run

    strategies = [("hosted", FakeAuthStrategy())]
    exit_code = run(strategies, snapshot_path=tmp_path / "probe.json")
    assert exit_code == 0


def test_probe_exit_code_nonzero_on_any_fail(tmp_path: Path) -> None:
    from tools.probe_hosted import run

    strategies = [
        ("hosted", FakeAuthStrategy()),
        ("veo", FakeAuthStrategy(credentials_ok=False)),
    ]
    exit_code = run(strategies, snapshot_path=tmp_path / "probe.json")
    assert exit_code != 0
