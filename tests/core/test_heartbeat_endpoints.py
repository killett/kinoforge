"""Substrate Protocol + helper tests (B5a Task a).

Verifies:
- HeartbeatEndpoint Protocol is runtime_checkable and structurally satisfied
  by the fake doubles in tests/providers/conftest.py.
- provider_heartbeat_supported returns True for B5a-shipped providers
  (local + runpod) and False otherwise (drift-detector for B5b).
- TransportError sits under the KinoforgeError hierarchy.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kinoforge.core.errors import KinoforgeError, TransportError
from kinoforge.core.heartbeat_endpoints import (
    HeartbeatEndpoint,
    provider_heartbeat_supported,
)
from tests.conftest import FakeRunPodHeartbeatEndpoint


def test_transport_error_is_kinoforge_error() -> None:
    """TransportError must subclass KinoforgeError so existing broad-catch
    arms (HeartbeatLoop._tick_once) keep working."""
    err = TransportError("boom")
    assert isinstance(err, KinoforgeError)


@pytest.mark.parametrize(
    ("provider_kind", "expected"),
    [
        ("local", True),
        ("runpod", True),
        ("skypilot", False),  # drift-detector: flips True when B5b ships
        ("unknown", False),
        ("", False),
    ],
)
def test_provider_heartbeat_supported_table(provider_kind: str, expected: bool) -> None:
    """provider_heartbeat_supported is the gate B1/B2/B3 + classify
    consult before destroying on HEARTBEAT_UNKNOWN. B5a-shipped set is
    {local, runpod}; B5b adds skypilot."""
    assert provider_heartbeat_supported(provider_kind) is expected


def test_protocol_is_runtime_checkable_via_fake_runpod(
    fake_runpod_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """The fake double is structurally a HeartbeatEndpoint via the
    runtime_checkable Protocol — guarantees parity tests can parametrize
    over the Protocol type and the cross-provider fixture works."""
    assert isinstance(fake_runpod_heartbeat_endpoint, HeartbeatEndpoint)


def test_protocol_is_runtime_checkable_via_fake_skypilot(
    fake_skypilot_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """B5b drift mitigation: the SkyPilot fake must satisfy the contract
    BEFORE the wire-level real version ships."""
    assert isinstance(fake_skypilot_heartbeat_endpoint, HeartbeatEndpoint)


def test_fake_runpod_round_trips_tz_aware_datetime(
    fake_runpod_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """The Protocol contract: write+read preserves the TZ-aware datetime.
    Bug catch: a fake that silently strips tzinfo or normalises to UTC
    would let a real wire bug ride."""
    ts = datetime(2026, 6, 12, 14, 23, 5, tzinfo=UTC).astimezone()
    fake_runpod_heartbeat_endpoint.write("pod-x", ts)
    got = fake_runpod_heartbeat_endpoint.read("pod-x")
    assert got == ts
    assert got is not None and got.tzinfo is not None


def test_fake_skypilot_truncates_to_seconds(
    fake_skypilot_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """SkyPilot satisfier reads filesystem mtime (`stat -c %Y`),
    which is second-precision. The fake must mirror that truncation
    so consumers can't accidentally depend on sub-second precision."""
    ts = datetime.now().astimezone().replace(microsecond=500_000)
    fake_skypilot_heartbeat_endpoint.write("cluster-x", ts)
    got = fake_skypilot_heartbeat_endpoint.read("cluster-x")
    assert got is not None
    assert got.microsecond == 0


def test_read_of_never_written_returns_none(
    fake_runpod_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """Reading a slot that was never written is NOT a transport failure;
    it is a valid 'no data yet' answer (Protocol invariant)."""
    assert fake_runpod_heartbeat_endpoint.read("never-written") is None


def test_inject_transport_failure_raises(
    fake_runpod_heartbeat_endpoint: FakeRunPodHeartbeatEndpoint,
) -> None:
    """Fake's inject_transport_failure toggle is the contract for the
    cross-provider parity tests (Task e) — must raise TransportError,
    not a generic Exception."""
    fake_runpod_heartbeat_endpoint.inject_transport_failure("write")
    with pytest.raises(TransportError):
        fake_runpod_heartbeat_endpoint.write("pod-x", datetime.now().astimezone())
