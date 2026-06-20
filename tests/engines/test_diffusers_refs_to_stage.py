"""Tests for ``DiffusersEngine.refs_to_stage`` — Task 7.5 (plan amendment 2026-06-19).

The DiffusersEngine pod self-fetches model weights at server-start time
via ``diffusers.WanPipeline.from_pretrained(MODEL_ID)``. The workspace
download driven by ``provisioner.download_all`` was therefore pure waste
— ~70 GB of duplicate aria2c traffic for the Wan 2.2 14B cfg, paid for
by ~$0.50-0.85 of pod-idle time per smoke leg while the workspace was
busy. ``refs_to_stage`` returning ``[]`` is the canonical opt-out.
"""

from __future__ import annotations

from kinoforge.core.interfaces import Artifact
from kinoforge.engines.diffusers import DiffusersEngine


def test_diffusers_engine_refs_to_stage_returns_empty_for_any_input() -> None:
    """DiffusersEngine reports no artifacts to stage locally.

    Asserts the override exists and unconditionally returns ``[]`` —
    regardless of how many artifacts the provisioner merged. The pod's
    ``wan_t2v_server.py`` and any future diffusers serving module are
    expected to self-fetch from HF; the workspace stays clean.
    """
    engine = DiffusersEngine()
    artifacts = [
        Artifact(filename="shard-1.safetensors", url="hf:Wan-AI/Wan2.2-T2V-A14B"),
        Artifact(filename="shard-2.safetensors", url="hf:Wan-AI/Wan2.2-T2V-A14B"),
    ]

    result = engine.refs_to_stage(artifacts)

    assert result == [], (
        "DiffusersEngine.refs_to_stage must skip workspace-side staging "
        "(pod self-fetches via from_pretrained); see Task 7.5 amendment."
    )


def test_diffusers_engine_refs_to_stage_returns_empty_on_empty_input() -> None:
    """Empty input produces empty output — boundary check.

    Locks down that an empty merged list survives the override without
    raising; the provisioner emits an empty list when ``cfg.models`` is
    empty (a legal config shape for engines that don't need any models).
    """
    engine = DiffusersEngine()

    assert engine.refs_to_stage([]) == []
