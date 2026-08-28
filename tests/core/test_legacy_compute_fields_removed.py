"""Behavior: the vendor-specific compute keys are gone, and their removal is loud.

A silent drop would be worse than the F5 status quo: the operator's pin would
stop working with no message at all. Each removed key must name its
replacement path.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ConfigError

_BASE: dict[str, Any] = {
    "engine": {"kind": "diffusers", "precision": "fp16"},
    "spec": {"model": "m", "precision": "bf16"},
    "models": [{"kind": "base", "ref": "hf:org/repo", "target": "diffusion_models"}],
}


def _load(compute: dict[str, Any]) -> Config:
    return Config.model_validate({**_BASE, "compute": compute})


def test_legacy_cloud_key_names_its_replacement():
    with pytest.raises(ConfigError) as exc:
        _load({"provider": "skypilot", "image": "i", "cloud": ["lambda"]})
    assert "compute.backend_options.skypilot.clouds" in str(exc.value)


def test_legacy_cloud_type_key_names_its_replacement():
    with pytest.raises(ConfigError) as exc:
        _load({"provider": "runpod", "image": "i", "cloud_type": "secure"})
    assert "compute.backend_options.runpod.cloud_type" in str(exc.value)


def test_capacity_wait_is_runpod_scoped():
    # Bug caught: capacity_wait_s stays on Lifecycle and every provider keeps
    # paying a RunPod-shaped retry loop, which is what made it look portable.
    from kinoforge.core.interfaces import Lifecycle

    assert not hasattr(Lifecycle(), "capacity_wait_s")


def test_runpod_capacity_wait_reaches_the_orchestrator_from_the_namespace():
    from kinoforge._adapters import build_capacity_wait_for

    cfg = _load(
        {
            "provider": "runpod",
            "image": "i",
            "backend_options": {"runpod": {"capacity_wait_s": 42.0}},
        }
    )
    assert build_capacity_wait_for(cfg) == 42.0


def test_runpod_capacity_wait_defaults_when_the_namespace_is_absent():
    # Bug caught: the namespace read returns 0.0 when nobody wrote a
    # backend_options block, silently deleting RunPod's capacity retry for
    # every config that never opted in (i.e. all of them today).
    from kinoforge._adapters import build_capacity_wait_for

    assert build_capacity_wait_for(_load({"provider": "runpod", "image": "i"})) == 300.0


def test_non_runpod_provider_gets_no_capacity_wait():
    from kinoforge._adapters import build_capacity_wait_for

    assert build_capacity_wait_for(_load({"provider": "skypilot", "image": "i"})) == 0.0


def test_modal_provider_gets_no_capacity_wait():
    # The other half of the intended behaviour change: Modal schedules itself,
    # so it must not inherit RunPod's retry loop either.
    from kinoforge._adapters import build_capacity_wait_for

    assert build_capacity_wait_for(_load({"provider": "modal", "image": "i"})) == 0.0
