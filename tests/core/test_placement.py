"""Behavior: compute.placement is the portable resource block.

``requirements`` described a catalog filter. ``placement`` describes what to
get, which is the thing every provider can honour. The rename is not cosmetic:
``gpu_preference`` becomes ``accelerators`` and ``spot`` arrives from
``InstanceSpec``, so keeping the old name over new semantics would leave the
config surface lying. ``min_cuda`` stays on the block — it looks like a RunPod
knob, but kinoforge filters on it client-side for every enumerating provider.
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


def test_placement_block_populates_the_placement_object():
    cfg = _load(
        {
            "provider": "runpod",
            "image": "i",
            "placement": {
                "accelerators": ["NVIDIA A100 80GB PCIe"],
                "min_vram_gb": 80,
                "disk_gb": 200,
                "spot": True,
                "max_usd_per_hr": 1.75,
            },
        }
    )
    p = cfg.placement()
    assert p.accelerators == ("NVIDIA A100 80GB PCIe",)
    assert (p.min_vram_gb, p.disk_gb, p.spot, p.max_usd_per_hr) == (80, 200, True, 1.75)


def test_placement_defaults_match_the_old_requirements_defaults():
    # Bug caught: a changed default silently re-prices or re-sizes every config
    # that did not set the block. The goldens would catch it too; this names it.
    p = _load({"provider": "runpod", "image": "i"}).placement()
    assert (p.min_vram_gb, p.disk_gb, p.max_usd_per_hr, p.spot) == (
        48,
        100,
        2.20,
        False,
    )
    assert p.min_cuda == "12.8"
    assert p.accelerators == ()
    assert p.accelerator_count == 1


def test_legacy_requirements_block_names_its_replacement():
    with pytest.raises(ConfigError) as exc:
        _load({"provider": "runpod", "image": "i", "requirements": {"min_vram_gb": 48}})
    assert "compute.placement" in str(exc.value)


def test_min_cuda_is_portable_and_lives_on_placement():
    # Bug caught: min_cuda parked in a provider namespace. It reads as a vendor
    # knob because only the vendor APIs constrain CUDA at selection time, but
    # kinoforge filters client-side in core/offers.py::filter_offers over
    # whatever catalog ANY enumerating provider returns — so the floor is
    # portable, and namespacing it would leave every non-RunPod config either
    # unable to express it or carrying a runpod block it has no business having.
    cfg = _load(
        {"provider": "skypilot", "image": "i", "placement": {"min_cuda": "12.0"}}
    )
    assert cfg.placement().min_cuda == "12.0"
    assert cfg.hardware_requirements().min_cuda == "12.0"


def test_min_cuda_in_the_runpod_namespace_is_refused():
    # Bug caught: the namespace key survives as a second spelling of the same
    # floor, so a config can set both and only one of them steers find_offers.
    with pytest.raises(ConfigError) as exc:
        _load(
            {
                "provider": "runpod",
                "image": "i",
                "backend_options": {"runpod": {"min_cuda": "12.1"}},
            }
        )
    assert "min_cuda" in str(exc.value)


def test_legacy_gpu_preference_key_under_placement_is_refused():
    # Bug caught: extra="ignore" (pydantic's default) would drop the renamed key
    # and hand find_offers an EMPTY accelerator list — the operator's GPU
    # ordering silently gone, which is the failure mode S1 exists to end.
    with pytest.raises(ConfigError) as exc:
        _load(
            {
                "provider": "runpod",
                "image": "i",
                "placement": {"gpu_preference": ["RTX 4090"]},
            }
        )
    assert "compute.placement.accelerators" in str(exc.value)


def test_hardware_requirements_shim_sources_gpu_preference_from_accelerators():
    # The shim keeps find_offers working until S4 deletes it.
    cfg = _load(
        {"provider": "runpod", "image": "i", "placement": {"accelerators": ["H100"]}}
    )
    assert cfg.hardware_requirements().gpu_preference == ("H100",)


def test_hardware_requirements_shim_sources_min_cuda_from_placement():
    # Bug caught: the shim keeps returning the "12.8" default, so a config that
    # pinned a lower CUDA floor to admit older SKUs silently loses every offer —
    # SkyPilot's catalog reports a flat cuda=12.0, so a 12.8 floor empties it.
    cfg = _load(
        {"provider": "skypilot", "image": "i", "placement": {"min_cuda": "12.0"}}
    )
    assert cfg.hardware_requirements().min_cuda == "12.0"


def test_hardware_requirements_min_cuda_default_needs_no_provider_branch():
    for provider in ("runpod", "skypilot", "modal"):
        cfg = _load({"provider": provider, "image": "i"})
        assert cfg.hardware_requirements().min_cuda == "12.8", provider


def test_instance_spec_no_longer_carries_spot():
    from kinoforge.core.interfaces import InstanceSpec

    assert "spot" not in InstanceSpec.__dataclass_fields__


def test_built_spec_carries_the_placement_block():
    # Bug caught: Placement exists on the dataclass but build_instance_spec
    # never populates it, so SkyPilot's use_spot branch reads the default False
    # for every config that asked for spot.
    from kinoforge.core.interfaces import Lifecycle, RenderedProvision
    from kinoforge.core.spec_builder import build_instance_spec

    cfg = _load({"provider": "skypilot", "image": "i", "placement": {"spot": True}})
    spec = build_instance_spec(
        cfg=cfg,
        rendered=RenderedProvision(
            script="", run_cmd=[], image="i", ports=[], env_required=[]
        ),
        offer=None,
        engine_name="diffusers",
        key_hash="k",
        image="i",
        lifecycle=Lifecycle(),
        env={},
        run_id="r",
    )
    assert spec.placement.spot is True
