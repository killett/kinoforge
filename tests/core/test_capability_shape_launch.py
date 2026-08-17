# tests/core/test_capability_shape_launch.py
"""Launch-time shape re-check (Brief 2, Task 5)."""

from __future__ import annotations

import logging

import pytest

from kinoforge.core.capabilities import WorkloadShape
from kinoforge.core.config import Config
from kinoforge.core.orchestrator import assert_launch_capabilities


def test_shape_comes_from_the_spec_not_the_cfg(minimal_skypilot_cfg: Config) -> None:
    """Catches a re-check that re-reads cfg and therefore can never catch an
    inference miss: cfg infers SERVER, the spec says batch."""
    gaps = assert_launch_capabilities(
        minimal_skypilot_cfg, run_cmd=[], logger=logging.getLogger("t")
    )
    assert all(g.field != "compute.lifecycle.idle_timeout" for g in gaps)


def test_server_spec_still_reports_the_idle_gap(minimal_skypilot_cfg: Config) -> None:
    gaps = assert_launch_capabilities(
        minimal_skypilot_cfg,
        run_cmd=["python", "-m", "server"],
        logger=logging.getLogger("t"),
    )
    assert any(g.field == "compute.lifecycle.idle_timeout" for g in gaps)


def test_inference_miss_is_logged_not_swallowed(
    minimal_skypilot_cfg: Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catches a silent correction — the derivation must get fixed, not drift."""
    with caplog.at_level(logging.WARNING):
        assert_launch_capabilities(
            minimal_skypilot_cfg, run_cmd=[], logger=logging.getLogger("kinoforge")
        )
    assert any("shape inference miss" in r.message for r in caplog.records)


def test_error_gap_raises_before_create(
    minimal_skypilot_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an ERROR gap being logged instead of aborting the launch."""
    from kinoforge.core.errors import ValidationError
    from kinoforge.providers.skypilot import SkyPilotProvider

    monkeypatch.setattr(
        SkyPilotProvider,
        "capabilities",
        classmethod(lambda cls, shape=WorkloadShape.SERVER: frozenset()),
    )
    with pytest.raises(ValidationError, match="ON_INSTANCE_DEADLINE"):
        assert_launch_capabilities(
            minimal_skypilot_cfg,
            run_cmd=["python", "-m", "server"],
            logger=logging.getLogger("t"),
        )
