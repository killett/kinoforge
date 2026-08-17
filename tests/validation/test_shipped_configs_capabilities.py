"""Shipped configs validate clean (Brief 2, Task 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.capabilities import WorkloadShape
from kinoforge.core.config import Config, load_config
from kinoforge.validation.checks.capabilities import (
    evaluate_capability_gaps,
    infer_shape,
)
from kinoforge.validation.protocol import Severity

CONFIG_DIR = Path("examples/configs")
SERVER_SKY = [
    "skypilot-gpu.yaml",
    "skypilot-cpu.yaml",
    "skypilot-lambda-comfyui.yaml",
]
UPSCALE_SKY = [
    "skypilot-lambda-diffusers-flashvsr-upscale.yaml",
    "skypilot-vast-diffusers-flashvsr-upscale.yaml",
]

#: Risk-row leaf field names this module pins expectations against.
_RISK_LEAVES = ("idle_timeout", "job_timeout", "heartbeat_interval_s")


def _unset_fields(cfg: Config) -> set[str]:
    """Dotted risk-row fields the operator did not write in the cfg's YAML.

    ``evaluate_capability_gaps`` only reports a gap for a risk-row field
    that is present in ``lifecycle.model_fields_set`` — a field left at its
    Pydantic default was never asserted, so there is nothing to warn about.
    This mirrors that same-Config-instance check (both this helper and
    ``evaluate_capability_gaps`` read ``model_fields_set`` off the *same*
    loaded ``cfg``), so it reflects the LOAD-TIME set: whatever
    ``load_config`` already auto-fixed (e.g. the heartbeat-required
    auto-fix injecting ``heartbeat_interval_s=30``) counts as "set" here
    too, exactly as it would for a second `evaluate_capability_gaps` call
    against the same returned cfg.

    Args:
        cfg: A loaded Config with a compute block.

    Returns:
        Dotted ``compute.lifecycle.<leaf>`` names absent from
        ``model_fields_set``.
    """
    assert cfg.compute is not None
    lifecycle = cfg.compute.lifecycle
    assert lifecycle is not None
    set_leaves = set(lifecycle.model_fields_set)
    return {
        f"compute.lifecycle.{leaf}" for leaf in _RISK_LEAVES if leaf not in set_leaves
    }


@pytest.mark.parametrize(
    "name", sorted(p.name for p in CONFIG_DIR.glob("*.yaml")), ids=str
)
def test_no_shipped_config_has_an_error_gap(name: str) -> None:
    """Catches a config edit reintroducing an unenforceable guardrail.

    Every shipped top-level example (non-recursive glob, matching the
    established convention in tests/test_examples.py and
    tests/test_layer_r_backcompat.py — examples/configs/extras|grids|
    manifests|comparison are out of scope) must load with zero ERROR-level
    capability gaps at WorkloadShape.SERVER, the shape every provisioning
    call actually uses (infer_shape is unconditionally SERVER).
    """
    cfg = load_config(CONFIG_DIR / name)
    if cfg.compute is None:
        pytest.skip("hosted cfg, no compute block")
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    assert [g.field for g in gaps if g.severity is Severity.ERROR] == []


@pytest.mark.parametrize("name", SERVER_SKY)
def test_server_skypilot_configs_warn_on_exactly_these_fields(name: str) -> None:
    """Catches risk-table drift making real warnings silently disappear.

    All three ship with idle_timeout, job_timeout, and heartbeat_interval_s
    written explicitly, so ``_unset_fields`` is expected empty here — the
    subtraction stays in the assertion so a future config that drops one of
    these fields adjusts the expectation instead of silently going stale.
    """
    cfg = load_config(CONFIG_DIR / name)
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    assert {g.field for g in gaps} == {
        "compute.lifecycle.idle_timeout",
        "compute.lifecycle.job_timeout",
        "compute.lifecycle.heartbeat_interval_s",
    } - _unset_fields(cfg)


@pytest.mark.parametrize("name", UPSCALE_SKY)
def test_upscale_skypilot_configs_are_server_and_warn_on_idle(name: str) -> None:
    """`upscale_only: true` does NOT empty run_cmd — the upscaler runs as a
    pipeline stage against an already-provisioned pod. These deploy SERVER,
    so autostop is inert for them too. Catches a regression that reinstates
    the BATCH guess and reports idle_timeout as enforced when it is not.
    """
    cfg = load_config(CONFIG_DIR / name)
    assert infer_shape(cfg) is WorkloadShape.SERVER
    gaps = evaluate_capability_gaps(cfg, infer_shape(cfg))
    assert any(g.field == "compute.lifecycle.idle_timeout" for g in gaps)
    assert cfg.compute is not None
    assert cfg.compute.lifecycle is not None
    assert cfg.compute.lifecycle.heartbeat_interval_s == 30
