"""Parametrized contract tests for every registered UpscalerEngine.

This test exercises the safe (non-raising) ABC surface — name,
requires_compute, requires_local_weights, supported_scales,
model_identity({}). Engines in extras-stub mode (e.g. SeedVR2 pre-Phase 2)
still satisfy this contract because the stubs only raise from the
heavyweight methods (provision, upscale, render_provision, validate_spec)
— those are tested separately.
"""

from __future__ import annotations

import pytest

import kinoforge._adapters  # noqa: F401 — self-register every engine
from kinoforge.core import registry
from kinoforge.core.interfaces import UpscalerEngine
from kinoforge.core.scale_target import ScaleTarget


def _all_registered() -> list[str]:
    return registry.upscaler_names()


@pytest.mark.parametrize("name", _all_registered())
def test_engine_class_attrs_satisfy_contract(name: str) -> None:
    # Bug caught: a future engine omits `requires_compute` or sets
    # `supported_scales = []` (list, not tuple) — orchestrator scan
    # paths assume tuple membership and break opaquely.
    engine = registry.get_upscaler(name)()
    assert isinstance(engine, UpscalerEngine)
    assert engine.name == name
    assert isinstance(engine.requires_compute, bool)
    assert isinstance(engine.requires_local_weights, bool)
    assert isinstance(engine.supported_scales, tuple)
    for s in engine.supported_scales:
        assert isinstance(s, ScaleTarget)


@pytest.mark.parametrize("name", _all_registered())
def test_model_identity_pure_function_on_empty_cfg(name: str) -> None:
    # Bug caught: model_identity raises on missing cfg keys instead of
    # returning empty string. The output-sink filename schema calls
    # this on every job and a raise turns into "unknown" slugs at best,
    # a stage-fault at worst.
    engine = registry.get_upscaler(name)()
    result = engine.model_identity({})
    assert isinstance(result, str)


@pytest.mark.parametrize("name", _all_registered())
def test_model_identity_pure_function_on_other_engine_cfg(name: str) -> None:
    # Bug caught: model_identity hardcodes its own engine name as a
    # cfg-block lookup key (e.g. literal "seedvr2") instead of
    # self.name — when reading another engine's cfg by accident the
    # method explodes instead of returning empty string.
    engine = registry.get_upscaler(name)()
    cfg: dict[str, object] = {"upscale": {"engine": "other-engine", "other-engine": {}}}
    result = engine.model_identity(cfg)
    assert isinstance(result, str)
