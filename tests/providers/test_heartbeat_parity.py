"""Cross-provider parity tests for HeartbeatEndpoint (B5a Task e).

Parametrizes the Protocol-contract invariants across three fakes:
- LocalHeartbeatEndpoint (in-process dict; sub-second precision)
- FakeRunPodHeartbeatEndpoint (mirrors GraphQL-tag ISO round-trip;
  sub-second precision)
- FakeSkyPilotHeartbeatEndpoint (mirrors filesystem-mtime stat;
  second-precision truncation, SSH-refused transport failures)

The parity test is the load-bearing artifact: it freezes the contract
from BOTH the RunPod (sub-second) and SkyPilot (second-precision) sides
BEFORE B5b's wire implementation lands. When B5b ships, the parity
suite must still pass; only the SkyPilot real wire replaces the fake
in the live-path injection.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.core.heartbeat_endpoints import HeartbeatEndpoint

EndpointFactory = Callable[[], HeartbeatEndpoint]


def _local_factory() -> HeartbeatEndpoint:
    # Imported lazily so the conftest.py fixtures registered by Task a's
    # `tests/providers/conftest.py` are guaranteed available.
    from tests.conftest import LocalHeartbeatEndpoint

    return LocalHeartbeatEndpoint()


def _runpod_factory() -> HeartbeatEndpoint:
    from tests.conftest import FakeRunPodHeartbeatEndpoint

    return FakeRunPodHeartbeatEndpoint()


def _skypilot_factory() -> HeartbeatEndpoint:
    from tests.conftest import FakeSkyPilotHeartbeatEndpoint

    return FakeSkyPilotHeartbeatEndpoint()


_FACTORIES: dict[str, EndpointFactory] = {
    "local": _local_factory,
    "runpod": _runpod_factory,
    "skypilot": _skypilot_factory,
}


@pytest.fixture(params=list(_FACTORIES.keys()))
def endpoint(request: pytest.FixtureRequest) -> HeartbeatEndpoint:
    """Parametrized fixture: one HeartbeatEndpoint per registered fake."""
    return _FACTORIES[request.param]()


def test_read_of_never_written_returns_none(endpoint: HeartbeatEndpoint) -> None:
    """Protocol invariant: a slot that was never written reads as None,
    NOT raises TransportError. This is what makes B3 cross-session
    warm-reuse safe — a fresh CLI invocation queries the slot, gets None,
    and proceeds to provision a new pod (rather than crashing)."""
    assert endpoint.read("never-written") is None


def test_write_then_read_round_trips_wall_clock(endpoint: HeartbeatEndpoint) -> None:
    """Tolerates SkyPilot's second-precision truncation by comparing
    at second granularity (the floor for any consumer per substrate
    invariant)."""
    ts = datetime.now().astimezone().replace(microsecond=0)
    endpoint.write("pod-x", ts)
    got = endpoint.read("pod-x")
    assert got is not None
    assert got == ts


def test_double_write_same_ts_is_idempotent(endpoint: HeartbeatEndpoint) -> None:
    """Writing the same ts twice must not raise (e.g. via a 'duplicate
    key' constraint in a hypothetical satisfier that misuses a unique
    index)."""
    ts = datetime.now().astimezone().replace(microsecond=0)
    endpoint.write("pod-x", ts)
    endpoint.write("pod-x", ts)
    assert endpoint.read("pod-x") == ts


def test_write_then_overwrite_returns_latest(endpoint: HeartbeatEndpoint) -> None:
    """The slot is single-value, not a log. Latest write wins."""
    from datetime import timedelta

    ts1 = datetime.now().astimezone().replace(microsecond=0)
    ts2 = ts1 + timedelta(seconds=5)
    endpoint.write("pod-x", ts1)
    endpoint.write("pod-x", ts2)
    assert endpoint.read("pod-x") == ts2


def test_read_after_instance_destroyed_returns_none(
    endpoint: HeartbeatEndpoint,
) -> None:
    """When the underlying pod/cluster is gone, read returns None — not
    a stale value, not a TransportError. Layer V relies on this to
    classify STALE_LEDGER vs LIVE without false positives."""
    ts = datetime.now().astimezone().replace(microsecond=0)
    endpoint.write("pod-x", ts)
    # destroy_instance is a test-helper on the fakes (not on the
    # production Protocol). Mirrors real provider teardown.
    endpoint.destroy_instance("pod-x")  # type: ignore[attr-defined]
    assert endpoint.read("pod-x") is None


def test_transport_failure_raises_TransportError_on_write(
    endpoint: HeartbeatEndpoint,
) -> None:
    """The substrate exception is TransportError — concrete satisfiers
    that swallow vendor exceptions silently break the Layer U
    HeartbeatLoop's broad try/except envelope."""
    # Branch on fake kind: SkyPilot uses inject_ssh_refused (it can't
    # selectively fail just write — SSH refused breaks both).
    if hasattr(endpoint, "inject_ssh_refused"):
        endpoint.inject_ssh_refused()
    else:
        endpoint.inject_transport_failure("write")  # type: ignore[attr-defined]
    with pytest.raises(TransportError):
        endpoint.write("pod-x", datetime.now().astimezone())


def test_transport_failure_raises_TransportError_on_read(
    endpoint: HeartbeatEndpoint,
) -> None:
    """Same as above, read direction."""
    if hasattr(endpoint, "inject_ssh_refused"):
        endpoint.inject_ssh_refused()
    else:
        endpoint.inject_transport_failure("read")  # type: ignore[attr-defined]
    with pytest.raises(TransportError):
        endpoint.read("pod-x")


def test_second_precision_minimum(endpoint: HeartbeatEndpoint) -> None:
    """The contract floor: wall-clock round-trip preserves at LEAST
    1-second precision. Anything less precise breaks Layer V's
    dead-man window math (window is heartbeat_interval_s * 3,
    minimum 30s — second-precision is comfortably below that)."""
    ts = datetime.now().astimezone().replace(microsecond=0)
    endpoint.write("pod-x", ts)
    got = endpoint.read("pod-x")
    assert got is not None
    assert abs((got - ts).total_seconds()) < 1.0


def test_overwrite_does_not_create_second_slot(
    endpoint: HeartbeatEndpoint,
) -> None:
    """Belt-and-suspenders for the single-slot contract: write to id A,
    write to id B, write to id A again — neither id sees the other's
    values."""
    from datetime import timedelta

    a1 = datetime.now().astimezone().replace(microsecond=0)
    b1 = a1 + timedelta(seconds=1)
    a2 = a1 + timedelta(seconds=2)
    endpoint.write("A", a1)
    endpoint.write("B", b1)
    endpoint.write("A", a2)
    assert endpoint.read("A") == a2
    assert endpoint.read("B") == b1
