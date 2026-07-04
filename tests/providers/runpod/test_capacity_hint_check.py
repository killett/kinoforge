"""RunPodCapacityHintCheck tests.

Wire contract updated 2026-07-03: RunPod's GraphQL migration renamed
``GpuTypesInput`` → ``GpuTypeFilter`` (list field ``ids``, was
``gpuTypes``) and REMOVED ``GpuType.availableCount``. The capacity
signal is now ``lowestPrice(input: {gpuCount: 1}).stockStatus``
(observed values ``"Low"``/``"High"``; ``null`` = no stock). The old
query 400'd with GRAPHQL_VALIDATION_FAILED on every generate.
"""

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


def _seam(no_stock_for: list[str]) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Fake RunPod POST replaying the post-migration wire shape.

    Bug caught: sending the retired ``{"gpuTypes": [...]}`` variables
    shape (or querying ``availableCount``) — the fake asserts the new
    ``ids`` field is used, mirroring the live endpoint's 400.
    """

    def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        assert "availableCount" not in body["query"], (
            "GpuType.availableCount was removed by RunPod's 2026-07-03 "
            "schema migration — query would 400"
        )
        assert "GpuTypesInput" not in body["query"], (
            "GpuTypesInput type no longer exists — query would 400"
        )
        types = []
        for gpu in body["variables"]["input"]["ids"]:
            status = None if gpu in no_stock_for else "Low"
            types.append({"id": gpu, "lowestPrice": {"stockStatus": status}})
        return {"data": {"gpuTypes": types}}

    return post


def test_check_metadata() -> None:
    check = RunPodCapacityHintCheck(http_post=_seam([]))
    assert check.name == "runpod_capacity_hint"
    assert check.category == CheckCategory.PREFLIGHT
    assert check.severity == Severity.WARN


def test_passes_when_at_least_one_preference_has_stock() -> None:
    cfg = load_config(_CFG)
    check = RunPodCapacityHintCheck(http_post=_seam(["NVIDIA GeForce RTX 4090"]))
    result = check.run(cfg)
    assert result.passed is True


def test_warns_when_no_preference_has_stock() -> None:
    cfg = load_config(_CFG)
    check = RunPodCapacityHintCheck(
        http_post=_seam(["NVIDIA RTX A5000", "NVIDIA GeForce RTX 4090"])
    )
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity == Severity.WARN
    assert "no current capacity" in result.message.lower()


def test_missing_lowest_price_treated_as_no_stock() -> None:
    """Bug caught: a GPU type RunPod returns WITHOUT a lowestPrice block
    (seen for delisted SKUs) crashing the parse with AttributeError on
    None instead of counting as unavailable."""
    cfg = load_config(_CFG)

    def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "data": {
                "gpuTypes": [
                    {"id": g, "lowestPrice": None}
                    for g in body["variables"]["input"]["ids"]
                ]
            }
        }

    check = RunPodCapacityHintCheck(http_post=post)
    result = check.run(cfg)
    assert result.passed is False
