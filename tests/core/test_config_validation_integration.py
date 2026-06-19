"""load_config + Check Registry integration tests (STATIC pass only)."""

from __future__ import annotations

import logging

import pytest

from kinoforge.core.config import load_config
from kinoforge.core.errors import ValidationError

_CFG_TRIGGERS_HEARTBEAT_AUTOFIX = """\
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
  warm_reuse_auto_attach: true
  lifecycle:
    idle_timeout: 5m
    budget: 1.0
"""

_CFG_WITH_HEARTBEAT_SET = """\
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
  warm_reuse_auto_attach: true
  lifecycle:
    idle_timeout: 15m
    budget: 1.0
    heartbeat_interval_s: 30
"""


def test_load_config_applies_heartbeat_autofix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="kinoforge.validation"):
        cfg = load_config(_CFG_TRIGGERS_HEARTBEAT_AUTOFIX)
    assert cfg.compute is not None
    assert cfg.compute.lifecycle is not None
    assert cfg.compute.lifecycle.heartbeat_interval_s == 30
    assert any(
        "auto-fixed: heartbeat_interval_required" in r.getMessage()
        for r in caplog.records
    )


def test_load_config_passes_when_cfg_already_valid() -> None:
    cfg = load_config(_CFG_WITH_HEARTBEAT_SET)
    assert cfg.compute is not None
    assert cfg.compute.lifecycle is not None
    assert cfg.compute.lifecycle.heartbeat_interval_s == 30


def test_load_config_raises_on_unfixable_static_error() -> None:
    # Bad idle/heartbeat ratio — no auto-fix → ValidationError.
    bad_yaml = """\
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
  warm_reuse_auto_attach: false
  lifecycle:
    idle_timeout: 60s
    budget: 1.0
    heartbeat_interval_s: 30
"""
    with pytest.raises(ValidationError, match="idle_timeout"):
        load_config(bad_yaml)
