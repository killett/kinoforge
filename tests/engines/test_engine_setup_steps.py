"""Behavior: an engine's steps + launch reproduce its script exactly.

This is the invariant that makes S3 safe to land. If concatenating the steps and
appending the rendered launch does not reproduce today's byte stream, the RunPod
goldens move — and a moved RunPod golden in this stage means a real boot-script
change nobody asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import Launch, SetupStep, combine_steps, render_launch
from tools.snapshot_launch_payloads import compute_configs, render_for_config

#: The FAKE ENGINE is the one thing the reproduce-the-script invariant does NOT
#: cover, and it is excluded by engine kind — not by filename, which would miss
#: ``cost.yaml`` and ``sweeper.yaml``, two configs that also run it. Its script
#: is ``echo fake`` while its launch is ``sleep infinity``: the two have never
#: been connected, so declaring the launch (Task 3) deliberately does not add a
#: line to the script. ``test_fake_engine_declares_a_launch_its_script_never_had``
#: pins that exception directly.
_INVARIANT_EXCLUDED_ENGINE = "fake"


def _configs_for(engine_kind: str | None = None) -> list[Path]:
    """Return shipped compute configs, optionally narrowed to one engine.

    Selected by the config's declared engine rather than by filename, so a
    config that does not name its engine in its filename is still covered.

    Args:
        engine_kind: When given, keep only configs declaring this engine.

    Returns:
        Sorted config paths, excluding every config on the fake engine.
    """
    kinds = {p: load_config(str(p)).engine.kind for p in compute_configs()}
    return [
        p
        for p, kind in kinds.items()
        if kind != _INVARIANT_EXCLUDED_ENGINE
        and (engine_kind is None or kind == engine_kind)
    ]


_DIFFUSERS_CONFIGS = _configs_for("diffusers")
_ALL_CONFIGS = _configs_for()


def test_the_config_sets_are_not_empty_and_cover_more_than_one_engine() -> None:
    """Tripwire.

    Bug caught: a selector that matches nothing turns every parametrized test
    below into zero cases, which reads as a green suite while proving nothing.
    The engine-count half catches a widened set that is still diffusers-only,
    which would leave comfyui's workdir unproven.
    """
    assert len(_DIFFUSERS_CONFIGS) >= 10
    assert len(_ALL_CONFIGS) > len(_DIFFUSERS_CONFIGS)
    kinds = {load_config(str(p)).engine.kind for p in _ALL_CONFIGS}
    assert "comfyui" in kinds


@pytest.mark.parametrize("cfg_path", _ALL_CONFIGS, ids=lambda p: p.stem)
def test_steps_plus_launch_reproduce_the_script(cfg_path: Path) -> None:
    """The ordering invariant, over every shipped config but the fake one.

    Bug caught: a step emitted out of declaration order, a dropped newline
    between the last step and the launch, or a launch that no longer renders
    back to the line it replaced — each silently rewrites a RunPod boot script.
    """
    rendered = render_for_config(cfg_path)
    assert rendered.launch is not None, f"{cfg_path} rendered no launch"
    rebuilt = (
        combine_steps(rendered.setup_steps) + "\n" + render_launch(rendered.launch)
    )
    assert rebuilt == rendered.script


@pytest.mark.parametrize("cfg_path", _DIFFUSERS_CONFIGS, ids=lambda p: p.stem)
def test_every_step_runs_somewhere(cfg_path: Path) -> None:
    """No step may fall through both routing flags.

    Bug caught: a step tagged neither ``bakeable`` nor ``runtime`` is baked
    into no image and run in no container — it simply never executes, and
    because it still appears in the human-readable ``script`` there is nothing
    in a diff to show it. Modal is the provider that would silently lose it.
    """
    rendered = render_for_config(cfg_path)
    orphans = [s for s in rendered.setup_steps if not s.bakeable and not s.runtime]
    assert orphans == [], f"{cfg_path} has steps that execute nowhere"


@pytest.mark.parametrize("cfg_path", _DIFFUSERS_CONFIGS, ids=lambda p: p.stem)
def test_the_module_embed_is_in_both_partitions(cfg_path: Path) -> None:
    """A step can be bakeable AND runtime, and one of them has to be.

    Bug caught: collapsing the two flags into one drops the module-embed lines
    out of whichever partition loses them. The image needs ``/tmp/kfsrv`` so
    the build-phase weights fetch can resolve ``python -m kinoforge...``, and
    the container needs it so the server imports — a Modal container booting
    without ``PYTHONPATH=/tmp/kfsrv`` dies at import with nothing in the diff
    to explain why.
    """
    rendered = render_for_config(cfg_path)
    embed = [s for s in rendered.setup_steps if "PYTHONPATH=/tmp/kfsrv" in s.script]
    if not embed:
        pytest.skip("this config embeds no module tree")
    assert all(s.bakeable and s.runtime for s in embed)


def test_diffusers_launch_does_not_exec_because_bash_must_stay_pid_1() -> None:
    """Bug caught: exec_pid1=True here replaces the bash that owns the EXIT
    trap, so a crashed server would leave a pod alive with nothing listening —
    the failure mode the trap was added to end."""
    rendered = render_for_config(
        Path("examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml")
    )
    assert rendered.launch is not None
    assert rendered.launch.exec_pid1 is False
    assert rendered.launch.workdir == ""


def test_comfyui_launch_carries_the_workdir_the_strip_used_to_eat() -> None:
    """Bug caught, and it is live today: ``_strip_trailing_exec`` removes
    ``cd /workspace/ComfyUI && exec python main.py ...`` wholesale, so SkyPilot's
    Task.run is ``python main.py`` from the login directory — where main.py does
    not exist. The workdir has to survive as data.
    """
    rendered = render_for_config(Path("examples/configs/skypilot-lambda-comfyui.yaml"))
    assert rendered.launch is not None
    assert rendered.launch.workdir == "/workspace/ComfyUI"
    assert rendered.launch.exec_pid1 is True
    assert rendered.launch.argv[:2] == ("python", "main.py")
    rebuilt = (
        combine_steps(rendered.setup_steps) + "\n" + render_launch(rendered.launch)
    )
    assert rebuilt == rendered.script


def test_comfyui_emits_one_non_bakeable_step() -> None:
    """comfyui has no build/runtime split today.

    Bug caught: inventing one here would be a behaviour change wearing a
    refactor's clothes — Modal would bake comfyui's model downloads into an
    image that no comfyui config asks for, and the step would stop re-running
    per container.
    """
    rendered = render_for_config(Path("examples/configs/skypilot-lambda-comfyui.yaml"))
    assert len(rendered.setup_steps) == 1
    assert rendered.setup_steps[0].bakeable is False
    assert rendered.setup_steps[0].runtime is True


def test_fake_engine_declares_a_launch_its_script_never_had() -> None:
    """The fake engine is the exception to the reproduce-the-script invariant.

    Its script is ``echo fake`` and its run_cmd is ``sleep infinity``, and the
    two have never been connected: on RunPod the container runs ``echo fake``
    and exits. Declaring the launch makes that reachable for the first time. It
    moves no golden because the only config using this engine runs on the local
    provider, which starts nothing.

    Bug caught: quietly letting fake inherit the invariant would force a
    ``sleep infinity`` line into its script and change what a fake-engine
    container does.
    """
    rendered = render_for_config(Path("examples/configs/local-fake.yaml"))
    assert rendered.setup_steps == (SetupStep("echo fake"),)
    assert rendered.launch == Launch(("sleep", "infinity"))
    assert "sleep" not in rendered.script


def test_diffusers_launch_argv_is_the_server_command() -> None:
    """Bug caught: a launch built from the wrong list (the pip deps, the embed
    lines) would render a line that is not the server, and the container would
    provision and then exit with the pod still billing."""
    rendered = render_for_config(
        Path("examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml")
    )
    assert rendered.launch is not None
    server_cmd = " ".join(rendered.launch.argv)
    assert "wan_t2v_server" in server_cmd
    # And the COMMAND is not left behind in the steps — that is what lets
    # SkyPilot's Task.setup terminate. (The embedded `wan_t2v_server.py` FILE
    # legitimately appears in a step; the command form is what matters.)
    assert not any(server_cmd in s.script for s in rendered.setup_steps)
