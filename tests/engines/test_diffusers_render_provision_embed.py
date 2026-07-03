"""Lockdown tests for DiffusersEngine.render_provision's ``embed_modules`` payload.

Live smoke 2026-06-19 Leg 1 burned $0.27 because the bootstrap exec'd
``python -m kinoforge.engines.diffusers.servers.wan_t2v_server`` against
a stock RunPod pytorch image that had no kinoforge package — instant
ModuleNotFoundError, server never bound port 8000, /health stayed 404,
and wait_for_ready looped past boot_timeout until the test subprocess
timeout SIGKILL'd it.

Fix: cfg declares ``engine.diffusers.embed_modules`` (list of dotted
package names); render_provision base64-encodes each package's .py
files into the bootstrap, decodes them into /tmp/kfsrv/, ensures
__init__.py exists at every namespace level, and prepends
``/tmp/kfsrv`` to PYTHONPATH. The exec line then resolves the module
import without any pod-side pip install of kinoforge.
"""

from __future__ import annotations

import base64
import gzip
from typing import Any

from kinoforge.engines.diffusers import DiffusersEngine


def _decoded_blob_for(script: str, target: str) -> bytes | None:
    """Decode the gzip+base64 payload written to ``target``, or None.

    Provision emits one line per embedded file in the form:
    ``echo '<b64>' | python3 -c "...gzip.decompress(base64.b64decode(...))" > <target>``

    Args:
        script: Full provision script text (multi-line string).
        target: Absolute target path to match (e.g. ``/tmp/kfsrv/...py``).

    Returns:
        Decompressed file bytes, or ``None`` if no matching line is found.
    """
    for line in script.splitlines():
        if target in line and "base64" in line and line.startswith("echo '"):
            blob = line.split("'", 2)[1]
            return gzip.decompress(base64.b64decode(blob))
    return None


def _make_engine() -> DiffusersEngine:
    return DiffusersEngine(probe_profile=None)  # type: ignore[arg-type]


def _cfg_with_embed() -> dict[str, Any]:
    return {
        "engine": {
            "diffusers": {
                "base_url": "http://localhost:8000",
                "pip": [],
                "server_cmd": [
                    "python",
                    "-m",
                    "kinoforge.engines.diffusers.servers.wan_t2v_server",
                ],
                "embed_modules": ["kinoforge.engines.diffusers.servers"],
            }
        }
    }


def test_embed_modules_default_off_no_kfsrv_lines() -> None:
    # Regression: when cfg has no embed_modules, the script must NOT
    # gain spurious /tmp/kfsrv lines — preserves the existing minimal-
    # script contract for the pre-Phase-1 diffusers cfgs.
    cfg = {
        "engine": {
            "diffusers": {
                "base_url": "http://localhost:8000",
                "pip": [],
                "server_cmd": ["python", "-m", "diffusers_server"],
            }
        }
    }
    rp = _make_engine().render_provision(cfg)
    assert "/tmp/kfsrv" not in rp.script
    assert "PYTHONPATH" not in rp.script


def test_embed_writes_wan_t2v_server_source_to_kfsrv() -> None:
    # Bug caught: render_provision claims to embed the module but the
    # base64 payload is empty or points at the wrong file → on the pod,
    # /tmp/kfsrv/kinoforge/engines/diffusers/servers/wan_t2v_server.py
    # exists but its contents are stale or empty → python -m … starts
    # but fails on missing endpoints / wrong shape.
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv_mod

    rp = _make_engine().render_provision(_cfg_with_embed())
    script = rp.script
    target = "/tmp/kfsrv/kinoforge/engines/diffusers/servers/wan_t2v_server.py"
    assert target in script, "expected embedded write to wan_t2v_server.py"

    # Pull the gzip+base64 blob written to that target and decode it; must
    # contain a fingerprint string unique to our server module.
    fingerprint = b"FastAPI inference server for Wan 2.2 T2V-A14B"
    decoded = _decoded_blob_for(script, target)
    assert decoded is not None, f"no embed write line found for {target}"
    assert fingerprint in decoded, (
        f"embedded payload for {target} does not contain {fingerprint!r}"
    )
    # Sanity: the source on disk has the same fingerprint.
    with open(srv_mod.__file__, "rb") as f:
        assert fingerprint in f.read()


