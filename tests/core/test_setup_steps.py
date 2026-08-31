"""Behavior: a provisioning script is a LIST of steps plus a launch, not one blob.

The blob is what forced ``_strip_trailing_exec`` to guess where setup ends and the
server starts, by substring-matching ``" exec "`` on the last line. That guess is
already wrong on both shipped engines: comfyui's launch carries a ``cd`` the strip
discards, and diffusers' launch has no ``exec`` at all, so the strip does not fire
and the server command stays inside SkyPilot's ``Task.setup``.
"""

from __future__ import annotations

import dataclasses

import pytest

from kinoforge.core.interfaces import Launch, SetupStep, combine_steps, render_launch


def test_steps_combine_in_declaration_order() -> None:
    """Order is the whole contract — it is what reproduces today's script."""
    steps = (SetupStep("a"), SetupStep("b", bakeable=True), SetupStep("c"))
    assert combine_steps(steps) == "a\nb\nc"


def test_empty_steps_are_skipped_not_rendered_as_blank_lines() -> None:
    """Bug caught: an engine that emits an empty step for a disabled feature
    would otherwise insert a blank line and move every RunPod golden."""
    assert combine_steps((SetupStep("a"), SetupStep(""), SetupStep("c"))) == "a\nc"


def test_combining_no_steps_is_the_empty_string() -> None:
    """Boundary.

    Bug caught: an implementation that unconditionally prefixes or joins with a
    leading newline turns a step-less engine's script into a blank first line,
    which on RunPod is a byte the golden does not have.
    """
    assert combine_steps(()) == ""


def test_bakeable_defaults_to_false() -> None:
    """Bug caught: defaulting to True would bake a runtime step — an ``export``
    or a keep-alive trap — into an image where it runs once and never again."""
    assert SetupStep("x").bakeable is False


def test_setup_step_and_launch_are_frozen() -> None:
    """Bug caught: dropping ``frozen=True`` makes these mutable and unhashable-by-
    value, so a spec carrying them can be edited after the wire payload is built
    and two specs that compare equal can diverge underneath a cache."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        SetupStep("x").script = "y"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Launch(("x",)).workdir = "/tmp"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("launch", "expected"),
    [
        (Launch(("python", "main.py")), "python main.py"),
        (Launch(("python", "main.py"), exec_pid1=True), "exec python main.py"),
        (
            Launch(("python", "main.py"), workdir="/workspace/ComfyUI"),
            "cd /workspace/ComfyUI && python main.py",
        ),
        (
            Launch(("python", "main.py"), workdir="/workspace/ComfyUI", exec_pid1=True),
            "cd /workspace/ComfyUI && exec python main.py",
        ),
    ],
)
def test_render_launch_covers_all_four_shapes(launch: Launch, expected: str) -> None:
    """Bug caught: collapsing the two flags into one would force every engine
    into comfyui's shape and destroy the diffusers EXIT trap (see the plan's
    header table)."""
    assert render_launch(launch) == expected


def test_render_launch_quotes_the_workdir_but_not_the_argv() -> None:
    """The asymmetry is deliberate, so pin it.

    Bug caught: quoting argv would wrap the diffusers launch — which is
    ``env VAR=v python -m mod``, not a bare argv — into a single quoted word that
    bash would try to execute as one filename.
    """
    rendered = render_launch(Launch(("env", "A=1", "python"), workdir="/a b"))
    assert rendered == "cd '/a b' && env A=1 python"


def test_render_launch_refuses_none() -> None:
    """Bug caught: returning "" for a missing launch lets a provider silently
    ship a script that starts no server."""
    with pytest.raises(ValueError, match="no launch"):
        render_launch(None)


def test_spec_builder_threads_the_new_pair_and_keeps_the_old_fields() -> None:
    """Additive migration: both representations coexist until Task 7.

    Bug caught: switching the spec over in one commit would strand every one of
    the ~54 test modules that construct an InstanceSpec with provision_script.
    A builder that accepts the new fields but drops them on the floor is caught
    too — the provider would then see ``()`` and fall back forever.
    """
    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import (
        Lifecycle,
        Offer,
        RenderedProvision,
    )
    from kinoforge.core.spec_builder import build_instance_spec

    cfg = Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "bf16"},
            "spec": {"model": "m", "precision": "bf16"},
            "models": [{"kind": "base", "ref": "hf:org/repo", "target": "checkpoints"}],
            "compute": {"provider": "runpod", "image": "i"},
        }
    )
    rendered = RenderedProvision(
        script="install\nrun-server",
        run_cmd=["run-server"],
        image="img:tag",
        ports=["8000"],
        env_required=[],
        setup_steps=(SetupStep("install", bakeable=True),),
        launch=Launch(("run-server",)),
    )
    spec = build_instance_spec(
        cfg=cfg,
        rendered=rendered,
        offer=Offer(
            id="g", gpu_type="g", vram_gb=80, cuda="12.4", cost_rate_usd_per_hr=1.0
        ),
        engine_name="diffusers",
        key_hash="abc",
        image="fallback:img",
        lifecycle=Lifecycle(),
        env={},
        run_id="run-1",
    )
    assert spec.setup_steps == (SetupStep("install", bakeable=True),)
    assert spec.launch == Launch(("run-server",))
    # The old representation is untouched — that is what keeps the tree green.
    assert spec.provision_script == "install\nrun-server"
    assert spec.run_cmd == ["run-server"]


def test_an_engine_that_emits_no_steps_gets_no_synthesised_launch() -> None:
    """Bug caught: defaulting ``launch`` to ``Launch(tuple(run_cmd))`` would put
    a plausible-looking launch on every legacy spec and hide, rather than
    surface, the engines that have not been migrated yet."""
    from kinoforge.core.interfaces import RenderedProvision

    rendered = RenderedProvision(
        script="echo hi",
        run_cmd=["sleep", "infinity"],
        image="i",
        ports=[],
        env_required=[],
    )
    assert rendered.setup_steps == ()
    assert rendered.launch is None
