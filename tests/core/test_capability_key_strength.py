"""Capability key invariants for the P1 loras block.

P1-Identity: ``strength`` is mutable per-run and MUST NOT enter the
``capability_key`` hash material. Two cfgs identical in refs but
differing in strengths must derive the same key — otherwise warm-reuse
routes strength-tweak iterations to a fresh cold-boot.
"""

from __future__ import annotations

import warnings
from typing import Any

from kinoforge.core.config import Config


def _cfg(loras_block: list[dict[str, Any]] | None = None) -> Config:
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
    if loras_block is not None:
        base["loras"] = loras_block
    return Config.model_validate(base)


def test_capability_key_strength_invariant() -> None:
    """Bug: a future edit folds strength into the hash material →
    users re-running with strength=0.7 vs 1.0 cold-boot a fresh pod
    instead of reusing the warm one."""
    a = _cfg([{"ref": "civitai:1@2", "strength": 1.0}])
    b = _cfg([{"ref": "civitai:1@2", "strength": 0.5}])
    assert a.capability_key() == b.capability_key()


def test_capability_key_changes_when_ref_set_changes() -> None:
    """Bug: ref-set is silently dropped from the hash → different LoRA
    stacks alias to the same warm pool."""
    a = _cfg([{"ref": "civitai:1@2"}])
    b = _cfg([{"ref": "civitai:99@100"}])
    assert a.capability_key() != b.capability_key()


def test_capability_key_stable_across_legacy_to_new_shape_migration() -> None:
    """Bug: post-migration cfgs hash differently than pre-migration
    cfgs with the same refs — every operator who migrates their cfg
    invalidates their warm pool."""
    base_legacy: dict[str, Any] = {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
            {"ref": "civitai:1@2", "kind": "lora", "target": "loras"},
        ],
        "compute": {
            "provider": "local",
            "image": "fake:latest",
            "lifecycle": {"budget": 1.0},
        },
    }
    base_new: dict[str, Any] = {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
        ],
        "loras": [{"ref": "civitai:1@2"}],
        "compute": {
            "provider": "local",
            "image": "fake:latest",
            "lifecycle": {"budget": 1.0},
        },
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legacy_cfg = Config.model_validate(base_legacy)
    new_cfg = Config.model_validate(base_new)
    assert legacy_cfg.capability_key() == new_cfg.capability_key()


def test_capability_key_order_of_loras_matters() -> None:
    """Bug: a future edit sorts loras before hashing → swap order
    silently aliases to the same key, but set_adapters order affects
    output."""
    a = _cfg([{"ref": "civitai:1@2"}, {"ref": "civitai:3@4"}])
    b = _cfg([{"ref": "civitai:3@4"}, {"ref": "civitai:1@2"}])
    assert a.capability_key() != b.capability_key()
