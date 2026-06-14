"""Unit cover for the C27 Phase A1 FakeUtilEndpoint helper.

These tests are fast (no network) and intentionally live in ``tests/live/``
next to the smoke they support — they are NOT gated on ``KINOFORGE_LIVE``.
"""

from __future__ import annotations

from kinoforge.core.util_endpoints import UtilSnapshot, UtilSnapshotEndpoint
from tests.live._c27_fake_util_endpoint import FakeUtilEndpoint


def _snap(uptime: int) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=0.0,
        cpu_percent=13.0,
        memory_percent=20.0,
        disk_percent=None,
        uptime_seconds=uptime,
    )


def test_fake_util_endpoint_returns_configured_snapshot() -> None:
    ep = FakeUtilEndpoint(snap=_snap(uptime=1))
    out = ep.read_util("p1")
    assert out is not None
    assert out.uptime_seconds == 1
    assert out.gpu_util_percent == 0.0


def test_fake_util_endpoint_returns_same_snap_on_repeated_reads() -> None:
    """Phase A1 needs a steady low-uptime read every tick to drive the counter."""
    ep = FakeUtilEndpoint(snap=_snap(uptime=5))
    out1 = ep.read_util("p1")
    out2 = ep.read_util("p1")
    out3 = ep.read_util("p2")
    assert out1 is out2 is out3
    assert out1 is not None
    assert out1.uptime_seconds == 5


def test_fake_util_endpoint_satisfies_protocol_structurally() -> None:
    """Protocol membership check — runtime_checkable on UtilSnapshotEndpoint."""
    ep = FakeUtilEndpoint(snap=_snap(uptime=1))
    assert isinstance(ep, UtilSnapshotEndpoint)
