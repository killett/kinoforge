"""Behavior: SkyPilot's Task.setup is setup and its Task.run is the launch.

Both assertions here were FALSE at HEAD before compute-seam S3, in opposite
directions, and both were caused by ``_strip_trailing_exec`` guessing where a
setup script ended by substring-matching ``" exec "`` on its last line:

* diffusers' launch line has no ``exec``, so nothing was stripped. The server
  command stayed inside ``Task.setup`` — which therefore could never terminate —
  and ``Task.run`` launched the same server a second time.
* comfyui's launch line DID match, and the strip took ``cd /workspace/ComfyUI``
  with it. ``Task.run`` was rebuilt from ``run_cmd`` as a bare
  ``python main.py ...``, executed from the login directory where main.py does
  not exist.

These read the real shipped configs through the same capture the golden ratchet
uses, so a fix that only works on a hand-written fixture does not pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kinoforge.core.interfaces import combine_steps, render_launch
from tools.snapshot_launch_payloads import capture_launch, render_for_config


def _task_config(config_name: str) -> dict[str, Any]:
    """Return the SkyPilot task config captured for a shipped config.

    Args:
        config_name: Filename under ``examples/configs``.

    Returns:
        The ``task_config`` dict handed to ``sky.Task.from_yaml_config``.
    """
    launch = capture_launch(Path("examples/configs") / config_name)
    return dict(launch.payload["task_config"])


def test_skypilot_setup_ends_with_setup_not_with_the_server() -> None:
    """Bug caught, live at HEAD: the diffusers launch line has no ``exec``, so
    ``_strip_trailing_exec``'s ``" exec "`` substring test never fired and the
    server command stayed inside Task.setup. Setup then cannot terminate, and
    Task.run launches the same server a second time.
    """
    task_config = _task_config("skypilot-lambda-diffusers-flashvsr-upscale.yaml")
    setup_last = [
        line for line in task_config["setup"].rstrip().split("\n") if line.strip()
    ][-1]
    assert "wan_t2v_server" not in setup_last
    assert task_config["run"].count("wan_t2v_server") == 1


def test_skypilot_run_keeps_the_comfyui_workdir() -> None:
    """Bug caught, live at HEAD: the strip removed ``cd /workspace/ComfyUI &&``
    along with the exec, so Task.run ran main.py from the login directory."""
    task_config = _task_config("skypilot-lambda-comfyui.yaml")
    assert task_config["run"].startswith("cd /workspace/ComfyUI && exec python main.py")


def test_skypilot_run_is_the_rendered_launch_not_a_requoted_run_cmd() -> None:
    """``run`` comes from ``render_launch``, not from re-joining ``run_cmd``.

    Bug caught: rebuilding the line from ``run_cmd`` is precisely what dropped
    comfyui's ``cd`` and its ``exec``. Comparing against an independently
    computed ``render_launch`` — rather than against a literal — means a change
    to either side has to be made in both places on purpose.
    """
    name = "skypilot-lambda-comfyui.yaml"
    rendered = render_for_config(Path("examples/configs") / name)
    assert rendered.launch is not None
    assert _task_config(name)["run"] == render_launch(rendered.launch)


def test_skypilot_setup_is_the_watchdog_arm_plus_the_steps() -> None:
    """Setup is the arm and the steps, and nothing else.

    Bug caught: appending the combined ``script`` instead of the steps would
    put the launch back inside setup — the original bug — while every other
    assertion in this module still passed.
    """
    name = "skypilot-lambda-comfyui.yaml"
    rendered = render_for_config(Path("examples/configs") / name)
    setup = _task_config(name)["setup"]
    assert "# --- kinoforge watchdog arm" in setup
    assert setup.endswith(combine_steps(rendered.setup_steps))
