"""Behavior: Modal partitions the engine's steps on ``bakeable`` / ``runtime``.

Modal is the only provider that runs part of the provision at IMAGE-BUILD time
and the rest at container start. Before compute-seam S3 the engine handed it two
pre-split strings (``image_build_script`` / ``runtime_provision_script``) — field
names describing Modal's pipeline rather than any property of a step. The flags
say the same thing about the STEP, so every provider can route it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kinoforge.core.interfaces import combine_steps, render_launch
from tools.snapshot_launch_payloads import capture_launch, render_for_config

_CONFIG = "modal-diffusers-wan-2_1-1_3b-t2v.yaml"


def _request(config_name: str) -> dict[str, Any]:
    """Return the ``ModalAppRequest`` fields captured for a shipped config."""
    launch = capture_launch(Path("examples/configs") / config_name)
    return dict(launch.payload["request"])


def test_modal_bakes_exactly_the_bakeable_steps() -> None:
    """Bug caught: partitioning on the wrong side of the flag either bakes a
    runtime ``export`` into the image (where it is lost per container) or leaves
    a multi-GB weights fetch at container start, which is the boot stall the
    fast-boot image bake existed to fix."""
    rendered = render_for_config(Path("examples/configs") / _CONFIG)
    request = _request(_CONFIG)
    bakeable = tuple(s for s in rendered.setup_steps if s.bakeable)
    assert bakeable, "this config must have bakeable steps or the test is vacuous"
    # The build phase runs in isolation, so it carries its own fail-fast line;
    # the combined script's `set -euo pipefail` is in the runtime preamble.
    assert request["image_build_script"] == "set -euo pipefail\n" + combine_steps(
        bakeable
    )


def test_modal_boots_exactly_the_runtime_steps() -> None:
    """The other half of the partition.

    Bug caught: dropping the steps that are BOTH bakeable and runtime — the
    module embed — leaves the container without ``PYTHONPATH=/tmp/kfsrv``, so
    the server cannot import and the pod boots into an import error.
    """
    rendered = render_for_config(Path("examples/configs") / _CONFIG)
    runtime = tuple(s for s in rendered.setup_steps if s.runtime)
    assert _request(_CONFIG)["provision_script"] == combine_steps(runtime)


def test_modal_boot_script_launches_once() -> None:
    """Bug caught: ``_app.py`` appended ``exec <run_cmd>`` on top of a runtime
    script that already ended with the diffusers launch. Harmless only because
    the first one blocks forever — which is a latent double-launch, not a
    design."""
    request = _request(_CONFIG)
    boot = request["provision_script"] + "\n" + request["launch_line"]
    # Count the COMMAND, not the bare module name: the setup steps embed
    # ``.../servers/wan_t2v_server.py`` as a file, so the name legitimately
    # appears there and a looser count would never be 1.
    server_cmd = "python -m kinoforge.engines.diffusers.servers.wan_t2v_server"
    assert boot.count(server_cmd) == 1
    assert boot.endswith(server_cmd)


def test_modal_launch_line_is_the_rendered_launch() -> None:
    """The launch rides the request as data, composed by the engine.

    Bug caught: re-deriving it in ``_app.py`` as ``"exec " + shlex.join(run_cmd)``
    puts an ``exec`` on the diffusers launch, replacing the bash that owns the
    EXIT trap — the one thing that engine's comment says must not happen.
    """
    rendered = render_for_config(Path("examples/configs") / _CONFIG)
    assert rendered.launch is not None
    assert _request(_CONFIG)["launch_line"] == render_launch(rendered.launch)
    assert not _request(_CONFIG)["launch_line"].startswith("exec ")
