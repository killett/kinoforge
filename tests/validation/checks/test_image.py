"""ImageReachableCheck tests.

The check parses cfg.compute.image, derives the registry HEAD URL,
and asks the injected http_head seam for the status code. Tests
mock the seam directly — no live network.
"""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.core.config import Config, load_config
from kinoforge.validation.checks.image import ImageReachableCheck
from kinoforge.validation.protocol import CheckCategory, Severity


def _cfg(image: str) -> Config:
    yaml = f"""\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: runpod
  image: "{image}"
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
    check = ImageReachableCheck(http_head=_seam(200))
    assert check.name == "image_reachable"
    assert check.category == CheckCategory.NETWORK
    assert check.severity == Severity.ERROR


def test_does_not_apply_when_image_empty() -> None:
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
    check = ImageReachableCheck(http_head=_seam(200))
    assert check.applies_to(cfg) is False


def test_passes_on_200() -> None:
    cfg = _cfg("runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04")
    check = ImageReachableCheck(http_head=_seam(200))
    result = check.run(cfg)
    assert result.passed is True


def test_passes_on_401_auth_required() -> None:
    cfg = _cfg("runpod/pytorch:latest")
    check = ImageReachableCheck(http_head=_seam(401))
    result = check.run(cfg)
    assert result.passed is True


def test_fails_on_404_placeholder() -> None:
    cfg = _cfg("skypilot/skypilot-gpu:latest")
    check = ImageReachableCheck(http_head=_seam(404))
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity == Severity.ERROR
    assert "skypilot/skypilot-gpu" in result.message


def test_warns_on_transport_error_does_not_block() -> None:
    def raising(url: str) -> int:
        raise OSError("connection refused")

    cfg = _cfg("runpod/pytorch:latest")
    check = ImageReachableCheck(http_head=raising)
    result = check.run(cfg)
    assert result.passed is True
    assert result.severity == Severity.WARN
    assert "inconclusive" in result.message.lower()
