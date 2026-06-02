"""Snapshot + parametrised tests for ComfyUIEngine.render_provision."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import RenderedProvision
from kinoforge.engines.comfyui import ComfyUIEngine


def _make_engine() -> ComfyUIEngine:
    return ComfyUIEngine(probe_profile=None)  # type: ignore[arg-type]


def _minimal_cfg() -> dict[str, Any]:
    return {
        "engine": {
            "comfyui": {
                "custom_nodes": [],
                "launch_args": ["--listen", "0.0.0.0", "--port", "8188"],
            }
        },
        "models": [],
    }


def test_render_provision_returns_rendered_provision() -> None:
    """Sanity — engine emits a RenderedProvision."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert isinstance(rp, RenderedProvision)


def test_render_provision_default_image_is_stock_runpod_pytorch() -> None:
    """When cfg doesn't override image, default is the stock RunPod image."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.image == "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def test_render_provision_image_override_from_cfg() -> None:
    """cfg.engine.comfyui.image overrides the default."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["image"] = "custom/image:v1"
    rp = _make_engine().render_provision(cfg)
    assert rp.image == "custom/image:v1"


def test_render_provision_script_starts_with_set_euo_pipefail() -> None:
    """Script must fail-fast — set -euo pipefail at the top."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.startswith("set -euo pipefail")


def test_render_provision_script_clones_comfyui_with_guard() -> None:
    """Script clones default repo with idempotency guard."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert "[ ! -d ComfyUI ] && git clone --depth 1" in rp.script
    assert "https://github.com/comfyanonymous/ComfyUI" in rp.script


def test_render_provision_respects_repo_branch_override() -> None:
    """cfg.engine.comfyui.repo + branch flow into clone line."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["repo"] = "https://github.com/forky/ComfyUI"
    cfg["engine"]["comfyui"]["branch"] = "experimental"
    rp = _make_engine().render_provision(cfg)
    assert "https://github.com/forky/ComfyUI" in rp.script
    assert "--branch experimental" in rp.script


def test_render_provision_runs_comfyui_requirements_install() -> None:
    """Script installs ComfyUI's own requirements.txt."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert "pip install -q -r requirements.txt" in rp.script


def test_render_provision_custom_node_without_ref_uses_shallow_clone() -> None:
    """Without ref, shallow clone — fast + small disk."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["custom_nodes"] = [
        {"git": "https://github.com/kijai/ComfyUI-KJNodes"}
    ]
    rp = _make_engine().render_provision(cfg)
    assert (
        "[ ! -d custom_nodes/ComfyUI-KJNodes ] && "
        "git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes "
        "custom_nodes/ComfyUI-KJNodes"
    ) in rp.script
    assert (
        "[ -f custom_nodes/ComfyUI-KJNodes/requirements.txt ] && "
        "pip install -q -r custom_nodes/ComfyUI-KJNodes/requirements.txt || true"
    ) in rp.script


def test_render_provision_custom_node_with_ref_uses_full_clone_and_checkout() -> None:
    """With ref, full clone + git checkout for SHA pinning (Layer P T2 contract)."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["custom_nodes"] = [
        {
            "git": "https://github.com/kijai/ComfyUI-KJNodes",
            "ref": "abc123def456",
        }
    ]
    rp = _make_engine().render_provision(cfg)
    assert (
        "[ ! -d custom_nodes/ComfyUI-KJNodes ] && "
        "git clone https://github.com/kijai/ComfyUI-KJNodes "
        "custom_nodes/ComfyUI-KJNodes && "
        "cd custom_nodes/ComfyUI-KJNodes && git checkout abc123def456 && cd ../.."
    ) in rp.script


def test_render_provision_hf_model_with_auth_header_and_env_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF model emits curl with $HF_TOKEN header AND env_required includes HF_TOKEN."""
    # HuggingFaceSource self-registers on import; force-import.
    import kinoforge.sources.huggingface  # noqa: F401

    cfg = _minimal_cfg()
    cfg["models"] = [
        {
            "src": "hf:Kijai/WanVideo_comfy:wan2.1.safetensors",
            "target": "checkpoints",
        }
    ]
    rp = _make_engine().render_provision(cfg)
    assert "HF_TOKEN" in rp.env_required
    assert '-H "Authorization: Bearer $HF_TOKEN"' in rp.script
    assert "[ ! -f models/checkpoints/wan2.1.safetensors ]" in rp.script
    assert "models/checkpoints/wan2.1.safetensors" in rp.script


def test_render_provision_no_models_means_no_env_required() -> None:
    """Empty models list yields empty env_required."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.env_required == []


def test_render_provision_script_ends_with_exec_run_cmd() -> None:
    """Script's final line is exec python main.py … so it becomes PID 1."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.rstrip().endswith(
        "exec python main.py --listen 0.0.0.0 --port 8188"
    )


def test_render_provision_run_cmd_matches_launch_args() -> None:
    """run_cmd mirrors the launch_args list with python main.py prefix."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.run_cmd == ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]


def test_render_provision_port_parsed_from_launch_args() -> None:
    """--port arg in launch_args is reflected on ports."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["launch_args"] = ["--listen", "0.0.0.0", "--port", "9999"]
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["9999"]


def test_render_provision_port_defaults_to_8188_when_absent() -> None:
    """When --port not in launch_args, default 8188."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["launch_args"] = ["--listen", "0.0.0.0"]
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["8188"]
