"""Behavior: the Modal T2V config loads and resolves to a ModalProvider."""

from pathlib import Path

from kinoforge._adapters import build_provider_for
from kinoforge.core.config import load_config
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.modal._catalog import modal_offers

CFG = Path("examples/configs/modal-wan-t2v-1_3b.yaml")
CFG_A14B = Path("examples/configs/modal-wan-t2v-14b-2_2.yaml")


def test_config_resolves_to_modal_provider():
    cfg = load_config(CFG)
    assert cfg.compute is not None
    assert cfg.compute.provider == "modal"
    assert cfg.compute.cloud is None  # MUST omit cloud (non-sky)
    provider = build_provider_for(cfg)
    assert isinstance(provider, ModalProvider)


def test_config_targets_wan21_1_3b_cheaply():
    cfg = load_config(CFG)
    assert cfg.compute is not None
    assert cfg.compute.requirements.min_vram_gb <= 24
    assert any("Wan2.1-T2V-1.3B" in m.ref for m in cfg.models)


def test_a14b_config_resolves_to_modal_provider():
    cfg = load_config(CFG_A14B)
    assert cfg.compute is not None
    assert cfg.compute.provider == "modal"
    assert cfg.compute.cloud is None  # non-sky
    assert isinstance(build_provider_for(cfg), ModalProvider)


def test_a14b_config_targets_80gb_wan22():
    cfg = load_config(CFG_A14B)
    assert cfg.compute is not None
    assert cfg.compute.requirements.min_vram_gb == 80
    assert any("Wan2.2-T2V-A14B" in m.ref for m in cfg.models)
    assert cfg.spec is not None
    model = cfg.spec["model"]
    assert model and model.lower() != "unknown"


def test_a14b_config_selects_80gb_offer_first():
    cfg = load_config(CFG_A14B)
    assert cfg.compute is not None
    offers = modal_offers(cfg.hardware_requirements())
    assert offers, "expected at least one 80GB offer"
    assert offers[0].vram_gb >= 80
    assert offers[0].gpu_type == "A100-80GB"  # cheapest 80GB, first in preference
