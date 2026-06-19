"""HeartbeatIntervalRequiredCheck tests.

This is the check that catches the bug from the 2026-06-18 Wan 1.3B
warm-reuse smoke (two pods cold-created instead of warm-attaching
because `lifecycle.heartbeat_interval_s` was missing).
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config, load_config
from kinoforge.validation.checks.heartbeat import HeartbeatIntervalRequiredCheck
from kinoforge.validation.protocol import CheckCategory, Severity

_CFG_WITH_WARM_REUSE_NO_HEARTBEAT = """\
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
"""

_CFG_WITH_WARM_REUSE_AND_HEARTBEAT = """\
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

_CFG_WARM_REUSE_OFF = """\
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
    idle_timeout: 15m
    budget: 1.0
"""


@pytest.fixture
def check() -> HeartbeatIntervalRequiredCheck:
    return HeartbeatIntervalRequiredCheck()


def test_check_metadata_is_static_error(
    check: HeartbeatIntervalRequiredCheck,
) -> None:
    assert check.name == "heartbeat_interval_required"
    assert check.category == CheckCategory.STATIC
    assert check.severity == Severity.ERROR


def test_check_does_not_apply_when_warm_reuse_off(
    check: HeartbeatIntervalRequiredCheck,
) -> None:
    cfg = load_config(_CFG_WARM_REUSE_OFF)
    assert check.applies_to(cfg) is False


def test_check_fails_when_warm_reuse_on_and_heartbeat_unset(
    check: HeartbeatIntervalRequiredCheck,
) -> None:
    cfg = load_config(_CFG_WITH_WARM_REUSE_NO_HEARTBEAT)
    assert check.applies_to(cfg) is True
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity == Severity.ERROR
    assert "heartbeat_interval_s" in result.message
    assert result.fix_suggestion is not None and "30" in result.fix_suggestion


def test_check_passes_when_heartbeat_set(
    check: HeartbeatIntervalRequiredCheck,
) -> None:
    cfg = load_config(_CFG_WITH_WARM_REUSE_AND_HEARTBEAT)
    result = check.run(cfg)
    assert result.passed is True


def test_auto_fix_sets_heartbeat_to_30_without_mutating_input(
    check: HeartbeatIntervalRequiredCheck,
) -> None:
    cfg_before = load_config(_CFG_WITH_WARM_REUSE_NO_HEARTBEAT)
    assert cfg_before.compute is not None
    assert cfg_before.compute.lifecycle is not None
    assert cfg_before.compute.lifecycle.heartbeat_interval_s is None

    cfg_after = check.auto_fix(cfg_before)
    assert isinstance(cfg_after, Config)
    assert cfg_after is not cfg_before
    assert cfg_after.compute is not None
    assert cfg_after.compute.lifecycle is not None
    assert cfg_after.compute.lifecycle.heartbeat_interval_s == 30
    assert cfg_before.compute.lifecycle.heartbeat_interval_s is None
