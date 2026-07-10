"""Build the Block-Sparse-Attention **cp313** wheel on Modal + upload to GH.

Why Modal, not RunPod (the sibling ``build_bsa_wheel.py``): on 2026-07-09 two
RunPod builder pods were **reclaimed by the host ~6-8 min into the ~20 min
build** (``POD_NOT_FOUND`` mid-run, util telemetry never populated) — RunPod
could not hold a pod long enough to finish. Modal's image builder is stable and
streams its logs, so the compile is both reliable and observable. This is the
same reliability pivot the whole Modal-provider milestone is about.

No GPU needed: BSA (a flash-attention fork) compiles its CUDA kernels with
``nvcc`` + ``TORCH_CUDA_ARCH_LIST`` at build time and does NOT require a physical
GPU present (the standard flash-attn CI wheel-build pattern). So the compile runs
in a **CPU** Modal image build on an ``nvidia/cuda:12.4.1-devel`` base (nvcc
12.4 matches the cu124 torch wheels), under an ``add_python="3.13"`` interpreter
so the emitted wheel ABI tag is ``cp313-cp313`` — the tag Modal's serialized
web-server fn needs (its image is py3.13; the older cp311 wheel pip-rejects).

Run:
    pixi run -e live-modal modal run tools/build_bsa_wheel_modal.py

The image build (the long compile) streams to your terminal; the function then
uploads ``/wheels/*.whl`` to the ``bsa-cu124-torch2.6-cp313-v1`` release on
``killett/kinoforge-artifacts`` (the release must already exist).
"""

from __future__ import annotations

import os

import modal

# Load .env LOCALLY so os.environ carries GH_TOKEN for the Secret below (pixi does
# not auto-source .env). Guarded: Modal re-imports THIS module inside the container
# to locate the function, where kinoforge is not installed — the attached Secret
# already carries the token there, so skipping the load in-container is correct
# (an unguarded import raises ModuleNotFoundError and kills the function).
try:
    from kinoforge.core.dotenv_loader import load_env_file

    load_env_file()
except ModuleNotFoundError:
    pass

_GH_OWNER = "killett"
_GH_REPO = "kinoforge-artifacts"
_GH_TAG = "bsa-cu124-torch2.6-cp313-v1"
_BSA_COMMIT = "3453bbb1"
_TORCH_INDEX = "https://download.pytorch.org/whl/cu124"
_ARCH_LIST = "8.0;8.6;8.9;9.0"

# CPU image build: nvcc (from cuda-devel) compiles the CUDA kernels offline for
# the arch list; py3.13 (add_python) drives pip so the wheel tag is cp313.
_image = (
    modal.Image.from_registry("nvidia/cuda:12.4.1-devel-ubuntu22.04", add_python="3.13")
    .apt_install("git")
    .pip_install(
        "torch==2.6.0",
        "torchvision==0.21.0",
        "torchaudio==2.6.0",
        extra_index_url=_TORCH_INDEX,
    )
    # Build backend deps for `pip wheel --no-build-isolation`.
    .pip_install("packaging", "ninja", "wheel", "setuptools")
    # add_python's standalone CPython bakes `clang` into its sysconfig, so
    # torch's cpp_extension links the final .so with clang++ — absent from the
    # cuda-devel image (only g++). The 70-min nvcc compile succeeds, then the
    # link dies with "clang++ ... No such file or directory". Install clang so
    # the linker exists (it links the g++/nvcc objects against libstdc++ fine).
    # Placed AFTER the torch pip layer so that cached layer is reused on rebuild.
    .apt_install("clang")
    .run_commands(
        "git clone --depth 100 "
        "https://github.com/mit-han-lab/Block-Sparse-Attention.git /tmp/bsa",
        f"cd /tmp/bsa && git checkout {_BSA_COMMIT}",
        "mkdir -p /wheels",
        # The wheel links against the build-time torch (cu124/2.6). MAX_JOBS caps
        # parallel nvcc so the builder does not OOM.
        f"cd /tmp/bsa && TORCH_CUDA_ARCH_LIST='{_ARCH_LIST}' MAX_JOBS=4 "
        "python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .",
    )
)

app = modal.App("bsa-wheel-cp313-builder", image=_image)


@app.function(  # type: ignore[untyped-decorator]  # modal decorators are Any-typed
    timeout=1800,
    secrets=[modal.Secret.from_dict({"GH_TOKEN": os.environ.get("GH_TOKEN", "")})],
)
def upload_wheel() -> str:
    """Inside the built image: find the cp313 wheel and upload it to the release.

    Returns:
        The uploaded wheel filename.

    Raises:
        RuntimeError: No wheel built, or the wheel is not tagged ``cp313``.
    """
    import glob
    import json
    import urllib.request

    wheels = glob.glob("/wheels/*.whl")
    if not wheels:
        raise RuntimeError("no wheel in /wheels — the image build did not compile it")
    wheel = wheels[0]
    name = os.path.basename(wheel)
    if "cp313-cp313" not in name:
        raise RuntimeError(f"built wheel is not cp313: {name}")

    tok = os.environ["GH_TOKEN"]
    api = f"https://api.github.com/repos/{_GH_OWNER}/{_GH_REPO}"
    rel_req = urllib.request.Request(  # noqa: S310 — https, hardcoded host
        f"{api}/releases/tags/{_GH_TAG}",
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(rel_req, timeout=30) as resp:  # noqa: S310
        release_id = int(json.load(resp)["id"])

    with open(wheel, "rb") as fh:
        blob = fh.read()
    up = urllib.request.Request(  # noqa: S310 — https, hardcoded uploads host
        f"https://uploads.github.com/repos/{_GH_OWNER}/{_GH_REPO}"
        f"/releases/{release_id}/assets?name={name}",
        data=blob,
        method="POST",
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/octet-stream",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(up, timeout=300) as resp:  # noqa: S310
        state = json.load(resp).get("state")
    print(f"uploaded {name} ({len(blob)} bytes) state={state}")
    return name


@app.local_entrypoint()  # type: ignore[untyped-decorator]  # modal decorator is Any-typed
def main() -> None:
    """Trigger the image build (compile) + upload; print the wheel name."""
    print("wheel:", upload_wheel.remote())
