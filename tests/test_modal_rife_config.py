"""Offline: the Modal RIFE cfg loads and its provision splits build/runtime.

Milestone 4 rides the M3 fast-boot bake — the composed RIFE install must land in
build_script (baked into the image) and the server exec in runtime_script.
"""

from kinoforge.core.config import load_config
from kinoforge.engines.diffusers import DiffusersEngine

_CFG = "examples/configs/modal-rife-60fps.yaml"
_SERVER_EXEC = "python -m kinoforge.engines.diffusers.servers.wan_t2v_server"


def _render():
    return DiffusersEngine().render_provision(load_config(_CFG).model_dump())


def test_cfg_loads_modal_provider_no_cloud() -> None:
    # Bug caught: a stray `cloud:` key (SkyPilot-only) or wrong provider makes
    # the cfg route to the wrong transport / fail validation at run time.
    # NB: ComputeConfig.cloud defaults to None (no exclude_none on model_dump),
    # so the key is always PRESENT — the intent (no SkyPilot pin set) is captured
    # by asserting the value is None, not by key absence.
    d = load_config(_CFG).model_dump()
    assert d["compute"]["provider"] == "modal"
    assert d["compute"]["cloud"] is None
    assert d["interpolate"]["engine"] == "rife"
    assert d["interpolate"]["fps"] == 60.0


def test_build_script_has_rife_install_not_server() -> None:
    # Bug caught: RIFE install leaks out of the bakeable build phase (Modal can't
    # bake it → slow boot → preemption), or the server exec wrongly bakes in.
    b = _render().build_script
    assert "git clone" in b and "Practical-RIFE" in b  # RIFE repo clone
    assert "numpy<2" in b  # RIFE's pip pin
    assert "RIFEv4.26" in b  # weights zip fetch
    assert "torch==2.6.0" in b  # torch baked (slim has none)
    assert _SERVER_EXEC not in b  # server exec is runtime, never baked


def test_runtime_script_has_server_not_rife_install() -> None:
    # Bug caught: the RIFE install stays in the runtime boot → re-downloads at
    # container start, re-opening the preemption window.
    r = _render().runtime_script
    assert _SERVER_EXEC in r
    assert "git clone" not in r
    assert "numpy<2" not in r
