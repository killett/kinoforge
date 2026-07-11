"""Modal App construction (the Option-A reuse hinge) + default deploy/stop/list.

``build_modal_app`` builds a ``modal.App`` whose serialized ``web_server`` runs the
same ``provision_script; exec run_cmd`` bundle that RunPod runs, so the existing
FastAPI server and ``render_provision`` machinery are reused verbatim. Per-run
config reaches the remote container through a ``modal.Secret`` (no image rebuild).
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any

#: Modal caps a single Secret value at 32768 bytes. The gzipped+base64 boot
#: payload (embedded server modules) exceeds that, so it is split across
#: ``KINOFORGE_PROVISION_B64_<i>`` keys, each safely under the cap.
_SECRET_CHUNK = 30000


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
    # Modal fast-boot: bakeable install steps (pip deps, BSA wheel, model
    # weights) baked into the image via Image.run_commands at BUILD time. None =>
    # nothing to bake (the whole provision runs at container start, as before).
    image_build_script: str | None = None


_VOLUME_NAME = "kinoforge-hf-cache"


def _boot_payload(req: ModalAppRequest) -> str:
    """Compose the container boot script: run provision, then exec the server."""
    exec_line = "exec " + shlex.join(req.run_cmd)
    return f"{req.provision_script}\n{exec_line}\n"


def _payload_secret_env(payload: str) -> dict[str, str]:
    """gzip+base64 the boot payload and split it across chunked secret keys.

    Modal's 32768-byte-per-value Secret cap forces chunking. Returns the env
    dict the container reassembles: an ``NCHUNKS`` count plus ``_B64_<i>`` parts.
    """
    blob = base64.b64encode(gzip.compress(payload.encode())).decode()
    chunks = [blob[i : i + _SECRET_CHUNK] for i in range(0, len(blob), _SECRET_CHUNK)]
    env = {"KINOFORGE_PROVISION_NCHUNKS": str(len(chunks))}
    for i, chunk in enumerate(chunks):
        env[f"KINOFORGE_PROVISION_B64_{i}"] = chunk
    return env


def build_modal_app(req: ModalAppRequest, modal_mod: Any) -> tuple[Any, Any]:  # noqa: ANN401
    """Build ``(app, server_fn)`` for ``req`` using ``modal_mod``.

    Args:
        req: The per-run app request.
        modal_mod: The ``modal`` SDK module (or a fake in tests).

    Returns:
        The constructed app and its decorated web-server function.
    """
    image = modal_mod.Image.from_registry(req.image, add_python=req.add_python)
    if req.image_build_script:
        # Bake the slow install steps (pip/BSA/weights) INTO the image so
        # container boot is seconds — no ~15min runtime provision for Modal to
        # preempt (the 2026-07-09 FlashVSR failure). run_commands streams to the
        # build log, so a bad wheel/weights fetch surfaces at BUILD time rather
        # than as a silent boot hang. Chainable, mirrors Modal's Image API.
        image = image.run_commands(req.image_build_script)
    app = modal_mod.App(name=f"kinoforge-{req.run_id}", image=image)
    volume = modal_mod.Volume.from_name(_VOLUME_NAME, create_if_missing=True)

    secret = modal_mod.Secret.from_dict(
        {**req.env, **_payload_secret_env(_boot_payload(req))}
    )

    @app.function(  # type: ignore[untyped-decorator]  # decorator from an Any-typed module
        gpu=req.gpu,
        serialized=True,  # cloudpickle this runtime-built fn (not import-by-ref)
        scaledown_window=req.scaledown_window_s,
        # Container-init window. With serialized=True Modal DROPS the
        # @web_server(startup_timeout=...) below and governs the init window by
        # the FUNCTION's startup_timeout (which itself defaults to `timeout`,
        # 300s). A ~63GB Wan 2.2 A14B download takes ~30min, so a 300s default
        # kills the container mid-download ("initializing for too long: 300
        # seconds"). Set both from req.startup_timeout_s (Milestone-1's 1.3B
        # downloaded under 300s, which is why this only surfaced at A14B).
        startup_timeout=req.startup_timeout_s,
        timeout=req.startup_timeout_s,
        volumes={req.volume_mount: volume},
        secrets=[secret],
    )
    @modal_mod.web_server(8000, startup_timeout=req.startup_timeout_s)  # type: ignore[untyped-decorator]
    def server() -> None:
        # Runs INSIDE the Modal container at startup. Reassemble the chunked
        # gzip+base64 boot script, write it, and launch (non-blocking) so it
        # binds 0.0.0.0:8000.
        n = int(os.environ["KINOFORGE_PROVISION_NCHUNKS"])
        blob = "".join(os.environ[f"KINOFORGE_PROVISION_B64_{i}"] for i in range(n))
        script = gzip.decompress(base64.b64decode(blob)).decode()
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
