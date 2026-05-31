"""Tests for core.provision_state helpers (Layer I)."""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.provision_state import (
    is_marker_current,
    marker_path,
    read_marker,
    write_marker,
)


def test_marker_path_layout(tmp_path: Path) -> None:
    """marker_path returns <state_dir>/instances/<instance_id>/.provisioned."""
    p = marker_path(tmp_path, "i-abc123")
    assert p == tmp_path / "instances" / "i-abc123" / ".provisioned"


def test_read_marker_returns_none_when_absent(tmp_path: Path) -> None:
    """Missing marker file yields None, never raises."""
    p = tmp_path / "instances" / "i-x" / ".provisioned"
    assert read_marker(p) is None


def test_read_marker_returns_none_when_corrupt(tmp_path: Path) -> None:
    """Corrupt JSON yields None, never raises (self-healing on next provision)."""
    p = tmp_path / "instances" / "i-x" / ".provisioned"
    p.parent.mkdir(parents=True)
    p.write_text("not json at all {{{")
    assert read_marker(p) is None


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    """write_marker then read_marker yields the exact dict written."""
    p = marker_path(tmp_path, "i-abc")
    write_marker(p, "i-abc", "key-hex-xyz", "comfyui", 1717200000.5)
    record = read_marker(p)
    assert record is not None
    assert record["instance_id"] == "i-abc"
    assert record["capability_key"] == "key-hex-xyz"
    assert record["engine"] == "comfyui"
    assert record["timestamp"] == 1717200000.5


def test_is_marker_current_staleness_rule(tmp_path: Path) -> None:
    """is_marker_current returns True iff the cached key matches current key."""
    marker = {
        "instance_id": "i-abc",
        "capability_key": "abc123",
        "engine": "comfyui",
        "timestamp": 1.0,
    }
    assert is_marker_current(marker, "abc123") is True
    assert is_marker_current(marker, "xyz789") is False


def test_read_marker_returns_none_when_keys_missing(tmp_path: Path) -> None:
    """Marker missing required keys yields None (treated as not-provisioned)."""
    p = tmp_path / "instances" / "i-x" / ".provisioned"
    p.parent.mkdir(parents=True)
    p.write_text('{"instance_id": "i-x"}')  # missing capability_key, engine, timestamp
    assert read_marker(p) is None
