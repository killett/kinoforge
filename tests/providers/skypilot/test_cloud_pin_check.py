"""SkyPilotCloudPinSupportedCheck tests (migrated from Pydantic validator)."""

from __future__ import annotations

from kinoforge.core.config import Config, _parse_cfg_raw
from kinoforge.providers.skypilot import SkyPilotCloudPinSupportedCheck
from kinoforge.validation.protocol import CheckCategory, Severity


def _cfg(cloud_value: str) -> Config:
    yaml = f"""\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: skypilot
  image: "alpine:3"
  mode: pod
  cloud:
{cloud_value}
  lifecycle:
    budget: 1.0
"""
    return _parse_cfg_raw(yaml)


def test_check_metadata() -> None:
    check = SkyPilotCloudPinSupportedCheck()
    assert check.name == "skypilot_cloud_pin_supported"
    assert check.category == CheckCategory.STATIC
    assert check.severity == Severity.ERROR


def test_passes_when_all_entries_in_supported_set() -> None:
    cfg = _cfg('    - "lambda"')
    check = SkyPilotCloudPinSupportedCheck()
    result = check.run(cfg)
    assert result.passed is True


def test_fails_when_entry_unsupported() -> None:
    cfg = _cfg('    - "nintendo-cloud"')
    check = SkyPilotCloudPinSupportedCheck()
    result = check.run(cfg)
    assert result.passed is False
    assert "nintendo-cloud" in result.message
