"""compute.lifecycle.lora_swap_re_probe_after_s cfg knob."""

from __future__ import annotations

from kinoforge.core.config import LifecycleConfig
from kinoforge.core.interfaces import Lifecycle


def test_default_is_300() -> None:
    """Field absent → default 300.0 (back-compat with pre-feature cfgs).

    Bug: omitted field defaults to None and crashes the matcher's
    threshold comparison with TypeError.
    """
    lc = LifecycleConfig(budget=1.0)
    assert lc.lora_swap_re_probe_after_s == 300.0


def test_explicit_value_round_trips() -> None:
    """Setting the field at construction preserves the value.

    Bug: field is shadowed by another lifecycle field or silently
    coerced to the default.
    """
    lc = LifecycleConfig(budget=1.0, lora_swap_re_probe_after_s=60.0)
    assert lc.lora_swap_re_probe_after_s == 60.0


def test_zero_disables_stale_check() -> None:
    """Zero is permitted and means 'trust the snapshot indefinitely'.

    Bug: pydantic rejects 0.0 with a positive-only validator, blocking
    operators who want to skip re-probes entirely (e.g. unit-test cfgs).
    """
    lc = LifecycleConfig(budget=1.0, lora_swap_re_probe_after_s=0.0)
    assert lc.lora_swap_re_probe_after_s == 0.0


def test_negative_rejected() -> None:
    """Negative values are rejected at load (matcher contract is t >= 0).

    Bug: negative slides through, matcher does (now - observed > -1) and
    re-probes never fire.
    """
    import pytest

    with pytest.raises(ValueError, match="lora_swap_re_probe_after_s"):
        LifecycleConfig(budget=1.0, lora_swap_re_probe_after_s=-5.0)


def test_threaded_into_interface_lifecycle() -> None:
    """Config.lifecycle() projects the new field onto the dataclass.

    Bug: Config.lifecycle() omits the new field so the orchestrator
    can't access it without reading the raw pydantic model.
    """
    import yaml

    from kinoforge.core.config import Config

    yaml_text = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: fal
    endpoint: "https://fal.run/x"
    api_key_env: FAL_KEY
lifecycle: {budget: 5.0, lora_swap_re_probe_after_s: 42.0}
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
spec:
  model: "vendor/m"
"""
    cfg = Config.model_validate(yaml.safe_load(yaml_text))
    iface = cfg.lifecycle()
    assert isinstance(iface, Lifecycle)
    assert iface.lora_swap_re_probe_after_s == 42.0
