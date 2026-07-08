"""src/sitecustomize.py applies the vast shim at interpreter startup.

This is the seam that reaches SkyPilot's API server SUBPROCESS (``python -m
sky.server.server``), where vast provisioning actually runs — the in-process
``vast_compat`` shim never reaches it, so without this a vast launch dies with
``VastAI has no attribute client`` (root-caused live 2026-07-07).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("vastai_sdk")  # only runs in the live-skypilot env

_SRC = Path(__file__).resolve().parents[2] / "src"


def test_sitecustomize_patches_vastai_in_a_fresh_subprocess() -> None:
    # Bug caught: the shim is applied only in-process, so sky's API server
    # subprocess (which never imports kinoforge) still AttributeErrors on
    # VastAI.client and every vast launch fails after resource selection.
    # A fresh interpreter with src on PYTHONPATH must resolve .client WITHOUT
    # anyone calling apply_vast_sdk_compat() — proving the startup hook fires.
    probe = (
        "from vastai_sdk import VastAI;"
        "assert hasattr(VastAI, 'client'), 'startup hook did not patch VastAI';"
        "assert VastAI(api_key='k').client.api_key == 'k';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env={"PYTHONPATH": str(_SRC), "PATH": _env_path()},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def _env_path() -> str:
    import os

    return os.environ.get("PATH", "")
