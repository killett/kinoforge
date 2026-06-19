"""ModelRefReachableCheck tests."""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.core.config import Config, load_config
from kinoforge.validation.checks.models import ModelRefReachableCheck
from kinoforge.validation.protocol import CheckCategory, Severity

_TARGETS: dict[str, str] = {
    "base": "diffusion_models",
    "lora": "loras",
    "vae": "vae",
    "text_encoder": "text_encoders",
    "clip_vision": "clip_vision",
}


def _cfg_with_models(refs: list[tuple[str, str]]) -> Config:
    """refs: list of (ref, kind)."""
    model_lines = "\n".join(
        f'  - ref: "{r}"\n    kind: {k}\n    target: {_TARGETS[k]}' for r, k in refs
    )
    yaml = f"""\
engine:
  kind: fake
  precision: fp16
models:
{model_lines}
compute:
  provider: runpod
  image: "runpod/pytorch:latest"
  mode: pod
  lifecycle:
    budget: 1.0
"""
    return load_config(yaml)


def _recording_seam(
    code_for: dict[str, int],
) -> tuple[Callable[[str], int], list[str]]:
    visited: list[str] = []

    def head(url: str) -> int:
        visited.append(url)
        for key, code in code_for.items():
            if key in url:
                return code
        return 200

    return head, visited


def test_check_metadata() -> None:
    head, _ = _recording_seam({})
    check = ModelRefReachableCheck(http_head=head)
    assert check.name == "model_ref_reachable"
    assert check.category == CheckCategory.NETWORK
    assert check.severity == Severity.ERROR


def test_generate_mode_only_checks_kind_base() -> None:
    cfg = _cfg_with_models(
        [
            ("hf:org/repo:base.safetensors", "base"),
            ("hf:org/repo:vae.safetensors", "vae"),
            ("hf:org/repo:t5.safetensors", "text_encoder"),
        ]
    )
    head, visited = _recording_seam({})
    check = ModelRefReachableCheck(http_head=head, full=False)
    result = check.run(cfg)
    assert result.passed is True
    assert len([u for u in visited if "base.safetensors" in u]) == 1
    assert not any("vae.safetensors" in u for u in visited)


def test_doctor_mode_checks_all_refs() -> None:
    cfg = _cfg_with_models(
        [
            ("hf:org/repo:base.safetensors", "base"),
            ("hf:org/repo:vae.safetensors", "vae"),
        ]
    )
    head, visited = _recording_seam({})
    check = ModelRefReachableCheck(http_head=head, full=True)
    result = check.run(cfg)
    assert result.passed is True
    assert any("base.safetensors" in u for u in visited)
    assert any("vae.safetensors" in u for u in visited)


def test_hf_ref_translated_to_hub_url() -> None:
    cfg = _cfg_with_models(
        [("hf:Kijai/WanVideo_comfy:Wan2_1-T2V-1_3B_fp8_e4m3fn.safetensors", "base")]
    )
    head, visited = _recording_seam({})
    check = ModelRefReachableCheck(http_head=head, full=False)
    check.run(cfg)
    assert any(
        "huggingface.co/Kijai/WanVideo_comfy/resolve/main/Wan2_1-T2V-1_3B_fp8_e4m3fn.safetensors"
        in u
        for u in visited
    )


def test_404_on_base_model_fails() -> None:
    cfg = _cfg_with_models([("hf:org/repo:gone.safetensors", "base")])
    head, _ = _recording_seam({"gone.safetensors": 404})
    check = ModelRefReachableCheck(http_head=head, full=False)
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity == Severity.ERROR
    assert "gone.safetensors" in result.message
