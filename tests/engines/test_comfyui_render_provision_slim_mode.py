"""C28 B2 — ``render_provision`` slim-mode branch for ``kinoforge/wan-comfyui:`` images.

When the configured image starts with ``kinoforge/wan-comfyui:``, the
pre-baked image already contains the ComfyUI clone + every custom-node
clone + all `pip install -r requirements.txt` work, so the provision
script SKIPS those steps. Pure-additive: any other image prefix is
byte-identical to the pre-B2 baseline.

Selfterm + model downloads + `exec python main.py` MUST still emit in
both modes.
"""

from __future__ import annotations

from typing import Any

from kinoforge.engines.comfyui import ComfyUIEngine

_STOCK_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
_PREBAKE_IMAGE = "kinoforge/wan-comfyui:v0.3.10-088128b2-cu124"


def _make_engine() -> ComfyUIEngine:
    return ComfyUIEngine(probe_profile=None)  # type: ignore[arg-type]


def _cfg_with_image(image: str) -> dict[str, Any]:
    return {
        "engine": {
            "comfyui": {
                "image": image,
                "custom_nodes": [
                    {
                        "git": "https://github.com/kijai/ComfyUI-WanVideoWrapper",
                        "ref": "088128b224242e110d3906c6750e9a3a348a659b",
                    },
                ],
                "launch_args": ["--listen", "0.0.0.0", "--port", "8188"],
            },
        },
        "models": [],
    }


def _render(image: str) -> str:
    return _make_engine().render_provision(_cfg_with_image(image)).script


def test_stock_image_still_clones_comfyui_and_pip_installs() -> None:
    """Stock image path must remain byte-identical to the pre-B2 baseline."""
    script = _render(_STOCK_IMAGE)
    assert "git clone --depth 1 --branch" in script
    assert "ComfyUI && pip install -q -r requirements.txt" in script
    assert "git clone https://github.com/kijai/ComfyUI-WanVideoWrapper" in script


def test_prebake_image_skips_comfyui_clone_and_pip() -> None:
    script = _render(_PREBAKE_IMAGE)
    assert "git clone --depth 1 --branch" not in script
    assert "pip install -q -r requirements.txt" not in script


def test_prebake_image_skips_custom_node_clones() -> None:
    script = _render(_PREBAKE_IMAGE)
    assert "git clone https://github.com/kijai/ComfyUI-WanVideoWrapper" not in script
    assert "custom_nodes/ComfyUI-WanVideoWrapper" not in script


def test_prebake_image_still_emits_selfterm_bootstrap() -> None:
    script = _render(_PREBAKE_IMAGE)
    assert "KINOFORGE_SELFTERM_SCRIPT" in script


def test_prebake_image_still_emits_exec_python_main() -> None:
    script = _render(_PREBAKE_IMAGE)
    assert "exec python main.py" in script


def test_prebake_image_still_cds_into_comfyui_workspace() -> None:
    """The pre-baked image's ComfyUI lives at /workspace/ComfyUI."""
    script = _render(_PREBAKE_IMAGE)
    assert "cd /workspace/ComfyUI" in script


def test_other_third_party_image_treated_as_stock() -> None:
    """Only the kinoforge/wan-comfyui: prefix flips slim-mode."""
    script = _render("ghcr.io/someone/other:latest")
    assert "git clone --depth 1 --branch" in script
