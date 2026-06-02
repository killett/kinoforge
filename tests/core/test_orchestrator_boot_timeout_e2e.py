"""End-to-end lockdown: boot_timeout in YAML → reaches engine.provision via cfg_dict.

Regression for the whole-branch finding that cfg.model_dump() does NOT include
a top-level 'lifecycle' key — cfg["lifecycle"] would silently return {} and
the engine would fall back to 900.0. The orchestrator must lift the resolved
Lifecycle dataclass onto cfg_dict["lifecycle"] so engines read the YAML value.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import kinoforge.sources.huggingface  # noqa: F401 — registers hf: source for provisioner
from kinoforge.core.config import load_config
from kinoforge.core.interfaces import (
    Instance,
    Offer,
    RenderedProvision,
)
from kinoforge.core.orchestrator import _provision_instance_and_build_backend
from kinoforge.engines.fake import FakeEngine


def test_orchestrator_passes_boot_timeout_from_yaml_through_to_engine_provision(
    tmp_path: Path,
) -> None:
    """End-to-end lockdown: boot_timeout in YAML → reaches engine.provision via cfg_dict.

    Regression for the whole-branch finding that cfg.model_dump() does NOT include
    a top-level "lifecycle" key — cfg["lifecycle"] would silently return {} and
    the engine would fall back to 900.0. The orchestrator must lift the resolved
    Lifecycle dataclass onto cfg_dict["lifecycle"] so engines read the YAML value.

    This test uses a REAL Config loaded from YAML (not a MagicMock) to ensure
    the fix holds against the actual pydantic model_dump() output shape.
    """
    yaml_text = textwrap.dedent("""\
        engine:
          kind: comfyui
          precision: fp16
          comfyui:
            version: '1.0'
        models:
          - ref: 'hf:foo/bar:weight.safetensors'
            kind: base
            target: checkpoints
        compute:
          provider: runpod
          image: runpod/pytorch:latest
          lifecycle:
            budget: 5.0
            boot_timeout: 600
    """)
    yaml_path = tmp_path / "k.yaml"
    yaml_path.write_text(yaml_text)
    cfg = load_config(yaml_path)

    # Confirm the bug would have been silent: model_dump has no top-level "lifecycle" key.
    dump = cfg.model_dump()
    assert "lifecycle" not in dump, (
        f"If pydantic ever adds a top-level 'lifecycle' key this test needs updating; "
        f"got dump keys: {list(dump.keys())}"
    )

    # Spy engine that captures the cfg_dict passed to provision().
    captured_cfg: list[dict[str, Any]] = []

    class _SpyEngine(FakeEngine):
        def provision(
            self, instance: Instance | None, cfg_dict: dict[str, Any]
        ) -> None:
            captured_cfg.append(dict(cfg_dict))

        def render_provision(self, cfg_dict: dict[str, object]) -> RenderedProvision:
            return RenderedProvision(
                script="echo hi",
                run_cmd=["sleep", "1"],
                image="img:latest",
                ports=["8000"],
                env_required=[],
            )

    engine = _SpyEngine()
    provider = MagicMock()
    provider.name = "runpod"
    provider.find_offers.return_value = [
        Offer(
            id="X",
            gpu_type="A100",
            vram_gb=80,
            cuda="12.8",
            cost_rate_usd_per_hr=0.30,
        )
    ]
    ready_instance = Instance(
        id="inst-1",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://inst-1-8000"},
    )
    provider.create_instance.return_value = ready_instance
    provider.get_instance.return_value = ready_instance

    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef" * 8  # 64 hex chars

    _provision_instance_and_build_backend(
        resolved_engine=engine,
        resolved_provider=provider,
        cfg=cfg,
        run_id="run-1",
        key=key,
        creds=creds,
        store=store,
        state_dir=tmp_path,
        for_discovery=False,
    )

    assert len(captured_cfg) == 1, (
        f"expected engine.provision called once, got {len(captured_cfg)}"
    )
    lifecycle_block = captured_cfg[0].get("lifecycle", {})
    assert isinstance(lifecycle_block, dict), (
        f"cfg_dict['lifecycle'] should be a dict, got {lifecycle_block!r}"
    )
    assert lifecycle_block.get("boot_timeout_s") == 600.0, (
        f"orchestrator should lift Lifecycle.boot_timeout_s=600.0 onto cfg_dict, "
        f"got lifecycle_block={lifecycle_block!r}"
    )
