"""Tests for orchestrator _provision_instance_and_build_backend Layer Q wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.errors import (
    AuthError,
    ProvisionFailed,
    ProvisionTimeout,
)
from kinoforge.core.interfaces import (
    Instance,
    InstanceSpec,
    Offer,
    RenderedProvision,
)
from kinoforge.core.orchestrator import _provision_instance_and_build_backend


@pytest.fixture
def fake_engine() -> MagicMock:
    engine = MagicMock()
    engine.name = "fakeengine"
    engine.render_provision.return_value = RenderedProvision(
        script="echo hi",
        run_cmd=["python", "-m", "x"],
        image="fake:latest",
        ports=["8000"],
        env_required=["HF_TOKEN"],
    )
    return engine


@pytest.fixture
def fake_provider() -> MagicMock:
    provider = MagicMock()
    provider.name = "fakeprovider"
    provider.find_offers.return_value = [
        Offer(
            id="X1",
            gpu_type="X1",
            vram_gb=24,
            cuda="12.8",
            cost_rate_usd_per_hr=0.30,
        )
    ]
    provider.create_instance.return_value = Instance(
        id="inst-1",
        provider="fakeprovider",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://inst-1-8000"},
    )
    provider.get_instance.return_value = Instance(
        id="inst-1",
        provider="fakeprovider",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://inst-1-8000"},
    )
    return provider


def _make_cfg() -> MagicMock:
    """Build a MagicMock cfg that _cfg_dict(cfg) can handle."""
    cfg = MagicMock()
    cfg.lifecycle.return_value = MagicMock(idle_timeout_s=3600.0, boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="should-be-overridden")
    cfg.model_dump.return_value = {
        "lifecycle": {"boot_timeout_s": 900.0, "idle_timeout_s": 3600.0},
        "engine": {},
        "models": [],
    }
    return cfg


def test_orchestrator_calls_render_provision_once(
    fake_engine: MagicMock, fake_provider: MagicMock
) -> None:
    """render_provision must be called exactly once per _provision_instance_and_build_backend."""
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = _make_cfg()
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    _provision_instance_and_build_backend(
        resolved_engine=fake_engine,
        resolved_provider=fake_provider,
        cfg=cfg,
        run_id="run-1",
        key=key,
        creds=creds,
        store=store,
        state_dir=Path("/tmp"),
        for_discovery=False,
    )
    fake_engine.render_provision.assert_called_once()


def test_orchestrator_validates_env_required_via_creds(
    fake_engine: MagicMock, fake_provider: MagicMock
) -> None:
    """Each var in rendered.env_required must be looked up via creds.get."""
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = _make_cfg()
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    _provision_instance_and_build_backend(
        resolved_engine=fake_engine,
        resolved_provider=fake_provider,
        cfg=cfg,
        run_id="run-1",
        key=key,
        creds=creds,
        store=store,
        state_dir=Path("/tmp"),
        for_discovery=False,
    )
    creds.get.assert_any_call("HF_TOKEN")


def test_orchestrator_raises_auth_error_when_env_required_missing(
    fake_engine: MagicMock, fake_provider: MagicMock
) -> None:
    """AuthError raised before create_instance when a required env var is missing."""
    creds = MagicMock()
    creds.get = MagicMock(return_value=None)
    cfg = _make_cfg()
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    with pytest.raises(AuthError, match="HF_TOKEN"):
        _provision_instance_and_build_backend(
            resolved_engine=fake_engine,
            resolved_provider=fake_provider,
            cfg=cfg,
            run_id="run-1",
            key=key,
            creds=creds,
            store=store,
            state_dir=Path("/tmp"),
            for_discovery=False,
        )
    fake_provider.create_instance.assert_not_called()


def test_orchestrator_spec_carries_rendered_provision_payload(
    fake_engine: MagicMock, fake_provider: MagicMock
) -> None:
    """InstanceSpec passed to create_instance carries image/provision_script/run_cmd/env from rendered."""
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = _make_cfg()
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    _provision_instance_and_build_backend(
        resolved_engine=fake_engine,
        resolved_provider=fake_provider,
        cfg=cfg,
        run_id="run-1",
        key=key,
        creds=creds,
        store=store,
        state_dir=Path("/tmp"),
        for_discovery=False,
    )
    spec_arg: InstanceSpec = fake_provider.create_instance.call_args[0][0]
    assert spec_arg.image == "fake:latest"
    assert spec_arg.provision_script == "echo hi"
    assert spec_arg.run_cmd == ["python", "-m", "x"]
    assert spec_arg.env.get("HF_TOKEN") == "hf_REAL"


def test_orchestrator_destroys_on_provision_failed(
    fake_engine: MagicMock, fake_provider: MagicMock, tmp_path: Path
) -> None:
    """ProvisionFailed → destroy_instance called + exception propagates."""
    fake_engine.provision.side_effect = ProvisionFailed("boot crashed")
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = _make_cfg()
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    with pytest.raises(ProvisionFailed):
        _provision_instance_and_build_backend(
            resolved_engine=fake_engine,
            resolved_provider=fake_provider,
            cfg=cfg,
            run_id="run-1",
            key=key,
            creds=creds,
            store=store,
            state_dir=tmp_path,
            for_discovery=False,
        )
    fake_provider.destroy_instance.assert_called_once_with("inst-1")


def test_orchestrator_destroys_on_provision_timeout(
    fake_engine: MagicMock, fake_provider: MagicMock, tmp_path: Path
) -> None:
    """ProvisionTimeout → destroy_instance called + exception propagates."""
    fake_engine.provision.side_effect = ProvisionTimeout("ran out")
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = _make_cfg()
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    with pytest.raises(ProvisionTimeout):
        _provision_instance_and_build_backend(
            resolved_engine=fake_engine,
            resolved_provider=fake_provider,
            cfg=cfg,
            run_id="run-1",
            key=key,
            creds=creds,
            store=store,
            state_dir=tmp_path,
            for_discovery=False,
        )
    fake_provider.destroy_instance.assert_called_once_with("inst-1")


def test_orchestrator_wires_get_instance_onto_engine_before_provision(
    fake_engine: MagicMock, fake_provider: MagicMock, tmp_path: Path
) -> None:
    """engine._get_instance is set to provider.get_instance before engine.provision runs."""
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = _make_cfg()
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    def _provision_check(instance: Any, cfg_dict: Any) -> None:
        assert fake_engine._get_instance is fake_provider.get_instance

    fake_engine.provision.side_effect = _provision_check

    _provision_instance_and_build_backend(
        resolved_engine=fake_engine,
        resolved_provider=fake_provider,
        cfg=cfg,
        run_id="run-1",
        key=key,
        creds=creds,
        store=store,
        state_dir=tmp_path,
        for_discovery=False,
    )
