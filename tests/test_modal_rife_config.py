"""Offline: the Modal RIFE cfg loads and its provision splits build/runtime.

Milestone 4 rides the M3 fast-boot bake — the composed RIFE install must land in
the bakeable steps (baked into the image) and the server launch off both.
"""

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import combine_steps
from kinoforge.engines.diffusers import DiffusersEngine

_CFG = "examples/configs/modal-diffusers-rife-60fps-interpolate.yaml"
_SERVER_EXEC = "python -m kinoforge.engines.diffusers.servers.wan_t2v_server"


def _render():
    return DiffusersEngine().render_provision(load_config(_CFG).model_dump())


def test_cfg_loads_modal_provider_no_cloud() -> None:
    # Bug caught: a stray skypilot backend_options namespace (SkyPilot-only)
    # or wrong provider makes the cfg route to the wrong transport / fail
    # validation at run time.
    d = load_config(_CFG).model_dump()
    assert d["compute"]["provider"] == "modal"
    assert "skypilot" not in d["compute"]["backend_options"]
    assert d["interpolate"]["engine"] == "rife"
    assert d["interpolate"]["fps"] == 60.0


def test_build_script_has_rife_install_not_server() -> None:
    # Bug caught: RIFE install leaks out of the bakeable build phase (Modal can't
    # bake it → slow boot → preemption), or the server exec wrongly bakes in.
    b = combine_steps(tuple(s for s in _render().setup_steps if s.bakeable))
    assert "git clone" in b and "Practical-RIFE" in b  # RIFE repo clone
    assert "numpy<2" in b  # RIFE's pip pin
    assert "RIFEv4.26" in b  # weights zip fetch
    assert "torch==2.6.0" in b  # torch baked (slim has none)
    # core.frames MUST be embedded: the RIFE runtime imports ffprobe_fps from it
    # (interpolators/rife/_runtime.py). Omitting it fails the server at run time
    # with `No module named 'kinoforge.core.frames'` (live-caught 2026-07-11).
    assert "core/frames.py" in b
    assert _SERVER_EXEC not in b  # server exec is runtime, never baked


def test_runtime_script_has_server_not_rife_install() -> None:
    # Bug caught: the RIFE install stays in the runtime boot → re-downloads at
    # container start, re-opening the preemption window.
    rendered = _render()
    r = combine_steps(tuple(s for s in rendered.setup_steps if s.runtime))
    # S3 moved the server COMMAND out of the scripts onto `launch`; the runtime
    # partition is what Modal boots BEFORE it, and the launch is what starts it.
    assert rendered.launch is not None
    assert _SERVER_EXEC in " ".join(rendered.launch.argv)
    assert "git clone" not in r
    assert "numpy<2" not in r
