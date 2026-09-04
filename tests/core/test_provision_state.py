"""Tests for core.provision_state helpers (Layer I)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

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


def test_read_marker_is_silent_when_the_marker_is_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An absent marker is the normal first-generate path, not an anomaly.

    Bug catch: warning on every absent marker would fire on every first
    generate against a fresh instance, training the reader to ignore the
    warning that the two tests below depend on being meaningful.
    """
    p = tmp_path / "instances" / "i-x" / ".provisioned"
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.provision_state"):
        assert read_marker(p) is None
    assert caplog.records == []


def test_read_marker_warns_when_present_but_corrupt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A marker that EXISTS but does not parse is an anomaly, not a first run.

    Bug catch: read_marker collapses "absent" and "present but unreadable"
    into the same None, and the caller's only reaction is to silently
    re-provision. Without this warning the two are indistinguishable in a
    log, so a re-provision caused by corruption looks exactly like a normal
    cold start — which is how an unexplained double-provision stays
    unexplained.
    """
    p = tmp_path / "instances" / "i-x" / ".provisioned"
    p.parent.mkdir(parents=True)
    p.write_text("not json at all {{{")
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.provision_state"):
        assert read_marker(p) is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert str(p) in caplog.records[0].getMessage()


def test_read_marker_warns_when_present_but_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The transient-IO path: the file is there, the read fails.

    Bug catch: under heavy load an EIO/EMFILE on this open is swallowed
    whole, the caller re-runs a provision that may cost minutes and real
    money on a live pod, and nothing anywhere records why. Simulated with a
    real OSError from ``Path.open`` because that is the exact call that
    fails; the file itself stays valid, proving the warning keys off the
    read failure and not off the content.
    """
    p = marker_path(tmp_path, "i-abc")
    write_marker(p, "i-abc", "key-hex-xyz", "comfyui", 1717200000.5)
    assert read_marker(p) is not None  # baseline: readable before the fault

    def _raise_eio(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Path, "open", _raise_eio)
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.provision_state"):
        assert read_marker(p) is None
    assert len(caplog.records) == 1
    assert "Input/output error" in caplog.records[0].getMessage()


def test_read_marker_returns_none_when_keys_missing(tmp_path: Path) -> None:
    """Marker missing required keys yields None (treated as not-provisioned)."""
    p = tmp_path / "instances" / "i-x" / ".provisioned"
    p.parent.mkdir(parents=True)
    p.write_text('{"instance_id": "i-x"}')  # missing capability_key, engine, timestamp
    assert read_marker(p) is None
