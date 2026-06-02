"""Snapshot tests for DiffusersEngine.render_provision."""

from __future__ import annotations

from typing import Any

from kinoforge.core.interfaces import RenderedProvision
from kinoforge.engines.diffusers import DiffusersEngine


def _make_engine() -> DiffusersEngine:
    return DiffusersEngine(probe_profile=None)  # type: ignore[arg-type]


def _minimal_cfg() -> dict[str, Any]:
    return {
        "engine": {
            "diffusers": {
                "base_url": "http://localhost:8000",
                "pip": [],
                "server_cmd": ["python", "-m", "diffusers_server"],
            }
        }
    }


def test_render_provision_returns_rendered_provision() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert isinstance(rp, RenderedProvision)


def test_render_provision_script_starts_with_set_euo_pipefail() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.startswith("set -euo pipefail")


def test_render_provision_script_runs_pip_install_for_each_dep() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["pip"] = [
        "diffusers==0.27.0",
        "transformers",
        "accelerate",
    ]
    rp = _make_engine().render_provision(cfg)
    assert "pip install -q diffusers==0.27.0 transformers accelerate" in rp.script


def test_render_provision_script_ends_with_exec_server_cmd() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.rstrip().endswith("exec python -m diffusers_server")


def test_render_provision_run_cmd_matches_server_cmd() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.run_cmd == ["python", "-m", "diffusers_server"]


def test_render_provision_default_image_is_stock_runpod_pytorch() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.image == "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def test_render_provision_image_override_from_cfg() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["image"] = "myorg/diffusers-base:v1"
    rp = _make_engine().render_provision(cfg)
    assert rp.image == "myorg/diffusers-base:v1"


def test_render_provision_port_parsed_from_base_url() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["base_url"] = "http://localhost:9999"
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["9999"]


def test_render_provision_port_defaults_to_8000_when_base_url_missing() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["base_url"] = ""
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["8000"]


def test_render_provision_env_required_is_empty() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.env_required == []
