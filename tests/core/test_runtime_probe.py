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


def test_runpod_provider_inherits_default_before_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Until Task 2 lands, RunPodProvider's probe_runtime is the ABC default.

    This test deliberately constructs a RunPodProvider WITHOUT triggering its
    GraphQL substrate and asserts that probe_runtime returns None. Once Task 2
    overrides probe_runtime, delete this test.
    """
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-not-used")
    provider = RunPodProvider()
    assert provider.probe_runtime("anything") is None


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
