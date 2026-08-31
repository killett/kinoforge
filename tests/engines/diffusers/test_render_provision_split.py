"""Behavior: render_provision splits into build/runtime phases; script unchanged.

The Modal fast-boot image-bake feature (2026-07-10) needs the slow install
steps (pip, BSA wheel, FlashVSR weights) separated from the fast container-start
steps (log surface, trap, embed) so Modal can bake the former into the image at
build time. RunPod still provisions at runtime and must see the combined
``script`` unchanged — hence the golden byte-identity test.

compute-seam S3 replaced the ``build_script`` / ``runtime_script`` string pair
with per-step ``bakeable`` / ``runtime`` flags, and moved the server launch out
of the scripts entirely onto ``RenderedProvision.launch``. The partitions below
are what Modal now composes, so these assertions still pin the same behaviour.
"""

import importlib.resources
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import RenderedProvision, combine_steps
from kinoforge.engines.diffusers import DiffusersEngine, _render_embed_lines

_GOLDEN = json.loads(Path("tests/engines/diffusers/_golden_provision.json").read_text())
_FLASHVSR = "examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml"
_WAN = "examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml"


def _render(path: str) -> RenderedProvision:
    return DiffusersEngine().render_provision(load_config(path).model_dump())


def _build_script(rp: RenderedProvision) -> str:
    """Return the steps Modal bakes into the image."""
    return combine_steps(tuple(s for s in rp.setup_steps if s.bakeable))


def _runtime_script(rp: RenderedProvision) -> str:
    """Return the steps Modal runs at container start."""
    return combine_steps(tuple(s for s in rp.setup_steps if s.runtime))


def test_script_is_byte_identical_to_golden():
    # Bug caught: a careless refactor reorders/duplicates lines -> the RunPod
    # boot script drifts from what shipped, silently changing provisioning.
    for path, golden in _GOLDEN.items():
        assert _render(path).script == golden, f"{path} script drifted"


def test_flashvsr_build_script_has_installs_not_runtime():
    # Bug caught: pip/BSA/weights leak out of build_script -> Modal can't bake
    # them, or runtime-only bits (server exec, sidecar, trap) get baked into the
    # image where they don't belong.
    b = _build_script(_render(_FLASHVSR))
    assert "pip install" in b
    assert "block_sparse_attn" in b  # BSA wheel curl+install (composed upscaler)
    assert "FlashVSR" in b or "flashvsr" in b  # weights fetch
    # runtime-only bits must NOT be in the bakeable build script:
    assert "sleep infinity" not in b
    assert "http.server 8001" not in b
    # the server EXEC (module -m invocation) is runtime, never baked. NB: the
    # embed DOES write .../wan_t2v_server.py into the image (build needs the
    # module tree for the weights fetch), so assert on the exec form, not the
    # bare substring.
    assert "python -m kinoforge.engines.diffusers.servers.wan_t2v_server" not in b
    assert "/tmp/bootstrap.log" not in b  # runtime log redirect


def test_flashvsr_runtime_script_has_server_not_installs():
    # Bug caught: the heavy installs stay in runtime_script -> Modal re-downloads
    # everything at container start, re-opening the ~15min preemption window that
    # killed the 2026-07-09 FlashVSR live run.
    r = _runtime_script(_render(_FLASHVSR))
    # The module tree is embedded at container start (S3 moved the server
    # COMMAND out to `launch`; the embedded `wan_t2v_server.py` file stays).
    assert "wan_t2v_server.py" in r
    assert "/tmp/bootstrap.log" in r  # runtime log redirect
    assert "sleep infinity" in r  # keep-alive trap preamble
    assert "block_sparse_attn" not in r  # BSA is baked, not runtime
    assert "torch==2.6.0" not in r  # the heavy pip line is baked, not runtime
    assert "pip install" not in r


def test_flashvsr_build_script_embeds_before_weights_fetch():
    # Bug caught: the composed FlashVSR weights fetch runs `python -m kinoforge
    # ...._fetch_weights`, which resolves ONLY against the embedded /tmp/kfsrv
    # tree + PYTHONPATH. If the embed is runtime-only, the build-time fetch hits
    # ModuleNotFoundError and the Modal image bake fails before any GPU spend.
    # The embed (+ PYTHONPATH export) must appear in build_script BEFORE the
    # fetch line.
    b = _build_script(_render(_FLASHVSR))
    pythonpath_pos = b.find("export PYTHONPATH=/tmp/kfsrv")
    # Match the fetch INVOCATION, not the embed's .../_fetch_weights.py write.
    fetch_pos = b.find("python -m kinoforge.upscalers.flashvsr._fetch_weights")
    assert pythonpath_pos != -1, "embed PYTHONPATH missing from build_script"
    assert fetch_pos != -1, "weights fetch missing from build_script"
    assert pythonpath_pos < fetch_pos, "PYTHONPATH must be set before the fetch"
    # The embed is ALSO in runtime (the server needs it) — appears in both.
    assert "export PYTHONPATH=/tmp/kfsrv" in _runtime_script(_render(_FLASHVSR))


class _FakeResource:
    """Stands in for an importlib.resources traversable file entry."""

    def __init__(self, name: str, is_file: bool = True) -> None:
        self.name = name
        self._is_file = is_file

    def is_file(self) -> bool:
        return self._is_file

    def read_bytes(self) -> bytes:
        return b"# " + self.name.encode()


def test_embed_lines_written_in_sorted_name_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug caught: an unsorted pkg_root.iterdir() makes the embed order follow
    # filesystem enumeration order, so the same commit renders a different
    # script on different machines (2026-07-11..13 CI reds: GitHub runners
    # enumerated the servers package alphabetically while the golden was
    # captured in this container's inode order). Dropping the sort brings
    # that per-machine golden flake straight back.
    scrambled = [
        _FakeResource("wan_t2v_server.py"),
        _FakeResource("_video_io.py"),
        _FakeResource("notes.txt"),  # non-.py: must never be embedded
        _FakeResource("_util_stats.py"),
        _FakeResource("__init__.py"),
    ]
    monkeypatch.setattr(
        importlib.resources,
        "files",
        lambda mod_name: SimpleNamespace(iterdir=lambda: list(scrambled)),
    )
    lines = _render_embed_lines(["kinoforge.engines.diffusers.servers"])
    targets = [line.rsplit("> ", 1)[1] for line in lines if line.startswith("echo '")]
    prefix = "/tmp/kfsrv/kinoforge/engines/diffusers/servers/"
    assert targets == [
        prefix + "__init__.py",
        prefix + "_util_stats.py",
        prefix + "_video_io.py",
        prefix + "wan_t2v_server.py",
    ]


def test_wan_cfg_without_upscaler_has_no_build_script():
    # Bug caught: a plain Wan t2v cfg (pip only) should still populate build_script
    # with its pip line but never with upscaler/server bits; runtime carries server.
    rp = _render(_WAN)
    assert "pip install" in _build_script(rp)
    # The server launch is in NEITHER script any more — it is the launch, which
    # is the whole point of S3: a provider composes it where its own convention
    # needs it rather than finding it at the end of a script.
    exec_line = "python -m kinoforge.engines.diffusers.servers.wan_t2v_server"
    assert exec_line not in _build_script(rp)
    assert exec_line not in _runtime_script(rp)
    assert rp.launch is not None
    assert " ".join(rp.launch.argv) == exec_line
    assert "pip install" not in _runtime_script(rp)
