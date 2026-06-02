"""Lockdown tests for LifecycleConfig.boot_timeout round-trip + interfaces.Lifecycle.boot_timeout_s."""

from __future__ import annotations

import textwrap
from pathlib import Path

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import Lifecycle


def test_interfaces_lifecycle_has_boot_timeout_s_default_900() -> None:
    """The interfaces dataclass carries the seam consumed by engine.wait_for_ready."""
    lc = Lifecycle()
    assert lc.boot_timeout_s == 900.0


def test_load_config_yaml_default_boot_timeout(tmp_path: Path) -> None:
    """When YAML omits boot_timeout, lifecycle() returns 900s."""
    yaml = textwrap.dedent(
        """\
        engine:
          kind: comfyui
          precision: fp16
          comfyui:
            version: "1.0"
        models:
          - ref: "hf:foo/bar:weight.safetensors"
            kind: base
            target: checkpoints
        compute:
          provider: runpod
          image: runpod/pytorch:latest
          lifecycle:
            budget: 5.0
        """
    )
    cfg_path = tmp_path / "k.yaml"
    cfg_path.write_text(yaml)
    cfg = load_config(cfg_path)
    assert cfg.lifecycle().boot_timeout_s == 900.0


def test_load_config_yaml_overrides_boot_timeout(tmp_path: Path) -> None:
    """YAML override of boot_timeout flows through Config.lifecycle()."""
    yaml = textwrap.dedent(
        """\
        engine:
          kind: comfyui
          precision: fp16
          comfyui:
            version: "1.0"
        models:
          - ref: "hf:foo/bar:weight.safetensors"
            kind: base
            target: checkpoints
        compute:
          provider: runpod
          image: runpod/pytorch:latest
          lifecycle:
            budget: 5.0
            boot_timeout: 1800
        """
    )
    cfg_path = tmp_path / "k.yaml"
    cfg_path.write_text(yaml)
    cfg = load_config(cfg_path)
    assert cfg.lifecycle().boot_timeout_s == 1800.0


def test_load_config_yaml_boot_timeout_accepts_duration_string(tmp_path: Path) -> None:
    """YAML can write `boot_timeout: 30m` as a duration string (parity with sibling fields)."""
    yaml = textwrap.dedent(
        """\
        engine:
          kind: comfyui
          precision: fp16
          comfyui:
            version: "1.0"
        models:
          - ref: "hf:foo/bar:weight.safetensors"
            kind: base
            target: checkpoints
        compute:
          provider: runpod
          image: runpod/pytorch:latest
          lifecycle:
            budget: 5.0
            boot_timeout: 30m
        """
    )
    cfg_path = tmp_path / "k.yaml"
    cfg_path.write_text(yaml)
    cfg = load_config(cfg_path)
    assert cfg.lifecycle().boot_timeout_s == 1800.0