def test_embed_writes_video_io_source_to_kfsrv() -> None:
    # Bug caught: embed only writes wan_t2v_server.py and forgets
    # _video_io.py → server.py import fails at module load with
    # ModuleNotFoundError on kinoforge.engines.diffusers.servers._video_io.
    rp = _make_engine().render_provision(_cfg_with_embed())
    target = "/tmp/kfsrv/kinoforge/engines/diffusers/servers/_video_io.py"
    assert target in rp.script
    fingerprint = b"MP4 encoder helper for diffusers-engine servers"
    decoded = _decoded_blob_for(rp.script, target)
    assert decoded is not None, f"no embed write line found for {target}"
    assert fingerprint in decoded, (
        f"embedded payload for {target} does not contain {fingerprint!r}"
    )


def test_embed_touches_init_py_at_every_namespace_level() -> None:
    # Bug caught: skipping the touch chain → `python -m
    # kinoforge.engines.diffusers.servers.wan_t2v_server` raises
    # `No module named kinoforge` because /tmp/kfsrv/kinoforge has no
    # __init__.py.
    rp = _make_engine().render_provision(_cfg_with_embed())
    script = rp.script
    for level in (
        "/tmp/kfsrv/kinoforge/__init__.py",
        "/tmp/kfsrv/kinoforge/engines/__init__.py",
        "/tmp/kfsrv/kinoforge/engines/diffusers/__init__.py",
        "/tmp/kfsrv/kinoforge/engines/diffusers/servers/__init__.py",
    ):
        assert level in script, f"missing touch/write for {level}"


def test_embed_prepends_pythonpath_before_exec() -> None:
    # Bug caught: PYTHONPATH set after `exec` — exec replaces the shell
    # so any later lines never run. Or PYTHONPATH missing entirely
    # which makes the embedded modules invisible.
    rp = _make_engine().render_provision(_cfg_with_embed())
    script = rp.script
    assert "export PYTHONPATH=/tmp/kfsrv" in script
    pp_idx = script.index("export PYTHONPATH=/tmp/kfsrv")
    # Post-Task-8-attempt-4 fix: main server is no longer `exec`'d so
    # bash retains PID 1 and its EXIT trap can fire on crash. Match the
    # non-exec form.
    exec_idx = script.rindex("python -m")
    assert pp_idx < exec_idx, "PYTHONPATH must be set before exec"


def test_embed_does_not_break_selfterm_or_pip_order() -> None:
    # Regression: selfterm watchdog still launches first; pip install
    # still runs before exec; embed writes sit somewhere between
    # selfterm (so embed crashes don't outlive the dead-man) and exec.
    cfg = _cfg_with_embed()
    cfg["engine"]["diffusers"]["pip"] = ["fastapi>=0.115"]
    rp = _make_engine().render_provision(cfg)
    script = rp.script
    selfterm_idx = script.index("nohup python3 /tmp/selfterm.py")
    embed_idx = script.index("/tmp/kfsrv/kinoforge")
    # Post-Task-8-attempt-2 fix: pip deps are shlex-quoted so bash does not
    # parse `>=` as a stdout redirect. Match the quoted form.
    # Post-Task-8-attempt-15: pip line includes --extra-index-url
    # before deps. Match the bare prefix instead.
    pip_idx = script.index("pip install -q")
    # Post-Task-8-attempt-4 fix: main server is no longer `exec`'d so
    # bash retains PID 1 and its EXIT trap can fire on crash. Match the
    # non-exec form.
    exec_idx = script.rindex("python -m")
    assert selfterm_idx < embed_idx
    assert selfterm_idx < pip_idx
    assert embed_idx < exec_idx
    assert pip_idx < exec_idx
