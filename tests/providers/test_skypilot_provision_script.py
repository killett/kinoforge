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
        setup_steps=(SetupStep("set -e\necho hi\n"),),
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
        launch=Launch(("python", "main.py", "--listen", "0.0.0.0")),
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert task_config["run"] == "python main.py --listen 0.0.0.0"


def test_create_instance_joins_launch_argv_verbatim_without_quoting() -> None:
    """``run`` is ``render_launch``'s output, and that does NOT quote argv.

    This is a deliberate asymmetry, pinned here because it is a real trade-off
    rather than an oversight. Before compute-seam S3 this provider rebuilt the
    line by ``shlex.quote``-joining ``run_cmd``, which is exactly what dropped
    comfyui's ``cd`` and its ``exec``. ``render_launch`` therefore emits argv
    as written; the diffusers launch is an ``env VAR=v python -m mod`` prefix
    form that quoting would collapse into one unrunnable word.

    The cost: an engine whose argv contains shell metacharacters must quote
    them itself. No shipped engine does — all three build argv from literals —
    and the golden ratchet would catch one that started.
    """
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        launch=Launch(("python", "-c", "print('hello world')")),
    )
    p.create_instance(spec)
    assert sky.launches[0][0]["run"] == "python -c print('hello world')"


def test_create_instance_without_a_launch_omits_the_run_key() -> None:
    """No launch means no ``run`` key — a BATCH spec, not a server."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest", setup_steps=(SetupStep("echo hi"),))
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "run" not in task_config


def test_create_instance_with_no_steps_still_carries_watchdog_arm() -> None:
    """A step-less spec contributes nothing extra, but ``setup`` is still armed."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest")
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "# --- kinoforge watchdog arm" in task_config["setup"]


def test_create_instance_with_only_a_launch_still_carries_watchdog_arm() -> None:
    """A launch with no steps still arms the watchdog — no spurious setup content."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest", launch=Launch(("python", "main.py")))
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "# --- kinoforge watchdog arm" in task_config["setup"]
    assert task_config["run"] == "python main.py"


def test_create_instance_appends_the_steps_verbatim() -> None:
    """The steps land after the watchdog arm unchanged.

    Nothing is stripped off the end any more. ``_strip_trailing_exec`` used to
    remove the last line whenever it contained ``" exec "``, which is how
    comfyui's ``cd /workspace/ComfyUI &&`` got discarded; the launch now
    arrives as data on ``spec.launch`` instead of being guessed at here.
    """
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    script = "set -euo pipefail\necho preparing\n"
    spec = InstanceSpec(image="img:latest", setup_steps=(SetupStep(script),))
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


def test_create_instance_renders_the_launch_rather_than_rejoining_argv() -> None:
    """``run`` is ``render_launch``'s output, workdir and all.

    Bug caught: re-deriving the line by shell-quoting and joining the argv is
    exactly what dropped comfyui's ``cd`` and its ``exec``, and it would do so
    silently while every other test here stayed green.
    """
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        setup_steps=(SetupStep("echo hi"),),
        launch=Launch(("python", "main.py"), workdir="/workspace/ComfyUI"),
    )
    p.create_instance(spec)
    assert sky.launches[0][0]["run"] == "cd /workspace/ComfyUI && python main.py"
