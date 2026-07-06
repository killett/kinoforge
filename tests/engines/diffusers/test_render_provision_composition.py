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


def _with_rife(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(cfg)
    cfg["interpolate"] = {
        "engine": "rife",
        "fps": 60.0,
        "rife": {
            "weights_ref": "hf:kinoforge/rife",
            "model": "rife49",
            "precision": "fp16",
        },
    }
    return cfg


def test_compose_rife_appends_to_script_before_server_exec() -> None:
    # Bug caught: render_provision does NOT compose the interpolator script, so
    # the pod boots without Practical-RIFE installed and the first /interpolate
    # crashes at model load. Composition must fire BEFORE the server exec line.
    rp = DiffusersEngine().render_provision(_with_rife(_wan_only_cfg()))
    assert "Practical-RIFE" in rp.script
    rife_idx = rp.script.find("Practical-RIFE")
    server_idx = rp.script.find("wan_t2v_server")
    assert rife_idx >= 0
    assert server_idx >= 0
    assert rife_idx < server_idx


def test_no_interpolate_block_means_no_rife_composition() -> None:
    # Bug caught: interpolator composition fires unconditionally, dragging
    # Practical-RIFE onto pure-t2v / upscale pods that never interpolate.
    rp = DiffusersEngine().render_provision(_wan_only_cfg())
    assert "Practical-RIFE" not in rp.script


def test_compose_spandrel_appends_to_script() -> None:
    # Bug caught: render_provision does NOT compose the upscaler script
    # and the pod boots without spandrel installed; `import spandrel`
    # inside the runtime crashes on first /upscale request.
    rp = DiffusersEngine().render_provision(_with_spandrel(_wan_only_cfg()))
    assert "spandrel" in rp.script
    # Weights fetch is now inlined as a curl HF resolve URL (rather than a
    # `python -m kinoforge.upscalers.spandrel._fetch_weights` call) so the
    # pod doesn't need the kinoforge package importable.
    assert "huggingface.co/lllyasviel/realesrgan/resolve/main" in rp.script


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


def test_upscale_only_emits_skip_wan_load_export() -> None:
    # Bug caught: upscale_only knob silently ignored — the on-pod
    # wan_t2v_server still calls WanPipeline.from_pretrained at startup
    # against a cfg with no Wan model, crashing the cold boot before
    # the first /upscale POST can ever fire.
    cfg = _with_spandrel(_wan_only_cfg())
    cfg["engine"]["diffusers"]["upscale_only"] = True

    rp = DiffusersEngine().render_provision(cfg)
    assert "export KINOFORGE_SKIP_WAN_LOAD=1" in rp.script
    # Order: the export must come BEFORE the server_cmd line (the export
    # only affects child processes the bootstrap launches afterwards).
    export_idx = rp.script.find("export KINOFORGE_SKIP_WAN_LOAD=1")
    server_idx = rp.script.find("wan_t2v_server")
    assert export_idx < server_idx


def test_no_upscale_only_means_no_skip_wan_load_export() -> None:
    # Bug caught: the export fires on every cfg, including Wan-loaded ones,
    # making the multi-stage cfgs accidentally skip the very Wan load they
    # need.
    rp = DiffusersEngine().render_provision(_with_spandrel(_wan_only_cfg()))
    assert "KINOFORGE_SKIP_WAN_LOAD" not in rp.script


def test_seedvr2_composition_raises_extras_not_installed() -> None:
    # Bug caught: composition continues into a seedvr2 cfg, the
    # SeedVR2Engine extras-stub raises, the orchestrator sees an
    # opaque NotYetImplementedError from the wrong layer. Asserts
    # the ExtrasNotInstalled propagates cleanly.
    with pytest.raises(ExtrasNotInstalled, match=r"seedvr"):
        DiffusersEngine().render_provision(_with_seedvr2(_wan_only_cfg()))
