"""Capture the FlashVSR mp4-writer stack versions on a CPU-only Modal build ($0).

Why this exists (PROGRESS.md **U12** / matrix follow-up **F14**): every Tier 2a
FlashVSR upscale on Modal died server-side at

    iio.imwrite(str(out), video, fps=fps, plugin="pyav", codec="libx264")
    # src/kinoforge/upscalers/flashvsr/_runtime.py:416
    -> UpscaleFailed: ... failed on server: Cannot change width after codec is open.

3 for 3 (T2-01, T2-03, T2-05b), across two cfgs and both lifecycle routes. The
same code was live-green on 2026-07-10 (§24) and 2026-07-12 (§27). The leading
hypothesis was an unpinned newer ``av`` in a freshly-baked image — but the pods
were destroyed before any version was read off them, so it stayed a hypothesis.

The writer needs no GPU, so both the version capture **and the failing call
itself** can be reproduced on a CPU-only Modal image build for $0. That is what
this does. Shape follows ``tools/build_bsa_wheel_modal.py`` (the repo's existing
CPU-only Modal build driver).

Run:
    pixi run -e live-modal modal run tools/diagnose_flashvsr_writer_modal.py

The image layers mirror the real bake in order, so pip's resolver sees the same
constraint set in the same sequence:

  1. ``python:3.13-slim`` — the base the cfg pins (Modal's serialized
     web-server fn forces image-Python == controller-Python).
  2. apt: curl / git / build-essential / cmake / pkg-config — what
     ``providers/modal/_app.py`` installs before running the bake script.
  3. the cfg's ``engine.diffusers.pip`` list from
     ``examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml``, verbatim.
  4. the provision script's runtime-deps line from
     ``upscalers/flashvsr/_engine.py`` — the line that installs
     ``imageio[ffmpeg,pyav]>=2.34`` and a **completely unpinned** ``av``.

Deliberately NOT reproduced: the BSA wheel and the FlashVSR git install. Both
go in under ``pip install --no-deps``, so neither can constrain the resolution
of ``av`` / ``imageio``, and both are GPU-/wheel-specific.

Costs: CPU image build only. No GPU is requested anywhere in this module.
"""

from __future__ import annotations

import modal

# Load .env LOCALLY so the modal client authenticates (pixi does not auto-source
# .env). Guarded exactly as build_bsa_wheel_modal.py is: Modal re-imports this
# module inside the container, where kinoforge is not installed.
try:
    from kinoforge.core.dotenv_loader import load_env_file

    load_env_file()
except ModuleNotFoundError:
    pass

# --- layer 3: examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml -------
# `engine.diffusers.pip`, verbatim and in order. The torch trio comes from the
# default PyPI index rather than the cu124 index the pod uses: same version,
# and neither torch nor its deps constrain `av` or `imageio`.
_CFG_PIP = (
    "torch==2.6.0",
    "torchvision==0.21.0",
    "torchaudio==2.6.0",
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "imageio[ffmpeg]>=2.34",
    "modelscope",
    "setuptools<81",
    "wheel",
)

# --- layer 4: upscalers/flashvsr/_engine.py runtime-deps line ----------------
# Verbatim. Note the last two entries: the pyav extra, and a bare unpinned `av`.
_PROVISION_PIP = (
    "modelscope",
    "safetensors==0.5.3",
    "transformers>=4.48,<5",
    "accelerate==1.8.1",
    "peft==0.17.0",
    "einops==0.8.1",
    "ftfy==6.3.1",
    "sentencepiece==0.2.0",
    "imageio[ffmpeg,pyav]>=2.34",
    "av",
)

_image = (
    modal.Image.from_registry("python:3.13-slim")
    .apt_install("curl", "git", "build-essential", "cmake", "pkg-config")
    .pip_install(*_CFG_PIP)
    .pip_install(*_PROVISION_PIP)
)

app = modal.App("kinoforge-flashvsr-writer-diagnostic", image=_image)


