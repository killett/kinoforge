"""Modal App construction (the Option-A reuse hinge) + default deploy/stop/list.

``build_modal_app`` builds a ``modal.App`` whose serialized ``web_server`` runs the
same ``provision_script; exec run_cmd`` bundle that RunPod runs, so the existing
FastAPI server and ``render_provision`` machinery are reused verbatim. Per-run
config reaches the remote container through a ``modal.Secret`` (no image rebuild).
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModalAppRequest:
    """Everything needed to build one Modal generation App."""

    run_id: str
    image: str
    gpu: str
    provision_script: str
    run_cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)
    volume_mount: str = "/cache/hf"
    scaledown_window_s: int = 300
    startup_timeout_s: int = 1800
    # Only for base images that ship NO Python. Our images (runpod/pytorch,
    # python:X-slim) already have Python, so forcing add_python makes Modal's
    # `ln -s .../python3 /usr/local/bin/python` fail ("File exists"). Default
    # None => omit, use the image's own Python.
    add_python: str | None = None


_VOLUME_NAME = "kinoforge-hf-cache"


def _boot_payload(req: ModalAppRequest) -> str:
    """Compose the container boot script: run provision, then exec the server."""
    exec_line = "exec " + shlex.join(req.run_cmd)
    return f"{req.provision_script}\n{exec_line}\n"


def build_modal_app(req: ModalAppRequest, modal_mod: Any) -> tuple[Any, Any]:  # noqa: ANN401
    """Build ``(app, server_fn)`` for ``req`` using ``modal_mod``.

    Args:
        req: The per-run app request.
        modal_mod: The ``modal`` SDK module (or a fake in tests).

    Returns:
        The constructed app and its decorated web-server function.
    """
    image = modal_mod.Image.from_registry(req.image, add_python=req.add_python)
    app = modal_mod.App(name=f"kinoforge-{req.run_id}", image=image)
    volume = modal_mod.Volume.from_name(_VOLUME_NAME, create_if_missing=True)

    payload_b64 = base64.b64encode(_boot_payload(req).encode()).decode()
    secret = modal_mod.Secret.from_dict(
        {**req.env, "KINOFORGE_PROVISION_B64": payload_b64}
    )

    @app.function(  # type: ignore[untyped-decorator]  # decorator from an Any-typed module
        gpu=req.gpu,
        serialized=True,  # cloudpickle this runtime-built fn (not import-by-ref)
        scaledown_window=req.scaledown_window_s,
        volumes={req.volume_mount: volume},
        secrets=[secret],
    )
    @modal_mod.web_server(8000, startup_timeout=req.startup_timeout_s)  # type: ignore[untyped-decorator]
    def server() -> None:
        # Runs INSIDE the Modal container at startup. Decode the boot script,
        # write it, and launch (non-blocking) so it binds 0.0.0.0:8000.
        script = base64.b64decode(os.environ["KINOFORGE_PROVISION_B64"]).decode()
        with open("/tmp/kinoforge_boot.sh", "w") as fh:  # noqa: S108
            fh.write(script)
        subprocess.Popen(["bash", "/tmp/kinoforge_boot.sh"])  # noqa: S603,S607,S108

    return app, server


# --- default (live) seams -------------------------------------------------


def default_deploy(app: Any, server_fn: Any) -> str:  # noqa: ANN401
    """Deploy ``app`` and return the public web URL (survives process exit)."""
    import modal

    with modal.enable_output():
        app.deploy()
    url: str = server_fn.get_web_url()
    return url


def default_stop(app_name: str) -> None:
    """Stop a deployed app via the CLI (bounded by subprocess timeout)."""
    subprocess.run(  # noqa: S603 — fixed argv, app_name from our own run_id
        ["modal", "app", "stop", app_name, "--yes"],  # noqa: S607 — modal via PATH
        check=True,
        timeout=120,
        env=os.environ.copy(),
    )


def default_list() -> list[dict[str, Any]]:
    """Return deployed-app records via ``modal app list --json``."""
    out = subprocess.run(  # noqa: S603 — fixed argv, no untrusted input
        ["modal", "app", "list", "--json"],  # noqa: S607 — modal via PATH
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env=os.environ.copy(),
    )
    records: list[dict[str, Any]] = json.loads(out.stdout or "[]")
    return records
