"""Behavior: render_provision splits into build/runtime phases; script unchanged.

The Modal fast-boot image-bake feature (2026-07-10) needs the slow install
steps (pip, BSA wheel, FlashVSR weights) separated from the fast container-start
steps (log surface, trap, embed, exec server) so Modal can bake the former into
the image at build time. RunPod still provisions at runtime and must see the
combined ``script`` unchanged — hence the golden byte-identity test.
"""

import json
from pathlib import Path

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import RenderedProvision
from kinoforge.engines.diffusers import DiffusersEngine

_GOLDEN = json.loads(Path("tests/engines/diffusers/_golden_provision.json").read_text())
_FLASHVSR = "examples/configs/modal-flashvsr-x4.yaml"
_WAN = "examples/configs/modal-wan-t2v-1_3b.yaml"


def _render(path: str) -> RenderedProvision:
    return DiffusersEngine().render_provision(load_config(path).model_dump())


def test_script_is_byte_identical_to_golden():
    # Bug caught: a careless refactor reorders/duplicates lines -> the RunPod
    # boot script drifts from what shipped, silently changing provisioning.
    for path, golden in _GOLDEN.items():
        assert _render(path).script == golden, f"{path} script drifted"


def test_flashvsr_build_script_has_installs_not_runtime():
    # Bug caught: pip/BSA/weights leak out of build_script -> Modal can't bake
    # them, or runtime-only bits (server exec, sidecar, trap) get baked into the
    # image where they don't belong.
    b = _render(_FLASHVSR).build_script
    assert "pip install" in b
    assert "block_sparse_attn" in b  # BSA wheel curl+install (composed upscaler)
    assert "FlashVSR" in b or "flashvsr" in b  # weights fetch
    # runtime-only bits must NOT be in the bakeable build script:
    assert "sleep infinity" not in b
    assert "http.server 8001" not in b
    assert "wan_t2v_server" not in b  # server exec is runtime, never baked
    assert "/tmp/bootstrap.log" not in b  # runtime log redirect


def test_flashvsr_runtime_script_has_server_not_installs():
    # Bug caught: the heavy installs stay in runtime_script -> Modal re-downloads
    # everything at container start, re-opening the ~15min preemption window that
    # killed the 2026-07-09 FlashVSR live run.
    r = _render(_FLASHVSR).runtime_script
    assert "wan_t2v_server" in r  # the server_cmd line
    assert "/tmp/bootstrap.log" in r  # runtime log redirect
    assert "sleep infinity" in r  # keep-alive trap preamble
    assert "block_sparse_attn" not in r  # BSA is baked, not runtime
    assert "torch==2.6.0" not in r  # the heavy pip line is baked, not runtime
    assert "pip install" not in r


def test_wan_cfg_without_upscaler_has_no_build_script():
    # Bug caught: a plain Wan t2v cfg (pip only) should still populate build_script
    # with its pip line but never with upscaler/server bits; runtime carries server.
    rp = _render(_WAN)
    assert "pip install" in rp.build_script
    assert "wan_t2v_server" not in rp.build_script
    assert "wan_t2v_server" in rp.runtime_script
    assert "pip install" not in rp.runtime_script
