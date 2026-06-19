"""IdleTimeoutVsHeartbeatCheck + GraceAfterSessionTooTightCheck tests."""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config, _parse_cfg_raw
from kinoforge.validation.checks.lifecycle import (
    GraceAfterSessionTooTightCheck,
    IdleTimeoutVsHeartbeatCheck,
)
from kinoforge.validation.protocol import CheckCategory, Severity


def _cfg_yaml(idle: str, hb_s: int | None) -> str:
    hb_line = f"\n    heartbeat_interval_s: {hb_s}" if hb_s is not None else ""
    return f"""\
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
  lifecycle:
    idle_timeout: {idle}
    budget: 1.0{hb_line}
"""


@pytest.fixture
def check() -> IdleTimeoutVsHeartbeatCheck:
    return IdleTimeoutVsHeartbeatCheck()


def test_check_metadata(check: IdleTimeoutVsHeartbeatCheck) -> None:
    assert check.name == "idle_timeout_vs_heartbeat"
    assert check.category == CheckCategory.STATIC
    assert check.severity == Severity.ERROR


def test_does_not_apply_when_heartbeat_unset(
    check: IdleTimeoutVsHeartbeatCheck,
) -> None:
    cfg = _parse_cfg_raw(_cfg_yaml(idle="15m", hb_s=None))
    assert check.applies_to(cfg) is False


def test_fails_when_idle_timeout_below_3x_heartbeat(
    check: IdleTimeoutVsHeartbeatCheck,
) -> None:
    cfg = _parse_cfg_raw(_cfg_yaml(idle="60s", hb_s=30))
    result = check.run(cfg)
    assert result.passed is False
    assert "dead-man" in result.message or "3 * heartbeat" in result.message


def test_passes_when_idle_timeout_at_3x_heartbeat(
    check: IdleTimeoutVsHeartbeatCheck,
) -> None:
    cfg = _parse_cfg_raw(_cfg_yaml(idle="90s", hb_s=30))
    result = check.run(cfg)
    assert result.passed is True


def test_auto_fix_returns_none(check: IdleTimeoutVsHeartbeatCheck) -> None:
    cfg = _parse_cfg_raw(_cfg_yaml(idle="60s", hb_s=30))
    assert check.auto_fix(cfg) is None


def _cfg_with_grace(grace_s: int) -> Config:
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
  image: "runpod/pytorch:latest"
  mode: pod
  lifecycle:
    budget: 1.0
    grace_after_session_s: {grace_s}
"""
    return _parse_cfg_raw(yaml)


def test_grace_check_metadata() -> None:
    check = GraceAfterSessionTooTightCheck()
    assert check.name == "grace_after_session_too_tight"
    assert check.category == CheckCategory.STATIC
    assert check.severity == Severity.WARN


def test_grace_warns_when_below_600s() -> None:
    cfg = _cfg_with_grace(300)
    check = GraceAfterSessionTooTightCheck()
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity == Severity.WARN
    assert "operator-typing-pace" in result.message or "300" in result.message


def test_grace_passes_at_600s_floor() -> None:
    cfg = _cfg_with_grace(600)
    check = GraceAfterSessionTooTightCheck()
    result = check.run(cfg)
    assert result.passed is True


def test_grace_passes_at_default_1800s() -> None:
    cfg = _parse_cfg_raw("""\
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
  lifecycle:
    budget: 1.0
""")
    check = GraceAfterSessionTooTightCheck()
    result = check.run(cfg)
    assert result.passed is True
