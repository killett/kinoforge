"""Config migration tests: legacy ``models: [{kind: lora, ...}]`` →
new top-level ``loras:`` block."""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from pydantic import ValidationError

from kinoforge.core.config import Config, ModelEntry


def _base_cfg_dict(extra: dict[str, Any]) -> dict[str, Any]:
    """Return a minimum-viable cfg dict to which a test merges ``extra``."""
    base: dict[str, Any] = {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
        ],
        "compute": {
            "provider": "local",
            "image": "fake:latest",
            "lifecycle": {"budget": 1.0},
        },
    }
    base.update(extra)
    return base


def test_legacy_models_kind_lora_promotes_to_loras_block() -> None:
    """Bug: the promoter silently drops legacy LoRAs, capability_key
    derivation no longer includes the LoRA refs, warm-reuse routes the
    user to the wrong pool of pods."""
    cfg_dict = _base_cfg_dict(
        {
            "models": [
                {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
                {"ref": "civitai:1@2", "kind": "lora", "target": "loras"},
                {
                    "ref": "hf:Org/y:foo.safetensors",
                    "kind": "lora",
                    "target": "loras",
                },
            ],
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cfg = Config.model_validate(cfg_dict)
    assert len(cfg.loras) == 2
    assert cfg.loras[0].ref == "civitai:1@2"
    assert cfg.loras[0].strength == 1.0
    assert cfg.loras[1].ref == "hf:Org/y:foo.safetensors"
    assert all(m.kind != "lora" for m in cfg.models)  # type: ignore[comparison-overlap]


def test_legacy_promotion_emits_deprecation_warning() -> None:
    """Bug: the promoter silently auto-fixes legacy cfgs forever →
    operators never learn to update the cfg shape, the transition window
    never closes."""
    cfg_dict = _base_cfg_dict(
        {
            "models": [
                {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
                {"ref": "civitai:1@2", "kind": "lora", "target": "loras"},
            ],
        }
    )
    with pytest.warns(DeprecationWarning, match="legacy.*kind: lora.*promoted 1"):
        Config.model_validate(cfg_dict)


def test_new_shape_loads_without_warnings() -> None:
    """Bug: false-positive warnings on already-migrated cfgs."""
    cfg_dict = _base_cfg_dict(
        {
            "loras": [
                {"ref": "civitai:1@2"},
                {"ref": "hf:Org/y:foo.safetensors", "strength": 0.7},
            ],
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = Config.model_validate(cfg_dict)
    assert len(cfg.loras) == 2
    assert cfg.loras[1].strength == 0.7


def test_modelentry_rejects_kind_lora_directly() -> None:
    """Bug: bypassing the Config-level promoter by constructing a
    ModelEntry with kind='lora' should fail at the Literal level —
    otherwise we silently leak a kind=lora entry into Config.models and
    capability_key derivation reads the wrong list."""
    with pytest.raises(ValidationError):
        ModelEntry(ref="civitai:1@2", kind="lora", target="loras")  # type: ignore[arg-type]


def test_legacy_promotion_carries_sha256_through() -> None:
    cfg_dict = _base_cfg_dict(
        {
            "models": [
                {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
                {
                    "ref": "civitai:1@2",
                    "kind": "lora",
                    "target": "loras",
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cfg = Config.model_validate(cfg_dict)
    assert cfg.loras[0].sha256 == "a" * 64


def test_loras_block_and_legacy_models_kind_lora_merge_explicit_first() -> None:
    """Bug: a cfg with BOTH the new ``loras:`` block AND legacy
    ``kind: lora`` entries — the explicit (new-shape) entries must win
    on ordering so the cfg author's intent is preserved. set_adapters
    order matters."""
    cfg_dict = _base_cfg_dict(
        {
            "loras": [{"ref": "civitai:99@100"}],
            "models": [
                {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
                {"ref": "civitai:1@2", "kind": "lora", "target": "loras"},
            ],
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cfg = Config.model_validate(cfg_dict)
    assert [lo.ref for lo in cfg.loras] == ["civitai:99@100", "civitai:1@2"]
