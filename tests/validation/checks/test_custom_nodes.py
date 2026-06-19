"""CustomNodeSHAReachableCheck tests."""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.core.config import Config, load_config
from kinoforge.validation.checks.custom_nodes import CustomNodeSHAReachableCheck
from kinoforge.validation.protocol import CheckCategory, Severity


def _cfg_with_custom_nodes(nodes: list[tuple[str, str]]) -> Config:
    """nodes: list of (git_url, ref)."""
    lines = "\n".join(f'      - git: "{g}"\n        ref: "{r}"' for g, r in nodes)
    yaml = f"""\
engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
    custom_nodes:
{lines}
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: runpod
  image: "runpod/pytorch:latest"
  mode: pod
  lifecycle:
    budget: 1.0
"""
    return load_config(yaml)


def _seam(code: int) -> Callable[[str], int]:
    def head(url: str) -> int:
        return code

    return head


def test_check_metadata() -> None:
    check = CustomNodeSHAReachableCheck(http_head=_seam(200))
    assert check.name == "custom_node_sha_reachable"
    assert check.category == CheckCategory.NETWORK
    assert check.severity == Severity.WARN


def test_does_not_apply_to_non_comfyui_engine() -> None:
    yaml = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: diffusion_models
"""
    cfg = load_config(yaml)
    check = CustomNodeSHAReachableCheck(http_head=_seam(200))
    assert check.applies_to(cfg) is False


def test_passes_when_all_shas_reachable() -> None:
    cfg = _cfg_with_custom_nodes(
        [("https://github.com/kijai/ComfyUI-KJNodes", "abc123")]
    )
    check = CustomNodeSHAReachableCheck(http_head=_seam(200))
    result = check.run(cfg)
    assert result.passed is True


def test_warns_on_404() -> None:
    cfg = _cfg_with_custom_nodes(
        [("https://github.com/kijai/ComfyUI-KJNodes", "archived0")]
    )
    check = CustomNodeSHAReachableCheck(http_head=_seam(404))
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity == Severity.WARN
    assert "archived0" in result.message
