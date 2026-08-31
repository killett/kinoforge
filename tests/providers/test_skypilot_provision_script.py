"""Tests for SkyPilotProvider create_instance setup/run mapping."""

from __future__ import annotations

from typing import Any

from kinoforge.core.interfaces import InstanceSpec, Launch, SetupStep
from kinoforge.providers.skypilot import SkyPilotProvider


class _FakeTask:
    """Stand-in for :class:`sky.Task` carrying the config dict for inspection."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config: dict[str, Any] = config


class _FakeTaskNamespace:
    """Stand-in for ``sky.Task`` exposing the ``from_yaml_config`` factory."""

    def __init__(self) -> None:
        self.configs: list[dict[str, Any]] = []

    def from_yaml_config(self, config: dict[str, Any]) -> _FakeTask:
        self.configs.append(config)
        return _FakeTask(config)


class _FakeSky:
    """Minimal sky-client stub that records launches.

    ``launches`` is kept on the public API but each entry's first element is
    now the *task config dict* (extracted from the :class:`_FakeTask` the
    provider builds via :meth:`Task.from_yaml_config`) so existing assertions
    continue to read like ``sky.launches[0][0]["setup"]``.
    """

    def __init__(self) -> None:
        self.launches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.Task: _FakeTaskNamespace = _FakeTaskNamespace()

    def launch(self, task: Any, **kwargs: Any) -> tuple[None, None]:  # noqa: ANN401
        config: dict[str, Any] = task.config if isinstance(task, _FakeTask) else task
        self.launches.append((config, kwargs))
        return (None, None)

    def status(self) -> list[dict[str, Any]]:
        return []

    def down(self, name: str) -> None: ...

    def gpu_list(self) -> list[dict[str, Any]]:
        return []


def test_create_instance_without_provision_script_omits_run() -> None:
    """No provision_script: no ``run`` key, but ``setup`` still carries the watchdog arm.

    Setup is no longer conditional on ``provision_script`` (2026-08-15
    instance-deadline design) — every launched cluster is armed, including
    provision-script-less deploys like the CPU smoke.
    """
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest")
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "# --- kinoforge watchdog arm" in task_config["setup"]
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
    assert task_config["setup"].endswith("set -e\necho hi\n")
    assert "# --- kinoforge watchdog arm" in task_config["setup"]
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


def test_create_instance_with_empty_provision_script_still_carries_watchdog_arm() -> (
    None
):
    """Empty provision_script contributes nothing extra, but ``setup`` is still armed."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest", provision_script="")
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "# --- kinoforge watchdog arm" in task_config["setup"]


def test_create_instance_with_only_run_cmd_still_carries_watchdog_arm() -> None:
    """Setting run_cmd alone still arms the watchdog in `setup` — no spurious script content."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest", run_cmd=["python", "main.py"])
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "# --- kinoforge watchdog arm" in task_config["setup"]
    assert task_config["run"] == "python main.py"


def test_create_instance_preserves_a_legacy_script_unchanged() -> None:
    """A legacy ``provision_script`` is appended after the watchdog arm verbatim.

    Nothing is stripped off the end any more. ``_strip_trailing_exec`` used to
    remove the last line whenever it contained ``" exec "``, which is how
    comfyui's ``cd /workspace/ComfyUI &&`` got discarded; the launch now
    arrives as data on ``spec.launch`` instead of being guessed at here.
    """
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    script = "set -euo pipefail\necho preparing\n"
    spec = InstanceSpec(image="img:latest", provision_script=script)
    p.create_instance(spec)
    assert sky.launches[0][0]["setup"].endswith(script)


def test_create_instance_maps_setup_steps_to_setup_and_launch_to_run() -> None:
    """The split, at the provider's own seam.

    Bug caught: putting the launch in ``setup`` (or the steps in ``run``)
    reproduces the exact hang this stage removed — ``Task.setup`` cannot
    terminate while it is running a server, so ``Task.run`` never starts.
    """
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        setup_steps=(SetupStep("pip install -q x"), SetupStep("echo ready")),
        launch=Launch(("python", "main.py"), workdir="/srv", exec_pid1=True),
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert task_config["setup"].endswith("pip install -q x\necho ready")
    assert task_config["run"] == "cd /srv && exec python main.py"


def test_create_instance_prefers_the_launch_over_a_stale_run_cmd() -> None:
    """Precedence, pinned.

    Bug caught: reading ``run_cmd`` first re-introduces the shell-quoted
    reconstruction that dropped comfyui's ``cd`` and its ``exec``, and it would
    do so silently while every other test here stayed green.
    """
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        setup_steps=(SetupStep("echo hi"),),
        launch=Launch(("python", "main.py"), workdir="/workspace/ComfyUI"),
        run_cmd=["python", "main.py"],
    )
    p.create_instance(spec)
    assert sky.launches[0][0]["run"] == "cd /workspace/ComfyUI && python main.py"
