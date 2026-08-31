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
from kinoforge.core.interfaces import combine_steps, render_launch
from tools.snapshot_launch_payloads import compute_configs, render_for_config

#: The build phase runs the bakeable steps in ISOLATION, so it needs its own
#: fail-fast line: the combined script's ``set -euo pipefail`` lives in the
#: runtime preamble, which a baked image never executes. That prefix is
#: therefore the one byte-difference between ``build_script`` and the bakeable
#: steps, and it is stated here rather than derived from the engine.
_BUILD_PREAMBLE = "set -euo pipefail\n"


def _diffusers_configs() -> list[Path]:
    """Return every shipped config whose engine is diffusers.

    Selected by the config's declared engine rather than by filename, so a
    diffusers config that does not say "diffusers" in its name is still
    covered.

    Returns:
        Sorted config paths.
    """
    return [
        p for p in compute_configs() if load_config(str(p)).engine.kind == "diffusers"
    ]


_DIFFUSERS_CONFIGS = _diffusers_configs()


def test_the_diffusers_config_set_is_not_empty() -> None:
    """Tripwire.

    Bug caught: a selector that matches nothing turns every parametrized test
    below into zero cases, which reads as a green suite while proving nothing.
    """
    assert len(_DIFFUSERS_CONFIGS) >= 10


@pytest.mark.parametrize("cfg_path", _DIFFUSERS_CONFIGS, ids=lambda p: p.stem)
def test_steps_plus_launch_reproduce_the_script(cfg_path: Path) -> None:
    """The ordering invariant, over every shipped diffusers config.

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
def test_bakeable_steps_reproduce_the_build_script(cfg_path: Path) -> None:
    """``bakeable`` is exactly the old build bucket.

    Bug caught: a step mis-tagged bakeable gets baked into the image and never
    re-runs per container — an ``export`` or a keep-alive trap tagged that way is
    silently absent at runtime, which is a boot failure nobody can see in a diff.
    The reverse mis-tag leaves a multi-GB weights fetch at container start, which
    is the boot stall the fast-boot image bake exists to prevent.
    """
    rendered = render_for_config(cfg_path)
    bakeable = combine_steps(tuple(s for s in rendered.setup_steps if s.bakeable))
    expected = _BUILD_PREAMBLE + bakeable if bakeable else ""
    assert expected == rendered.build_script


@pytest.mark.parametrize("cfg_path", _DIFFUSERS_CONFIGS, ids=lambda p: p.stem)
def test_non_bakeable_steps_plus_launch_reproduce_the_runtime_script(
    cfg_path: Path,
) -> None:
    """The other half of the partition, which is what Modal boots.

    Bug caught: collapsing ``bakeable`` and ``runtime`` into one flag drops the
    module-embed lines — which are BOTH — out of whichever script loses them.
    Modal boots this partition, so it would start without
    ``PYTHONPATH=/tmp/kfsrv`` and the server would not import.
    """
    rendered = render_for_config(cfg_path)
    assert rendered.launch is not None
    runtime = combine_steps(tuple(s for s in rendered.setup_steps if s.runtime))
    rebuilt = runtime + "\n" + render_launch(rendered.launch)
    assert rebuilt == rendered.runtime_script


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


def test_diffusers_launch_argv_is_the_server_command() -> None:
    """Bug caught: a launch built from the wrong list (the pip deps, the embed
    lines) would render a line that is not the server, and the container would
    provision and then exit with the pod still billing."""
    rendered = render_for_config(
        Path("examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml")
    )
    assert rendered.launch is not None
    assert tuple(rendered.run_cmd) == rendered.launch.argv
    assert "wan_t2v_server" in " ".join(rendered.launch.argv)
