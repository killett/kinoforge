"""Behavior: the Modal T2V config loads and resolves to a ModalProvider."""

from pathlib import Path

from kinoforge._adapters import build_provider_for
from kinoforge.core.config import load_config
from kinoforge.providers.modal import ModalProvider

CFG = Path("examples/configs/modal-wan-t2v-1_3b.yaml")


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
