"""Tests for SkyPilotProvider create_instance setup/run mapping."""

from __future__ import annotations

from typing import Any

from kinoforge.core.interfaces import InstanceSpec
from kinoforge.providers.skypilot import SkyPilotProvider


class _FakeSky:
    """Minimal sky-client stub that records launches."""

    def __init__(self) -> None:
        self.launches: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def launch(self, task_config: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.launches.append((task_config, kwargs))
        return {"cluster_name": "fake-cluster"}

    def status(self) -> list[dict[str, Any]]:
        return []

    def down(self, name: str) -> None: ...

    def gpu_list(self) -> list[dict[str, Any]]:
        return []


def test_create_instance_without_provision_script_omits_setup_run() -> None:
    """Default callers (pre-Layer-Q) keep working: no setup/run keys in task_config."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest")
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "setup" not in task_config
    assert "run" not in task_config


def test_create_instance_with_provision_script_maps_to_setup() -> None:
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        provision_script="set -e\necho hi\n",
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert task_config["setup"] == "set -e\necho hi\n"
    assert "run" not in task_config


def test_create_instance_with_run_cmd_maps_shell_quoted_to_run() -> None:
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        run_cmd=["python", "main.py", "--listen", "0.0.0.0"],
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert task_config["run"] == "python main.py --listen 0.0.0.0"


def test_create_instance_with_args_containing_spaces_shell_quotes_them() -> None:
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        run_cmd=["python", "-c", "print('hello world')"],
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    # shlex.quote wraps the arg with single quotes when it contains shell meta-chars
    assert task_config["run"] == "python -c 'print('\"'\"'hello world'\"'\"')'"


def test_create_instance_with_empty_run_cmd_omits_run_key() -> None:
    """Empty run_cmd is treated as 'not set' — no `run` key emitted."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest", run_cmd=[])
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "run" not in task_config


def test_create_instance_with_empty_provision_script_omits_setup_key() -> None:
    """Empty provision_script is treated as 'not set' — no `setup` key emitted."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest", provision_script="")
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "setup" not in task_config


def test_create_instance_with_only_run_cmd_omits_setup_key() -> None:
    """Setting run_cmd alone produces only the `run` key — no spurious `setup`."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest", run_cmd=["python", "main.py"])
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "setup" not in task_config
    assert task_config["run"] == "python main.py"
