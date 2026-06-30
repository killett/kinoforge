"""Tests for the cfg-time rejection of cfg.upscale.engine == 'seedvr2'.

Uses an isolated CheckRegistry containing only SeedVR2ExtrasPendingCheck
so the assertions exercise this check's contract without coupling to
the NETWORK/PREFLIGHT checks that fire against the fake refs in the
test cfgs.
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ValidationError
from kinoforge.validation import validate_for_generate
from kinoforge.validation.checks.upscale import SeedVR2ExtrasPendingCheck
from kinoforge.validation.registry import CheckRegistry


def _isolated_registry() -> CheckRegistry:
    reg = CheckRegistry()
    reg.register(SeedVR2ExtrasPendingCheck())
    return reg


def _wan_only_cfg() -> Config:
    return Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [
                {
                    "kind": "base",
                    "ref": "hf:Wan-AI/Wan2.2-T2V",
                    "target": "diffusion_models",
                }
            ],
            "compute": {"provider": "fake", "image": "fake:latest"},
        }
    )


def _spandrel_cfg() -> Config:
    return Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [
                {
                    "kind": "base",
                    "ref": "hf:Wan-AI/Wan2.2-T2V",
                    "target": "diffusion_models",
                }
            ],
            "compute": {"provider": "fake", "image": "fake:latest"},
            "upscale": {"engine": "spandrel", "scale": "2x"},
        }
    )


def _seedvr2_cfg() -> Config:
    return Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [
                {
                    "kind": "base",
                    "ref": "hf:Wan-AI/Wan2.2-T2V",
                    "target": "diffusion_models",
                }
            ],
            "compute": {"provider": "fake", "image": "fake:latest"},
            "upscale": {
                "engine": "seedvr2",
                "scale": "2x",
                "seedvr2": {"variant": "3B", "precision": "fp8"},
            },
        }
    )


def test_seedvr2_cfg_rejected_with_extras_hint() -> None:
    # Bug caught: a cfg referencing the extras-stub engine slips past
    # cfg-time validation and burns cold-boot budget on a pod whose
    # bootstrap will crash at the composed render_provision step.
    with pytest.raises(ValidationError, match=r"kinoforge\[seedvr\]"):
        validate_for_generate(_seedvr2_cfg(), registry=_isolated_registry())


def test_spandrel_cfg_passes() -> None:
    # Bug caught: a too-eager regex rejects the v1 default cfg too.
    validate_for_generate(_spandrel_cfg(), registry=_isolated_registry())


def test_wan_only_cfg_passes() -> None:
    # Bug caught: rejection logic doesn't gate on cfg.upscale being set
    # and pure-t2v cfgs start failing validation post-pivot.
    validate_for_generate(_wan_only_cfg(), registry=_isolated_registry())


def test_check_is_preflight_category() -> None:
    # Bug caught: check registered as STATIC fires at load-time, before
    # the operator's `kinoforge generate` invocation, surfacing the
    # rejection at the wrong CLI boundary. Plan AC: PREFLIGHT, not STATIC.
    from kinoforge.validation.protocol import CheckCategory

    assert SeedVR2ExtrasPendingCheck().category == CheckCategory.PREFLIGHT
