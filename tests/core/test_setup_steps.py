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


def test_bakeable_defaults_to_false_and_runtime_defaults_to_true() -> None:
    """Bug caught: defaulting ``bakeable`` to True would bake a runtime step —
    an ``export`` or a keep-alive trap — into an image where it runs once and
    never again. Defaulting ``runtime`` to False would drop every unannotated
    step out of the container-start script entirely."""
    assert SetupStep("x").bakeable is False
    assert SetupStep("x").runtime is True


def test_the_two_flags_are_independent_so_a_step_can_be_both() -> None:
    """The tri-state the engines actually need, expressed as two booleans.

    Bug caught: a single flag cannot express "runs at bake time AND at
    container start". The diffusers module embed is exactly that — the image
    needs ``/tmp/kfsrv`` so the build-phase weights fetch resolves
    ``python -m kinoforge...``, and the container needs it so the server
    imports. Collapsing the two would drop those lines from one of the two
    scripts, and a container booting without ``PYTHONPATH=/tmp/kfsrv`` fails
    at import with nothing in the diff to show why.
    """
    both = SetupStep("embed", bakeable=True, runtime=True)
    build_only = SetupStep("pip install", bakeable=True, runtime=False)
    runtime_only = SetupStep("export A=1", bakeable=False, runtime=True)
    steps = (both, build_only, runtime_only)
    assert combine_steps(tuple(s for s in steps if s.bakeable)) == "embed\npip install"
    assert combine_steps(tuple(s for s in steps if s.runtime)) == "embed\nexport A=1"


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


def test_spec_builder_threads_the_new_pair() -> None:
    """The builder carries the pair through untouched.

    Bug caught: a builder that accepts the new fields but drops them on the
    floor — the provider then sees ``()`` and provisions an empty script, and
    the pod comes up bare with nothing saying so.
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


def test_an_engine_that_emits_no_steps_gets_no_synthesised_launch() -> None:
    """Bug caught: defaulting ``launch`` to ``Launch(tuple(run_cmd))`` would put
    a plausible-looking launch on every legacy spec and hide, rather than
    surface, the engines that have not been migrated yet."""
    from kinoforge.core.interfaces import RenderedProvision

    rendered = RenderedProvision(
        script="echo hi",
        image="i",
        ports=[],
        env_required=[],
    )
    assert rendered.setup_steps == ()
    assert rendered.launch is None
