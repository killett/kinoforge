"""Lockdown tests for the RenderedProvision dataclass and InstanceSpec field extensions."""

from __future__ import annotations

import dataclasses

import pytest

from kinoforge.core.interfaces import InstanceSpec, Launch, RenderedProvision, SetupStep


def test_rendered_provision_carries_all_its_fields() -> None:
    """RenderedProvision must expose script, image, ports, env_required, and the pair."""
    rp = RenderedProvision(
        script="set -e\necho hi\npython main.py\n",
        setup_steps=(SetupStep("set -e\necho hi"),),
        launch=Launch(("python", "main.py")),
        image="runpod/pytorch:latest",
        ports=["8188"],
        env_required=["HF_TOKEN"],
    )
    assert rp.script == "set -e\necho hi\npython main.py\n"
    assert rp.setup_steps == (SetupStep("set -e\necho hi"),)
    assert rp.launch == Launch(("python", "main.py"))
    assert rp.image == "runpod/pytorch:latest"
    assert rp.ports == ["8188"]
    assert rp.env_required == ["HF_TOKEN"]


def test_rendered_provision_is_frozen() -> None:
    """RenderedProvision must be immutable so engines cannot mutate after render."""
    rp = RenderedProvision(script="", image="", ports=[], env_required=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        rp.script = "mutated"  # type: ignore[misc]


def test_instance_spec_setup_pair_defaults_to_empty() -> None:
    """Existing InstanceSpec callers must keep working without touching new fields.

    Bug caught: defaulting ``launch`` to anything but None would make every
    bare spec look like a server, and the workload-shape check reads exactly
    this to tell SERVER from BATCH.
    """
    spec = InstanceSpec(image="runpod/pytorch:latest")
    assert spec.setup_steps == ()
    assert spec.launch is None


def test_instance_spec_accepts_the_setup_run_pair() -> None:
    """Spec carries the rendered payload when callers populate the fields."""
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        setup_steps=(SetupStep("set -e\ngit clone ...\n"),),
        launch=Launch(("python", "main.py", "--listen", "0.0.0.0")),
    )
    assert spec.setup_steps == (SetupStep("set -e\ngit clone ...\n"),)
    assert spec.launch == Launch(("python", "main.py", "--listen", "0.0.0.0"))
