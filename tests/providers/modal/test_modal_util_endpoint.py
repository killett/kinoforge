"""Offline: ModalUtilEndpoint maps /util JSON to a UtilSnapshot.

Bugs caught: warm pod whose endpoint is unresolved must yield None (not crash);
a 500 must surface as TransportError so consumers can tolerate; a 404 (pod gone)
is None per the contract.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.core.util_endpoints import UtilSnapshot, UtilSnapshotEndpoint
from kinoforge.providers.modal.util import ModalUtilEndpoint


class _Resp:
    def __init__(self, status: int, body: dict[str, object] | None) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict[str, object]:
        return self._body or {}


_FULL: dict[str, object] = {
    "gpu_util_percent": 73.0,
    "cpu_percent": 12.0,
    "memory_percent": 40.0,
    "disk_percent": 5.0,
    "uptime_seconds": 88,
}


def _ep(
    status: int = 200,
    body: dict[str, object] | None = None,
    url: str | None = "https://x.modal.run",
) -> ModalUtilEndpoint:
    return ModalUtilEndpoint(
        resolve_endpoint=lambda _id: url,
        http_get=lambda _u: _Resp(status, body if body is not None else _FULL),
    )


def test_satisfies_protocol() -> None:
    assert isinstance(_ep(), UtilSnapshotEndpoint)


def test_maps_full_body_to_snapshot() -> None:
    snap = _ep().read_util("run-x")
    assert isinstance(snap, UtilSnapshot)
    assert snap.gpu_util_percent == 73.0
    assert snap.cpu_percent == 12.0
    assert snap.memory_percent == 40.0
    assert snap.disk_percent == 5.0
    assert snap.uptime_seconds == 88


def test_unresolved_endpoint_is_none_no_http() -> None:
    called = {"n": 0}

    def _boom(_u):  # must not be called
        called["n"] += 1
        raise AssertionError("HTTP called despite unresolved endpoint")

    ep = ModalUtilEndpoint(resolve_endpoint=lambda _id: None, http_get=_boom)
    assert ep.read_util("gone") is None
    assert called["n"] == 0


def test_404_is_none() -> None:
    assert _ep(status=404, body={}).read_util("run-x") is None


def test_500_raises_transport_error() -> None:
    with pytest.raises(TransportError):
        _ep(status=500, body={}).read_util("run-x")


def test_missing_fields_become_none() -> None:
    snap = _ep(body={"gpu_util_percent": 50.0}).read_util("run-x")
    assert snap is not None
    assert snap.gpu_util_percent == 50.0
    assert snap.cpu_percent is None
    assert snap.uptime_seconds is None
