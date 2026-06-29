"""Tests for DiffusersEngine.render_provision upscaler composition (Blocker B)."""

from __future__ import annotations

from typing import Any

import pytest

import kinoforge._adapters  # noqa: F401 — self-register engines
from kinoforge.core.errors import ExtrasNotInstalled
from kinoforge.engines.diffusers import DiffusersEngine


def _wan_only_cfg() -> dict[str, Any]:
    return {
        "engine": {
            "kind": "diffusers",
            "precision": "fp8",
            "diffusers": {
                "image": "runpod/pytorch:2.4.0",
                "pip": ["torch==2.6.0"],
                "server_cmd": [
                    "python",
                    "-m",
                    "kinoforge.engines.diffusers.servers.wan_t2v_server",
                ],
            },
        },
        "models": [
            {
                "kind": "base",
                "ref": "hf:Wan-AI/Wan2.2-T2V",
                "target": "diffusion_models",
            }
        ],
    }


def _with_spandrel(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(cfg)
    cfg["upscale"] = {
        "engine": "spandrel",
        "scale": "2x",
        "spandrel": {
            "model_url": "hf:lllyasviel/realesrgan/RealESRGAN_x2plus.pth",
            "arch": "realesrgan",
            "precision": "fp16",
            "tile_size": 512,
            "batch_size": 4,
        },
    }
    return cfg


def _with_seedvr2(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(cfg)
    cfg["upscale"] = {
        "engine": "seedvr2",
        "scale": "2x",
        "seedvr2": {"variant": "3B", "precision": "fp8"},
    }
    return cfg


def test_compose_spandrel_appends_to_script() -> None:
    # Bug caught: render_provision does NOT compose the upscaler script
    # and the pod boots without spandrel installed; `import spandrel`
    # inside the runtime crashes on first /upscale request.
    rp = DiffusersEngine().render_provision(_with_spandrel(_wan_only_cfg()))
    assert "spandrel" in rp.script
    assert "kinoforge.upscalers.spandrel._fetch_weights" in rp.script


def test_compose_order_spandrel_before_server_exec() -> None:
    # Bug caught: composition appends spandrel lines AFTER the
    # `wan_t2v_server` line, so they never run. Order matters.
    rp = DiffusersEngine().render_provision(_with_spandrel(_wan_only_cfg()))
    spandrel_idx = rp.script.find("spandrel")
    server_idx = rp.script.find("wan_t2v_server")
    assert spandrel_idx >= 0
    assert server_idx >= 0
    assert spandrel_idx < server_idx


def test_no_upscale_block_means_no_composition() -> None:
    # Bug caught: composition fires unconditionally and pure-t2v cfgs
    # start including spandrel deps for no reason.
    rp = DiffusersEngine().render_provision(_wan_only_cfg())
    assert "spandrel" not in rp.script
    assert "kinoforge.upscalers.spandrel._fetch_weights" not in rp.script


def test_seedvr2_composition_raises_extras_not_installed() -> None:
    # Bug caught: composition continues into a seedvr2 cfg, the
    # SeedVR2Engine extras-stub raises, the orchestrator sees an
    # opaque NotYetImplementedError from the wrong layer. Asserts
    # the ExtrasNotInstalled propagates cleanly.
    with pytest.raises(ExtrasNotInstalled, match=r"seedvr"):
        DiffusersEngine().render_provision(_with_seedvr2(_wan_only_cfg()))
