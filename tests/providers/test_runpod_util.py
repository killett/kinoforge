"""Unit tests for RunPodGraphQLUtilEndpoint (C26 Task 3)."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

_OK_RESP_4_GPU = {
    "data": {
        "pod": {
            "id": "p1",
            "runtime": {
                "uptimeInSeconds": 1234,
                "gpus": [
                    {
                        "id": "g1",
                        "gpuUtilPercent": 10.0,
                        "memoryUtilPercent": 50.0,
                    },
                    {
                        "id": "g2",
                        "gpuUtilPercent": 80.0,
                        "memoryUtilPercent": 60.0,
                    },
                    {
                        "id": "g3",
                        "gpuUtilPercent": 5.0,
                        "memoryUtilPercent": 40.0,
                    },
                    {
                        "id": "g4",
                        "gpuUtilPercent": 0.0,
                        "memoryUtilPercent": 20.0,
                    },
                ],
                "container": {"cpuPercent": 25.5, "memoryPercent": 78.0},
            },
        }
    }
}


class _SpyPost:
    def __init__(self, resp: dict[str, Any]) -> None:
        self.resp = resp
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, payload))
        return self.resp


def test_read_util_returns_max_gpu_across_devices() -> None:
    spy = _SpyPost(_OK_RESP_4_GPU)
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    snap = ep.read_util("p1")
    assert snap is not None
    assert snap.gpu_util_percent == 80.0


def test_read_util_returns_other_fields() -> None:
    spy = _SpyPost(_OK_RESP_4_GPU)
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    snap = ep.read_util("p1")
    assert snap is not None
    assert snap.cpu_percent == 25.5
    assert snap.memory_percent == 78.0
    assert snap.uptime_seconds == 1234
    assert snap.disk_percent is None


def test_read_util_returns_none_when_pod_gone() -> None:
    spy = _SpyPost({"data": {"pod": None}})
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    assert ep.read_util("p1") is None


def test_read_util_returns_partial_when_container_null() -> None:
    resp: dict[str, Any] = {
        "data": {
            "pod": {
                "id": "p1",
                "runtime": {
                    "uptimeInSeconds": 5,
                    "gpus": [
                        {
                            "id": "g1",
                            "gpuUtilPercent": 10.0,
                            "memoryUtilPercent": 50.0,
                        }
                    ],
                    "container": None,
                },
            }
        }
    }
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=_SpyPost(resp))
    snap = ep.read_util("p1")
    assert snap is not None
    assert snap.gpu_util_percent == 10.0
    assert snap.cpu_percent is None
    assert snap.memory_percent is None
    assert snap.uptime_seconds == 5


def test_read_util_handles_empty_gpus_array() -> None:
    resp: dict[str, Any] = {
        "data": {
            "pod": {
                "id": "p1",
                "runtime": {
                    "uptimeInSeconds": 5,
                    "gpus": [],
                    "container": {"cpuPercent": 25.0, "memoryPercent": 50.0},
                },
            }
        }
    }
    snap = RunPodGraphQLUtilEndpoint(api_key="k", http_post=_SpyPost(resp)).read_util(
        "p1"
    )
    assert snap is not None
    assert snap.gpu_util_percent is None


def test_read_util_raises_transport_error_on_graphql_errors() -> None:
    spy = _SpyPost({"errors": [{"message": "rate limited"}]})
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    with pytest.raises(TransportError, match="rate limited"):
        ep.read_util("p1")


def test_read_util_returns_none_when_runtime_null_during_boot() -> None:
    """Task 1 probe confirmed runtime=null is a valid early-boot state."""
    resp: dict[str, Any] = {"data": {"pod": {"id": "p1", "runtime": None}}}
    snap = RunPodGraphQLUtilEndpoint(api_key="k", http_post=_SpyPost(resp)).read_util(
        "p1"
    )
    assert snap is None


def test_read_util_passes_single_call_with_inlined_pod_id() -> None:
    """One GraphQL round trip per read_util; payload uses inlined podId."""
    spy = _SpyPost(_OK_RESP_4_GPU)
    ep = RunPodGraphQLUtilEndpoint(api_key="k", http_post=spy)
    ep.read_util("p1")
    assert len(spy.calls) == 1
    url, payload = spy.calls[0]
    assert url.endswith("/graphql")
    assert "query" in payload
    assert '"p1"' in payload["query"]
