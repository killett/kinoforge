"""ProviderCapabilityCheck (Brief 2, Task 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.config import load_config
from kinoforge.core.errors import ValidationError
from kinoforge.validation.checks.capabilities import (
    ProviderCapabilityCheck,
    evaluate_capability_gaps,
)
from kinoforge.validation.protocol import Severity

_CFG_TEMPLATE = """\
engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: {provider}
  image: "example/image:latest"
  mode: pod
  lifecycle:
    idle_timeout: 180
    max_lifetime: 1800
    budget: 0.10
    heartbeat_interval_s: 30
"""


def _write_cfg(tmp_path: Path, *, provider: str) -> Path:
    """Write a minimal valid cfg pinned to ``provider`` and return its path.

    Args:
        tmp_path: pytest tmp_path fixture directory.
        provider: ``compute.provider`` value to write.

    Returns:
        Path to the written YAML file.
    """
    path = tmp_path / f"{provider}.yaml"
    path.write_text(_CFG_TEMPLATE.format(provider=provider))
    return path


def test_heartbeat_gap_on_skypilot_is_a_warn_naming_the_clock(tmp_path: Path) -> None:
    """Catches a severity mapping that errors on a covered risk, and a
    message that does not tell the operator what the signal really means."""
    cfg = load_config(_write_cfg(tmp_path, provider="skypilot"))
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    hb = [g for g in gaps if g.field == "compute.lifecycle.heartbeat_interval_s"]
    assert len(hb) == 1
    assert hb[0].severity is Severity.WARN
    assert hb[0].missing is Capability.HEARTBEAT_READ
    result = ProviderCapabilityCheck().run(cfg)
    assert result.passed is False
    assert result.severity is Severity.WARN
    assert "orchestrator clock" in result.message


def test_idle_timeout_gap_names_the_substitute_and_its_bound(tmp_path: Path) -> None:
    """Catches a WARN that says 'unsupported' without telling the operator
    what actually bounds the run."""
    cfg = load_config(_write_cfg(tmp_path, provider="skypilot"))
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    idle = [g for g in gaps if g.field == "compute.lifecycle.idle_timeout"]
    assert idle[0].substitute is Capability.ON_INSTANCE_DEADLINE
    assert idle[0].severity is Severity.WARN


def test_uncovered_spend_risk_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a missing risk row letting an unbounded-spend config launch:
    a billed provider with no deadline and no autostop must not load."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    monkeypatch.setattr(
        SkyPilotProvider,
        "capabilities",
        classmethod(lambda cls, shape=WorkloadShape.SERVER: frozenset()),
    )
    with pytest.raises(ValidationError) as exc:
        load_config(_write_cfg(tmp_path, provider="skypilot"))
    assert "ON_INSTANCE_DEADLINE" in str(exc.value)
    assert "skypilot" in str(exc.value)


def test_unbilled_provider_skips_the_spend_rows(tmp_path: Path) -> None:
    """Catches `billed` being ignored, which would error every local run."""
    cfg = load_config(_write_cfg(tmp_path, provider="local"))
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    assert [g.field for g in gaps if "max_lifetime" in g.field] == []


def test_unregistered_provider_is_not_a_capability_gap(tmp_path: Path) -> None:
    """Catches the check claiming an unregistered provider "cannot enforce
    ON_INSTANCE_DEADLINE". There is no declaration to compare against, and
    `get_provider` already refuses the name at launch with the accurate
    message; a capability ERROR here would preempt it with a wrong one."""
    cfg = load_config(_write_cfg(tmp_path, provider="not-a-real-provider"))
    assert evaluate_capability_gaps(cfg, WorkloadShape.SERVER) == []
    assert ProviderCapabilityCheck().applies_to(cfg) is False


def test_check_never_auto_fixes_and_load_preserves_guardrail_values(
    tmp_path: Path,
) -> None:
    """Catches a future auto-fix that silences the diagnostic by rewriting
    the guardrail — how the current state became invisible."""
    path = _write_cfg(tmp_path, provider="skypilot")
    cfg = load_config(path)
    assert ProviderCapabilityCheck().auto_fix(cfg) is None
    assert cfg.compute is not None
    assert cfg.compute.lifecycle is not None
    assert cfg.compute.lifecycle.idle_timeout == 180.0
    assert cfg.compute.lifecycle.max_lifetime == 1800.0
