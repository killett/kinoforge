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


def test_create_instance_strips_trailing_exec_from_setup_script() -> None:
    """provision_script ending in `exec <cmd>` has that line removed in Task.setup."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    script = (
        "set -euo pipefail\n"
        "cd /workspace\n"
        "git clone --depth 1 https://example/x.git\n"
        "cd /workspace/ComfyUI && exec python main.py --port 8188\n"
    )
    spec = InstanceSpec(
        image="img:latest",
        provision_script=script,
        run_cmd=["python", "main.py", "--port", "8188"],
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "exec " not in task_config["setup"], task_config["setup"]
    assert "python main.py" in task_config["run"]


def test_create_instance_preserves_script_without_trailing_exec() -> None:
    """provision_script without a trailing exec line is passed through unchanged."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    script = "set -euo pipefail\necho preparing\n"
    spec = InstanceSpec(image="img:latest", provision_script=script)
    p.create_instance(spec)
    assert sky.launches[0][0]["setup"] == script


def test_create_instance_strips_diffusers_bare_exec_line() -> None:
    """Diffusers script ends with `exec python -m diffusers_server` (bare exec, no &&).

    Regression for the dual-exec hazard — _strip_trailing_exec must handle both
    ComfyUI's `cd ... && exec ...` shape AND Diffusers' bare `exec ...` shape.
    """
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    script = (
        "set -euo pipefail\n"
        "pip install -q diffusers transformers\n"
        "exec python -m diffusers_server\n"
    )
    spec = InstanceSpec(
        image="img:latest",
        provision_script=script,
        run_cmd=["python", "-m", "diffusers_server"],
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "exec " not in task_config["setup"], task_config["setup"]
    assert task_config["run"] == "python -m diffusers_server"