@app.function(timeout=900)  # type: ignore[untyped-decorator]  # modal decorators are Any-typed
def probe() -> dict[str, object]:
    """Report the writer stack's versions and whether the real call fails.

    Runs inside the baked CPU image. Prints a human-readable report and returns
    the same data so the local entrypoint can render it.

    Returns:
        A mapping with a ``versions`` block (imageio, imageio-ffmpeg, av,
        numpy, the bundled ffmpeg binary, and av's linked libav* versions) and
        a ``writes`` block: one entry per attempted ``iio.imwrite`` with either
        ``ok`` and the byte count, or the exception type and message.
    """
    import subprocess
    import sys
    import tempfile
    from importlib.metadata import version as _dist_version
    from pathlib import Path

    import av  # type: ignore[import-not-found]  # baked into the CPU image only
    import imageio.v3 as iio
    import numpy as np

    def _dist(name: str) -> str:
        try:
            return _dist_version(name)
        except Exception as exc:  # noqa: BLE001 — reporting tool
            return f"<absent: {exc}>"

    ffmpeg_exe_version = "<unknown>"
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]  # no py.typed

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        proc = subprocess.run(  # noqa: S603 — path from imageio_ffmpeg, no shell
            [exe, "-version"], capture_output=True, text=True, timeout=30
        )
        ffmpeg_exe_version = (
            proc.stdout.splitlines()[0] if proc.stdout else "<no output>"
        )
    except Exception as exc:  # noqa: BLE001 — reporting tool
        ffmpeg_exe_version = f"<absent: {exc}>"

    versions = {
        "python": sys.version.split()[0],
        "imageio": _dist("imageio"),
        "imageio-ffmpeg": _dist("imageio-ffmpeg"),
        "av": _dist("av"),
        "av.__version__": getattr(av, "__version__", "<none>"),
        "av.library_versions": {
            k: ".".join(str(n) for n in v) for k, v in dict(av.library_versions).items()
        },
        "numpy": _dist("numpy"),
        "imageio_ffmpeg bundled ffmpeg": ffmpeg_exe_version,
    }

    # The exact call from _runtime.py:416, on the shapes FlashVSR produces.
    # (T, H, W, C) uint8, contiguous — no GPU, no model, just the writer.
    writes: dict[str, object] = {}
    tmp = Path(tempfile.mkdtemp())
    for label, (frames, side) in {
        "1920sq_8f (FlashVSR 4x output shape)": (8, 1920),
        "480sq_8f (source shape)": (8, 480),
        "255sq_8f (odd side)": (8, 255),
    }.items():
        video = np.zeros((frames, side, side, 3), dtype=np.uint8)
        video[:, :: max(side // 8, 1), :, 0] = 255  # non-uniform content
        dest = tmp / f"{label.split(' ')[0]}.mp4"
        try:
            iio.imwrite(str(dest), video, fps=16.0, plugin="pyav", codec="libx264")
        except Exception as exc:  # noqa: BLE001 — this IS the thing under test
            writes[label] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            writes[label] = {"ok": True, "bytes": dest.stat().st_size}

    report: dict[str, object] = {"versions": versions, "writes": writes}
    print("=== FlashVSR writer stack, baked CPU image ===")
    for k, v in versions.items():
        print(f"  {k}: {v}")
    print("=== iio.imwrite(plugin='pyav', codec='libx264') ===")
    for k, v in writes.items():
        print(f"  {k}: {v}")
    return report


_WRITE_SNIPPET = """
import sys, tempfile, pathlib
import numpy as np, imageio.v3 as iio, av
from importlib.metadata import version
d = pathlib.Path(tempfile.mkdtemp()) / "probe.mp4"
vid = np.zeros((8, 480, 480, 3), dtype=np.uint8)
vid[:, ::60, :, 0] = 255
try:
    iio.imwrite(str(d), vid, fps=16.0, plugin="pyav", codec="libx264")
except Exception as exc:
    print(f"RESULT av={version('av')} imageio={version('imageio')} "
          f"FAIL {type(exc).__name__}: {exc}")
else:
    print(f"RESULT av={version('av')} imageio={version('imageio')} "
          f"OK bytes={d.stat().st_size}")
"""


@app.function(timeout=1800)  # type: ignore[untyped-decorator]  # modal decorators are Any-typed
def bisect_av(pins: list[str]) -> list[str]:
    """Re-run the write with `av` pinned to each candidate, imageio held fixed.

    Reuses the already-baked image and downgrades only ``av`` between attempts,
    so ``av`` is the single variable. Each attempt runs the write in a fresh
    subprocess because the pyav extension module cannot be reloaded in-process
    after a version swap.

    Args:
        pins: pip requirement strings for ``av``, tried in order (e.g.
            ``["av==15.1.0", "av==14.4.0"]``).

    Returns:
        One ``RESULT …`` line per pin, in the order tried.
    """
    import subprocess
    import sys

    lines: list[str] = []
    for pin in pins:
        inst = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-m", "pip", "install", "--quiet", pin],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if inst.returncode != 0:
            lines.append(f"RESULT {pin} INSTALL-FAILED {inst.stderr.strip()[-300:]}")
            continue
        run = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", _WRITE_SNIPPET],
            capture_output=True,
            text=True,
            timeout=600,
        )
        out = run.stdout.strip() or run.stderr.strip()[-300:]
        lines.append(f"{pin} -> {out}")
        print(lines[-1])
    return lines


@app.local_entrypoint()  # type: ignore[untyped-decorator]  # modal decorator is Any-typed
def main() -> None:
    """Build the CPU image, print the probe report, then bisect the `av` pin."""
    report = probe.remote()
    print(report)
    print("=== av bisect (imageio held at the baked version) ===")
    # Answer, 2026-09-06, imageio 2.37.4 / python 3.13.14 throughout:
    #   av 18.1.0 -> RuntimeError: Cannot change width after codec is open.
    #   av 17.1.0 -> OK      av 16.1.0 -> OK
    #   av 15.1.0 -> OK      av 13.1.0 -> OK
    # The break is exactly at av 18, so `av<18` is the pin. (av 14.x / 12.x
    # publish no cp313 wheel and fail to build from source — not evidence.)
    for line in bisect_av.remote(["av<18", "av<17", "av==15.1.0", "av==13.1.0"]):
        print(line)
