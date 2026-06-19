"""RunPodCapacityHintCheck tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kinoforge.core.config import load_config
from kinoforge.providers.runpod import RunPodCapacityHintCheck
from kinoforge.validation.protocol import CheckCategory, Severity

_CFG = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: runpod
  image: "runpod/pytorch:latest"
  mode: pod
  requirements:
    min_vram_gb: 16
    min_cuda: "12.4"
    max_usd_per_hr: 0.50
    gpu_preference:
      - "NVIDIA RTX A5000"
      - "NVIDIA GeForce RTX 4090"
    disk_gb: 40
  lifecycle:
    budget: 1.0
"""


def _seam(zero_for: list[str]) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        types = []
        for gpu in body["variables"]["input"]["gpuTypes"]:
            count = 0 if gpu in zero_for else 1
            types.append({"id": gpu, "availableCount": count})
        return {"data": {"gpuTypes": types}}

    return post


def test_check_metadata() -> None:
    check = RunPodCapacityHintCheck(http_post=_seam([]))
    assert check.name == "runpod_capacity_hint"
    assert check.category == CheckCategory.PREFLIGHT
    assert check.severity == Severity.WARN


def test_passes_when_at_least_one_preference_available() -> None:
    cfg = load_config(_CFG)
    check = RunPodCapacityHintCheck(http_post=_seam(["NVIDIA GeForce RTX 4090"]))
    result = check.run(cfg)
    assert result.passed is True


def test_warns_when_all_preferences_unavailable() -> None:
    cfg = load_config(_CFG)
    check = RunPodCapacityHintCheck(
        http_post=_seam(["NVIDIA RTX A5000", "NVIDIA GeForce RTX 4090"])
    )
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity == Severity.WARN
    assert "no current capacity" in result.message.lower()
