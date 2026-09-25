"""Behavior: the pod's models dir is provider-supplied, not a baked literal.

U52. Four ``/workspace/models/...`` paths survived the pod-path seam work as
runtime defaults in ``wan_t2v_server.py``:

* ``_SPANDREL_WEIGHTS_DIR_DEFAULT`` and ``_FLASHVSR_WEIGHTS_DIR_DEFAULT``
  (named-constant ``os.environ.get`` fallbacks, allowlisted by name in
  ``tests/test_pod_path_audit.py`` pending this item), and
* the SeedVR2 and RIFE weight dirs, hardcoded ``Path("/workspace/models/...")``
  with no override at all — so they never even reached the audit's
  ``os.environ.get`` pattern.

``/workspace`` is RunPod's volume mount. Modal mounts its Volume elsewhere, and
SkyPilot and Local attach none, so **on Modal these weights land on ephemeral
container disk rather than the mounted Volume** — re-fetched every boot, and
lost with the container.

What made a server-only fix wrong, and why this touches six files: the
provision scripts WRITE the same literals. ``upscalers/spandrel/_engine.py``,
``upscalers/flashvsr/_engine.py`` and ``interpolators/rife/_engine.py`` all
emit shell that populates those exact directories. Moving only the read side
would desync the server from the side that fills the directory — a pod that
boots clean and 404s on first use. Reader and writers move together, the same
shape as the ``HF_HOME`` / ``ARTIFACT_DIR`` / ``LORAS_DIR`` fix that shipped
with the seam.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kinoforge.core.pod_paths import (
    MODELS_DIR_VAR,
    SCRATCH_MODELS_DIR,
    pod_path_env,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "kinoforge"

_WRITERS = [
    "upscalers/spandrel/_engine.py",
    "upscalers/flashvsr/_engine.py",
    "interpolators/rife/_engine.py",
]


def _without_fallbacks(text: str) -> str:
    """Blank out every documented default so only BARE literals remain.

    A literal is legitimate in exactly two shapes — a shell ``${VAR:-default}``
    expansion and an ``os.environ.get(VAR, default)`` fallback — because both
    keep the code working on a host that does not export the var, which is
    what the sibling ``ARTIFACT_DIR`` / ``LORAS_DIR`` seams already do. What
    must not survive is a literal with no override in front of it: that is the
    shape of the two SeedVR2/RIFE paths U52 was filed for, which no operator
    could redirect.

    Args:
        text: Source text to scan.

    Returns:
        The text with those two shapes replaced by a marker.
    """
    text = re.sub(r"\$\{KINOFORGE_MODELS_DIR:-[^}]+\}", "<fallback>", text)
    return re.sub(
        r"os\.environ\.get\(\s*MODELS_DIR_VAR\s*,\s*[\"'][^\"']+[\"']\s*\)",
        "<fallback>",
        text,
    )


def test_a_mounted_volume_supplies_the_models_dir() -> None:
    """The dir is derived from the provider's mount, like its siblings.

    Bug caught: omitting ``KINOFORGE_MODELS_DIR`` from ``pod_path_env``. The
    server would fall back to scratch on every provider, so weights would
    never reach the volume even on RunPod — worse than the defect.
    """
    env = pod_path_env("/cache/hf")

    assert env[MODELS_DIR_VAR] == "/cache/hf/models"


def test_a_hostless_provider_falls_back_to_pod_local_scratch() -> None:
    """SkyPilot and Local attach no volume and must still have somewhere to write.

    Bug caught: deriving the models dir from an empty mount and producing
    ``/models`` — an unwritable root path that fails at first fetch.
    """
    env = pod_path_env(None)

    assert env[MODELS_DIR_VAR] == SCRATCH_MODELS_DIR
    assert not SCRATCH_MODELS_DIR.startswith("/models")


def test_no_server_module_bakes_the_runpod_models_literal() -> None:
    """The reader side: all four survivors resolve from the env var.

    Bug caught: leaving any of the four. The two hardcoded ``Path(...)`` ones
    carried no override at all, so on Modal they wrote to container disk with
    nothing an operator could set to redirect them.
    """
    servers = _SRC / "engines" / "diffusers" / "servers"
    offenders = [
        f"{path.relative_to(_SRC)}:{i}: {line.strip()}"
        for path in sorted(servers.glob("*.py"))
        for i, line in enumerate(_without_fallbacks(path.read_text()).splitlines(), 1)
        if "/workspace/models" in line and not line.lstrip().startswith("#")
    ]

    assert not offenders, "server modules still bake RunPod's mount:\n  " + "\n  ".join(
        offenders
    )


@pytest.mark.parametrize("rel", _WRITERS, ids=lambda p: p.split("/")[-2])
def test_no_provision_writer_bakes_the_runpod_models_literal(rel: str) -> None:
    """The writer side, which is why a server-only fix was wrong.

    Bug caught: fixing the server's read and leaving the provision script's
    write. The server would look in the volume while the script filled
    container disk — a pod that boots clean, reports healthy, and fails at
    first use, which is strictly harder to diagnose than the original defect.
    """
    text = _without_fallbacks((_SRC / rel).read_text())
    offenders = [
        f"{rel}:{i}: {line.strip()}"
        for i, line in enumerate(text.splitlines(), start=1)
        if "/workspace/models" in line and not line.lstrip().startswith("#")
    ]

    assert not offenders, (
        "provision writer still bakes RunPod's mount:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("rel", _WRITERS, ids=lambda p: p.split("/")[-2])
def test_each_writer_carries_a_shell_fallback(rel: str) -> None:
    """A provision script must not expand to an empty path.

    Bug caught: emitting a bare ``${KINOFORGE_MODELS_DIR}``. On any host where
    the var is unset the shell expands it to nothing, so ``mkdir -p /train_log``
    and ``cp -f ... /`` run against the filesystem ROOT — a destructive
    difference from the literal it replaced, not a neutral one.
    """
    text = (_SRC / rel).read_text()

    # `{{` / `}}` because an f-string doubles the braces to emit a literal
    # `${...}` — both spellings render the same shell.
    assert re.search(r"\$\{\{?KINOFORGE_MODELS_DIR:-[^}]+\}\}?", text), (
        f"{rel} references the models dir without a `:-default` shell fallback"
    )
