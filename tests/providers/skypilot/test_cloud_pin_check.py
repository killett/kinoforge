"""SkyPilotCloudPinSupportedCheck tests.

The pin moved from the portable ``compute.cloud`` to the provider-owned
``compute.backend_options.skypilot.clouds`` in compute-seam S1; the check
reads the namespace and its message names it.
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config, _parse_cfg_raw
from kinoforge.core.errors import ConfigError
from kinoforge.providers.skypilot import SkyPilotCloudPinSupportedCheck
from kinoforge.validation.protocol import CheckCategory, Severity


def _cfg(clouds_block: str) -> Config:
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
{clouds_block}  lifecycle:
    budget: 1.0
"""
    return _parse_cfg_raw(yaml)


def _with_clouds(entry: str) -> Config:
    return _cfg(
        f'  backend_options:\n    skypilot:\n      clouds:\n        - "{entry}"\n'
    )


def test_check_metadata() -> None:
    check = SkyPilotCloudPinSupportedCheck()
    assert check.name == "skypilot_cloud_pin_supported"
    assert check.category == CheckCategory.STATIC
    assert check.severity == Severity.ERROR


def test_passes_when_all_entries_in_supported_set() -> None:
    cfg = _with_clouds("lambda")
    check = SkyPilotCloudPinSupportedCheck()
    result = check.run(cfg)
    assert result.passed is True


def test_fails_when_entry_unsupported() -> None:
    cfg = _with_clouds("nintendo-cloud")
    check = SkyPilotCloudPinSupportedCheck()
    result = check.run(cfg)
    assert result.passed is False
    assert "nintendo-cloud" in result.message
    # Bug caught: the message keeps naming the deleted `compute.cloud` path,
    # so the operator edits a key that no longer exists.
    assert "compute.backend_options.skypilot.clouds" in result.message


def test_applies_only_when_the_namespace_pins_clouds() -> None:
    # Bug caught: applies_to keeps reading cfg.compute.cloud (now gone) and
    # either raises AttributeError inside `kinoforge doctor` or silently
    # stops applying to every config.
    check = SkyPilotCloudPinSupportedCheck()
    assert check.applies_to(_with_clouds("lambda")) is True
    assert check.applies_to(_cfg("")) is False


def test_legacy_cloud_key_in_yaml_is_refused_with_the_new_path() -> None:
    # Bug caught: a pre-S1 YAML keeps loading, the pin is silently dropped,
    # and sky's optimizer relocates the launch to whichever cloud is cheapest.
    with pytest.raises(ConfigError) as exc:
        _cfg('  cloud:\n    - "lambda"\n')
    assert "compute.backend_options.skypilot.clouds" in str(exc.value)
