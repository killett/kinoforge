"""Unit tests for kinoforge.core.util_endpoints (C26 Task 2)."""

from __future__ import annotations

import pytest

from kinoforge.core.util_endpoints import (
    UtilSnapshot,
    UtilSnapshotEndpoint,
    provider_util_supported,
)


def test_util_snapshot_is_frozen() -> None:
    snap = UtilSnapshot(
        gpu_util_percent=10.0,
        cpu_percent=20.0,
        memory_percent=30.0,
        disk_percent=40.0,
        uptime_seconds=50,
    )
    with pytest.raises((AttributeError, Exception)):
        snap.gpu_util_percent = 99.0  # type: ignore[misc]


def test_util_snapshot_fields_default_none() -> None:
    snap = UtilSnapshot(
        gpu_util_percent=None,
        cpu_percent=None,
        memory_percent=None,
        disk_percent=None,
        uptime_seconds=None,
    )
    assert snap.gpu_util_percent is None
    assert snap.cpu_percent is None
    assert snap.memory_percent is None
    assert snap.disk_percent is None
    assert snap.uptime_seconds is None


def test_util_snapshot_endpoint_protocol_is_runtime_checkable() -> None:
    class _Fake:
        def read_util(self, instance_id: str) -> UtilSnapshot | None:
            return None

    assert isinstance(_Fake(), UtilSnapshotEndpoint)


def test_util_snapshot_endpoint_rejects_missing_method() -> None:
    class _Wrong:
        pass

    assert not isinstance(_Wrong(), UtilSnapshotEndpoint)


def test_provider_util_supported_known_providers() -> None:
    assert provider_util_supported("runpod") is True
    assert provider_util_supported("local") is True


def test_provider_util_supported_unknown_providers() -> None:
    assert provider_util_supported("skypilot") is False
    assert provider_util_supported("bedrock") is False
    assert provider_util_supported("") is False
