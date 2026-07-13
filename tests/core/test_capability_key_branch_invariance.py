"""``capability_key`` is invariant under ``LoraEntry.branch`` differences.

P2 §4.3 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

Decision Q4: branch is IN the matcher (so a stack with high_noise is
NOT a cache hit for low_noise — see test_is_stack_match_branch.py) but
OUT of capability_key. The same warm pod serves every branch combination
via ``/lora/set_stack`` swap, so the warm-attach identity hash should
not key off branch.

A regression in either direction is bad:
  - branch IN capability_key → warm pool fragmentation; the same Wan 2.2
    pod cannot serve both an Arcane h+l recipe and a future variant
    swapping branches around without a cold-boot.
  - branch OUT of is_stack_match (separate test file) → wrong-stage LoRA
    silently treated as match.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from kinoforge.core.config import Config, load_config


def _wan_cfg_with_loras(loras: list[dict[str, object]]) -> dict[str, object]:
    """Build a minimal Wan-shape cfg dict with the given LoRA list."""
    return {
        "engine": {"kind": "diffusers", "precision": "fp16"},
        "models": [
            {
                "ref": "hf:Wan-AI/Wan2.2-T2V-A14B:high_noise_model/x.safetensors",
                "kind": "base",
                "target": "checkpoints",
            }
        ],
        "loras": loras,
        "compute": {
            "provider": "runpod",
            "image": "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
            "mode": "pod",
            "requirements": {
                "min_vram_gb": 80,
                "min_cuda": "12.4",
                "max_usd_per_hr": 3.5,
                "disk_gb": 200,
            },
            "lifecycle": {
                "idle_timeout": "2h",
                "job_timeout": "30m",
                "time_buffer": "30m",
                "max_lifetime": "5h",
                "budget": 25.0,
                "heartbeat_interval_s": 30,
            },
        },
        "spec": {"graph": {"nodes": []}},
        "params": {"fps": 24, "num_frames": 81, "steps": 30},
    }


def _load_cfg(root: Path, raw: dict[str, object]) -> Config:
    root.mkdir(parents=True, exist_ok=True)
    p = root / "runpod-comfyui-wan-2_2-14b-t2v.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_config(p)


def test_capability_key_invariant_under_branch_differences(
    tmp_path: Path,
) -> None:
    """Bug: a future edit threads ``branch`` into ``capability_key``,
    fragmenting the warm pool — the same pod cannot serve both a
    high+low Arcane pair and a swapped-branch variant without a
    cold-boot."""
    raw_a = _wan_cfg_with_loras(
        [
            {"ref": "civitai:1@1", "strength": 1.0, "branch": "high_noise"},
            {"ref": "civitai:2@2", "strength": 1.0, "branch": "low_noise"},
        ]
    )
    raw_b = _wan_cfg_with_loras(
        [
            {"ref": "civitai:1@1", "strength": 1.0, "branch": "low_noise"},
            {"ref": "civitai:2@2", "strength": 1.0, "branch": "high_noise"},
        ]
    )
    raw_c = _wan_cfg_with_loras(
        [
            {"ref": "civitai:1@1", "strength": 1.0},  # branch defaults "auto"
            {"ref": "civitai:2@2", "strength": 1.0},
        ]
    )
    key_a = _load_cfg(tmp_path / "a", raw_a).capability_key()
    key_b = _load_cfg(tmp_path / "b", raw_b).capability_key()
    key_c = _load_cfg(tmp_path / "c", raw_c).capability_key()
    assert key_a == key_b == key_c
