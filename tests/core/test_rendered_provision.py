"""Lockdown tests for the RenderedProvision dataclass and InstanceSpec field extensions."""

from __future__ import annotations

import dataclasses

import pytest

from kinoforge.core.interfaces import InstanceSpec, RenderedProvision


def test_rendered_provision_carries_all_five_fields() -> None:
    """RenderedProvision must expose script, run_cmd, image, ports, env_required."""
    rp = RenderedProvision(
        script="set -e\necho hi\n",
        run_cmd=["python", "main.py"],
        image="runpod/pytorch:latest",
        ports=["8188"],
        env_required=["HF_TOKEN"],
    )
    assert rp.script == "set -e\necho hi\n"
    assert rp.run_cmd == ["python", "main.py"]
    assert rp.image == "runpod/pytorch:latest"
    assert rp.ports == ["8188"]
    assert rp.env_required == ["HF_TOKEN"]


def test_rendered_provision_is_frozen() -> None:
    """RenderedProvision must be immutable so engines cannot mutate after render."""
    rp = RenderedProvision(script="", run_cmd=[], image="", ports=[], env_required=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        rp.script = "mutated"  # type: ignore[misc]


def test_instance_spec_provision_script_defaults_to_none() -> None:
    """Existing InstanceSpec callers must keep working without touching new fields."""
    spec = InstanceSpec(image="runpod/pytorch:latest")
    assert spec.provision_script is None
    assert spec.run_cmd is None


def test_instance_spec_accepts_provision_script_and_run_cmd() -> None:
    """Spec carries the rendered payload when callers populate the fields."""
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        provision_script="set -e\ngit clone ...\n",
        run_cmd=["python", "main.py", "--listen", "0.0.0.0"],
    )
    assert spec.provision_script == "set -e\ngit clone ...\n"
    assert spec.run_cmd == ["python", "main.py", "--listen", "0.0.0.0"]
