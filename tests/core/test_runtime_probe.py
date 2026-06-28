"""Unit tests for RuntimeProbe dataclass + ComputeProvider.probe_runtime default."""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from kinoforge.core.interfaces import ComputeProvider
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.runpod import RunPodProvider
from kinoforge.providers.skypilot import SkyPilotProvider


def test_runtime_probe_is_frozen() -> None:
    """A RuntimeProbe instance cannot be mutated after construction."""
    probe = RuntimeProbe(
        pod_id="abc",
        found=True,
        container_uptime_s=10.0,
        gpu_util_pct=50.0,
        cpu_pct=15.0,
        cost_per_hr=0.40,
        probed_at_local="2026-06-28T12:00:00",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        probe.gpu_util_pct = 99.0  # type: ignore[misc]


def test_runtime_probe_required_fields() -> None:
    """All seven required fields must be present; missing one raises TypeError."""
    with pytest.raises(TypeError):
        RuntimeProbe(  # type: ignore[call-arg]
            pod_id="abc",
            found=True,
            container_uptime_s=None,
            gpu_util_pct=None,
            cpu_pct=None,
            cost_per_hr=None,
        )


def test_runtime_probe_error_defaults_to_none() -> None:
    """``error`` is optional and defaults to None."""
    probe = RuntimeProbe(
        pod_id="abc",
        found=True,
        container_uptime_s=10.0,
        gpu_util_pct=50.0,
        cpu_pct=15.0,
        cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:00",
    )
    assert probe.error is None


def test_runtime_probe_not_found_shape() -> None:
    """``found=False`` is allowed with all util fields None — 'pod gone' state."""
    probe = RuntimeProbe(
        pod_id="dead-pod",
        found=False,
        container_uptime_s=None,
        gpu_util_pct=None,
        cpu_pct=None,
        cost_per_hr=None,
        probed_at_local="2026-06-28T12:00:00",
    )
    assert probe.found is False
    assert probe.gpu_util_pct is None


def test_abc_default_probe_returns_none_for_local() -> None:
    """LocalProvider inherits ABC default — substrate-missing signal."""
    assert LocalProvider().probe_runtime("anything") is None


def test_abc_default_probe_returns_none_for_skypilot() -> None:
    """SkyPilotProvider inherits ABC default — covers Lambda + Vast."""
    assert SkyPilotProvider().probe_runtime("anything") is None


def test_abc_probe_runtime_signature() -> None:
    """ABC method is callable with a single str arg and returns Optional[RuntimeProbe]."""
    sig = inspect.signature(ComputeProvider.probe_runtime)
    params = list(sig.parameters.values())
    assert len(params) == 2  # self + pod_id
    assert params[1].name == "pod_id"
    annotations = inspect.get_annotations(
        ComputeProvider.probe_runtime,
        eval_str=True,
        locals={"RuntimeProbe": RuntimeProbe},
    )
    assert annotations["pod_id"] is str
    assert annotations["return"] == (RuntimeProbe | None)


# --- Task 2: RunPodProvider.probe_runtime override --------------------------

from datetime import datetime  # noqa: E402
from typing import Any  # noqa: E402

from kinoforge.core.errors import TransportError  # noqa: E402
from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint  # noqa: E402


def _make_endpoint(response: dict[str, Any]) -> RunPodGraphQLUtilEndpoint:
    """Build an endpoint with a stubbed HTTP closure returning ``response``."""
    return RunPodGraphQLUtilEndpoint(
        api_key="test-key",
        http_post=lambda url, payload: response,
    )


def test_runpod_endpoint_probe_pod_404_returns_not_found() -> None:
    """``data.pod = null`` (RunPod's 404 shape) → (False, None)."""
    endpoint = _make_endpoint({"data": {"pod": None}})
    found, snapshot = endpoint.probe("dead-pod")
    assert found is False
    assert snapshot is None


def test_runpod_endpoint_probe_runtime_null_returns_found_no_snapshot() -> None:
    """Pod exists but runtime not started (early boot) → (True, None)."""
    endpoint = _make_endpoint({"data": {"pod": {"runtime": None}}})
    found, snapshot = endpoint.probe("booting-pod")
    assert found is True
    assert snapshot is None


def test_runpod_endpoint_probe_runtime_populated_returns_snapshot() -> None:
    """Pod + runtime present → (True, UtilSnapshot)."""
    endpoint = _make_endpoint(
        {
            "data": {
                "pod": {
                    "runtime": {
                        "uptimeInSeconds": 600,
                        "gpus": [
                            {
                                "id": "g0",
                                "gpuUtilPercent": 75.0,
                                "memoryUtilPercent": 50.0,
                            }
                        ],
                        "container": {"cpuPercent": 12.0, "memoryPercent": 30.0},
                    }
                }
            }
        }
    )
    found, snapshot = endpoint.probe("live-pod")
    assert found is True
    assert snapshot is not None
    assert snapshot.gpu_util_percent == 75.0
    assert snapshot.cpu_percent == 12.0
    assert snapshot.uptime_seconds == 600


def test_runpod_provider_probe_runtime_404() -> None:
    """RunPodProvider.probe_runtime returns RuntimeProbe(found=False) for 404."""
    provider = RunPodProvider()
    provider._util_endpoint = _make_endpoint({"data": {"pod": None}})
    probe = provider.probe_runtime("dead-pod")
    assert probe is not None
    assert probe.found is False
    assert probe.gpu_util_pct is None
    assert probe.container_uptime_s is None


def test_runpod_provider_probe_runtime_early_boot() -> None:
    """RunPodProvider.probe_runtime returns found=True with util=None for runtime=null."""
    provider = RunPodProvider()
    provider._util_endpoint = _make_endpoint({"data": {"pod": {"runtime": None}}})
    probe = provider.probe_runtime("booting-pod")
    assert probe is not None
    assert probe.found is True
    assert probe.gpu_util_pct is None
    assert probe.container_uptime_s is None


def test_runpod_provider_probe_runtime_live() -> None:
    """RunPodProvider.probe_runtime returns full RuntimeProbe for healthy pod."""
    provider = RunPodProvider()
    provider._util_endpoint = _make_endpoint(
        {
            "data": {
                "pod": {
                    "runtime": {
                        "uptimeInSeconds": 600,
                        "gpus": [
                            {
                                "id": "g0",
                                "gpuUtilPercent": 75.0,
                                "memoryUtilPercent": 50.0,
                            }
                        ],
                        "container": {"cpuPercent": 12.0, "memoryPercent": 30.0},
                    }
                }
            }
        }
    )
    probe = provider.probe_runtime("live-pod")
    assert probe is not None
    assert probe.found is True
    assert probe.gpu_util_pct == 75.0
    assert probe.cpu_pct == 12.0
    assert probe.container_uptime_s == 600.0


def test_runpod_provider_probe_runtime_reraises_transport_error() -> None:
    """Network/auth failure → TransportError propagates (NOT swallowed)."""

    def raising_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise TransportError("simulated network failure")

    provider = RunPodProvider()
    provider._util_endpoint = RunPodGraphQLUtilEndpoint(
        api_key="test-key", http_post=raising_post
    )
    with pytest.raises(TransportError):
        provider.probe_runtime("any-pod")


def test_runpod_provider_probe_runtime_local_timezone() -> None:
    """probed_at_local uses local-TZ ISO format, NOT UTC (project rule)."""
    provider = RunPodProvider()
    provider._util_endpoint = _make_endpoint({"data": {"pod": None}})
    before = datetime.now().isoformat()
    probe = provider.probe_runtime("any-pod")
    after = datetime.now().isoformat()
    assert probe is not None
    assert before <= probe.probed_at_local <= after
    assert "+" not in probe.probed_at_local and "Z" not in probe.probed_at_local
