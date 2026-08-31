"""Behavior: orchestrator threads the build/runtime provision split onto the spec.

The Modal fast-boot feature needs the engine's bakeable install steps and its
runtime-only steps carried separately on ``InstanceSpec`` so ``ModalProvider``
can bake the former into the image. Without this threading, the whole fast-boot
path is inert and FlashVSR keeps preempting on its ~15 min runtime boot.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kinoforge.core.interfaces import (
    Instance,
    InstanceSpec,
    Launch,
    Lifecycle,
    Offer,
    RenderedProvision,
    SetupStep,
    combine_steps,
)
from kinoforge.core.orchestrator import _provision_instance_and_build_backend


def _fake_provider() -> MagicMock:
    provider = MagicMock()
    provider.name = "fakeprovider"
    provider.find_offers.return_value = [
        Offer(id="X1", gpu_type="X1", vram_gb=80, cuda="12.4", cost_rate_usd_per_hr=1.0)
    ]
    inst = Instance(
        id="inst-1",
        provider="fakeprovider",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://inst-1-8000"},
    )
    provider.create_instance.return_value = inst
    provider.get_instance.return_value = inst
    return provider


def _fake_engine(rendered: RenderedProvision) -> MagicMock:
    engine = MagicMock()
    engine.name = "fakeengine"
    engine.render_provision.return_value = rendered
    return engine


def _make_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.lifecycle.return_value = Lifecycle(boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="should-be-overridden")
    cfg.model_dump.return_value = {"engine": {}, "models": []}
    return cfg


def _drive(rendered: RenderedProvision) -> InstanceSpec:
    provider = _fake_provider()
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    key = MagicMock()
    key.derive.return_value = "deadbeef"
    _provision_instance_and_build_backend(
        resolved_engine=_fake_engine(rendered),
        resolved_provider=provider,
        cfg=_make_cfg(),
        run_id="run-1",
        key=key,
        creds=creds,
        store=MagicMock(),
        state_dir=Path("/tmp"),
        for_discovery=False,
    )
    return provider.create_instance.call_args[0][0]


def test_spec_carries_the_setup_steps_with_their_routing_flags() -> None:
    # Bug caught: without threading, ModalProvider cannot distinguish the
    # bakeable install steps from the runtime boot -> fast-boot feature is dead.
    rendered = RenderedProvision(
        script="COMBINED-SCRIPT",
        setup_steps=(
            SetupStep("pip install torch", bakeable=True, runtime=False),
            SetupStep("export A=1"),
        ),
        launch=Launch(("python", "-m", "x")),
        image="fake:latest",
        ports=["8000"],
        env_required=["HF_TOKEN"],
    )
    spec = _drive(rendered)
    assert spec.setup_steps == rendered.setup_steps
    assert spec.launch == Launch(("python", "-m", "x"))
    assert combine_steps(tuple(s for s in spec.setup_steps if s.bakeable)) == (
        "pip install torch"
    )
    assert combine_steps(tuple(s for s in spec.setup_steps if s.runtime)) == (
        "export A=1"
    )


def test_a_spec_with_no_bakeable_steps_bakes_nothing() -> None:
    # Engines that emit no installs must leave Modal's build script empty so it
    # skips the run_commands bake entirely.
    rendered = RenderedProvision(
        script="COMBINED",
        setup_steps=(SetupStep("export A=1"),),
        launch=Launch(("python", "-m", "x")),
        image="fake:latest",
        ports=["8000"],
        env_required=["HF_TOKEN"],
    )
    spec = _drive(rendered)
    assert combine_steps(tuple(s for s in spec.setup_steps if s.bakeable)) == ""
    assert (
        combine_steps(tuple(s for s in spec.setup_steps if s.runtime)) == "export A=1"
    )


@pytest.mark.parametrize(
    "cfg_path", ["examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml"]
)
def test_flashvsr_cfg_threads_real_split(cfg_path: str) -> None:
    # End-to-end with the real engine + real cfg: the FlashVSR split reaches the
    # spec with pip/BSA bakeable and the server launch off the scripts entirely.
    from kinoforge.core.config import load_config
    from kinoforge.engines.diffusers import DiffusersEngine

    rendered = DiffusersEngine().render_provision(load_config(cfg_path).model_dump())
    spec = _drive(rendered)
    bakeable = combine_steps(tuple(s for s in spec.setup_steps if s.bakeable))
    runtime = combine_steps(tuple(s for s in spec.setup_steps if s.runtime))
    assert "pip install" in bakeable
    assert "block_sparse_attn" in bakeable
    assert "pip install" not in runtime
    assert spec.launch is not None
    assert "wan_t2v_server" in " ".join(spec.launch.argv)
