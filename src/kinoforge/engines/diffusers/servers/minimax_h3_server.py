"""FastAPI inference server for MiniMax-H3 text-to-video-AND-audio (t2va).

Runs on the GPU pod. Exposes the same DiffusersBackend HTTP contract the Wan
server does:

  GET  /health                  -> {"ready": bool, "model": str, "torch": {...}}
  GET  /util                    -> the five UtilSnapshot fields
  POST /generate                -> {"job_id": str}
  GET  /status/{job_id}         -> {"status": ..., ...}
  GET  /artifacts/{filename}    -> MP4 bytes (video AND audio)

This is a SIBLING of ``wan_t2v_server.py``, not an extension of it. That file is
2600+ lines carrying a LoRA registry, an LRU co-residency manager, an upscale
path and an interpolate path, all shaped around Wan; threading a second
architecture through it would put the working Wan path at risk for no benefit.
What is shared is shared as modules: ``_util_stats`` for ``/util`` and
``_av_io`` for the mux.

MiniMax-H3 facts this server is built around, all read from the diffusers
v0.40.0 source rather than inferred:

* **It is modular-only.** ``MiniMaxH3Pipeline`` — the ``_class_name`` in the
  repo's nested ``FL2VA/model_index.json`` — is not exported by diffusers at
  all. The loadable entry point is ``ModularPipeline`` against the repo ROOT.
* **``workflow="t2va"`` is mandatory.** Without it ``load_components`` pulls
  BOTH 61.7 GiB transformer partitions. See ``_load``.
* **It is guidance-distilled.** No guider, no ``negative_prompt``, no
  ``guidance_scale``; one forward pass per step. The request schema forbids
  those two fields rather than accepting and ignoring them.
* **Geometry is a hard contract**, enforced inside the pipeline. See the
  ``_CANVAS_MULTIPLE`` / frame-window constants below — this server rejects
  violations at the HTTP edge so they cost nothing instead of costing a load.
* **Video and audio come out as separate outputs** (``videos``, ``audio``,
  ``sampling_rate``) and muxing them is explicitly left to the caller. That
  caller is ``_av_io.write_mp4_with_audio``.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# Force-disable huggingface_hub's xet transport before any HF import. That one
# is a transport kill-switch, not a path, so it belongs here.
#
# HF_HOME is the PROVIDER's to set, not this module's: Modal exports the Volume
# root and RunPod exports <volume>/.hf_cache, both via core.pod_paths.
# pod_path_env. This module used to hardcode Modal's answer, which was right on
# Modal, wrong on RunPod, and unwritable on any CI runner. Getting it wrong does
# not fail loudly — it silently re-downloads 123.8 GiB onto container disk, so
# the provider, which alone knows its own mount, is authoritative. On a host
# with no volume, huggingface_hub's own ~/.cache/huggingface applies.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import numpy as np  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, field_validator  # noqa: E402

from kinoforge.engines.diffusers.servers._av_io import (  # noqa: E402
    write_mp4_with_audio,
)

_log = logging.getLogger("kinoforge.diffusers.minimax_h3_server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# WAN_MODEL_ID is a misnomer here, and deliberately not renamed. The diffusers
# engine's ``render_provision`` derives it from ``cfg.models[kind=base].ref`` and
# exports it for EVERY diffusers cfg; renaming it would move 20 launch-payload
# goldens for configs that have nothing to do with H3. Read it, do not rename it.
MODEL_ID: str = os.environ.get("WAN_MODEL_ID", "MiniMaxAI/MiniMax-H3")
ARTIFACT_DIR: Path = Path(
    os.environ.get("KINOFORGE_ARTIFACT_DIR", "/tmp/kf-artifacts")  # noqa: S108
)

# --- geometry, from diffusers v0.40.0 -------------------------------------
#
# Every one of these is enforced inside the pipeline call, which on Modal means
# a ValueError minutes after a 123.8 GiB load completed on a $4.54/hr card. They
# are re-stated here so a bad request is refused for free at the HTTP edge.
#
#: ``MiniMaxH3ModularPipeline.canvas_multiple`` = vae spatial compression (16) x
#: transformer patch_size[2] (2). A canvas has to survive the VAE's compression
#: and still be a whole number of patch rows wide.
_CANVAS_MULTIPLE = 32
#: ``MINIMAX_H3_FPS``. Fixed by the checkpoint: everything H3 generates and
#: conditions on is resampled onto 24 fps. Not a preference, not overridable.
_FPS = 24
#: The video VAE encodes ``clip_length`` 17 pixel frames per chunk and keeps
#: ``tokens_chunk_size`` 5 latents, so a frame count is snapped UP to the next
#: ``17 * n + 5`` it can decode.
_FRAMES_PER_CHUNK = 17
_LATENTS_PER_CHUNK = 5


def _align_frames(num_frames: int) -> int:
    """Snap *num_frames* up to the next count the video VAE can decode.

    Mirrors ``diffusers.modular_pipelines.minimax_h3.align_num_frames``.

    Args:
        num_frames: The requested count.

    Returns:
        The next value congruent to 5 mod 17, at or above *num_frames*.
    """
    return num_frames + (_LATENTS_PER_CHUNK - num_frames) % _FRAMES_PER_CHUNK


#: ``min_duration`` / ``max_duration``. The gate below checks the duration of
#: the ALIGNED count, which is what the pipeline itself checks — not the raw
#: request. Approximating it with raw frame bounds is wrong in BOTH directions:
#:
#: * too permissive at the top — the raw edge is 360 frames, but 360 aligns up
#:   to 362 = 15.083 s and is refused, so 346..360 would pass a naive bound and
#:   then raise ValueError inside ``before_denoise``, on a booked H200 minutes
#:   after a 124 GiB load. The diffusers source names this exact trap in a
#:   comment. **15.0 s is not reachable; the ceiling is 345 frames = 14.375 s.**
#: * too strict at the bottom — 120 (and 119, and 108) all align up to 124 =
#:   5.167 s, which the model accepts happily.
_MIN_DURATION_S = 5.0
_MAX_DURATION_S = 15.0
#: The longest clip that actually exists, for the error message and the docs.
_MAX_FRAMES = max(
    a
    for a in (_align_frames(n) for n in range(1, int(_MAX_DURATION_S * _FPS) + 1))
    if a / _FPS <= _MAX_DURATION_S
)
#: The blocks' own default for ``num_frames``: the shortest legal clip, and the
#: only ``17 * n + 5`` value in the 120-126 range.
_DEFAULT_FRAMES = 124
#: MiniMax-H3's trained canvas: ``canvas_short_edge`` 768 at 16:9, which is also
#: the ``canvas_max_pixels`` budget (768 * 1344).
_DEFAULT_WIDTH = 1344
_DEFAULT_HEIGHT = 768
#: ``InputParam.template("num_inference_steps")``'s default. Guidance-distilled
#: is not step-distilled: guidance distillation removes the second forward pass
#: per step, it does not shorten the schedule.
_DEFAULT_STEPS = 50

#: Reserve left free on the accelerator by the auto-offload strategy, for
#: activations and intermediates. "12GB" is the value in the diffusers H3
#: single-card recipe, not an invented one.
_OFFLOAD_MARGIN = os.environ.get("KINOFORGE_H3_OFFLOAD_MARGIN", "12GB")

#: Optional attention backend. ``_flash_3_hub`` is documented as roughly 3x
#: faster on Hopper — which H200 is — but it FETCHES KERNELS FROM THE HUB at set
#: time, so enabling it by default would put an unproven network fetch on the
#: critical path of a $4.54/hr card. Left off for the first run; one env var away
#: on a warm pod once the baseline is proven.
_ATTENTION_BACKEND = os.environ.get("KINOFORGE_H3_ATTENTION_BACKEND", "")

app = FastAPI(title="kinoforge minimax-h3 t2va server", version="0.1.0")
ready: threading.Event = threading.Event()
pipe: Any = None  # set in _startup
manager: Any = None  # the ComponentsManager holding the offload hooks
#: Which attention backend is actually in force. Surfaced on /health so a
#: degraded run is visible without reading the boot log — on Modal that log is
#: unreadable once an ephemeral app stops.
attention_backend: str = "default"
jobs: dict[str, JobState] = {}
_q: queue.Queue[str] = queue.Queue()
_worker_thread: threading.Thread | None = None


@dataclass
class JobState:
    """In-process job record updated by the worker thread."""

    job_id: str
    status: Literal["queued", "running", "done", "error"]
    prompt: str
    params: dict[str, Any]
    progress: float = 0.0
    filename: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None


class GenerateRequest(BaseModel):
    """JSON body for ``POST /generate``.

    ``extra="forbid"`` is the load-bearing line. The checkpoint is
    guidance-distilled — there is no guider, no ``negative_prompt`` and no
    ``guidance_scale`` — so a schema that accepted either would be accepting
    something the model cannot use. Refusing is better than ignoring: an
    operator who passes ``guidance_scale`` and sees no error would reasonably
    conclude it had an effect.

    It also refuses unknown keys generally, which matters because
    ``DiffusersBackend.submit`` posts the cfg's whole ``spec`` block as the
    request body: anything the cfg declares that this server does not model is a
    422 at submit time rather than a silent drop. The bookkeeping keys that block
    carries (``model``, ``pipeline``, ``scheduler``, ``fps``) are therefore
    declared below and then deliberately NOT forwarded to the pipeline.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str
    width: int = Field(_DEFAULT_WIDTH, ge=_CANVAS_MULTIPLE, le=4096)
    height: int = Field(_DEFAULT_HEIGHT, ge=_CANVAS_MULTIPLE, le=4096)
    num_frames: int = Field(_DEFAULT_FRAMES, ge=1, le=4096)
    num_inference_steps: int = Field(_DEFAULT_STEPS, ge=1, le=200)
    seed: int | None = None

    # Keys the orchestrator's cfg `spec` block carries for bookkeeping rather
    # than for the pipeline. Declared so `extra="forbid"` does not 422 a
    # perfectly good config, and ignored in the call.
    model: str | None = None
    pipeline: str | None = None
    scheduler: str | None = None
    fps: int | None = None
    # NOT written by any cfg author: `core/strategy.py:55` injects `_audio_mode`
    # into the job spec, and `DiffusersBackend.submit` posts that spec as the
    # body. Undeclared, it was a 422 on /generate — after the H200 had booted
    # and loaded 124 GiB, which is the most expensive place to learn it (live
    # 2026-09-18). Pydantic forbids a field name starting with an underscore, so
    # it arrives by alias.
    #
    # Declaring it here is NOT a use of the seam. The marker is read nowhere —
    # see the note at core/strategy.py:55 — and this server ignores its value;
    # H3's audio reaches the output through _av_io.write_mp4_with_audio. It is
    # declared only so the request the orchestrator actually sends is accepted.
    audio_mode: str | None = Field(None, alias="_audio_mode")

    @field_validator("width", "height")
    @classmethod
    def _check_canvas_multiple(cls, v: int) -> int:
        """Reject a canvas axis the VAE and transformer cannot tile.

        Args:
            v: The requested width or height.

        Returns:
            *v* unchanged.

        Raises:
            ValueError: *v* is not a multiple of 32.
        """
        if v % _CANVAS_MULTIPLE:
            raise ValueError(
                f"must be a multiple of {_CANVAS_MULTIPLE} "
                f"(MiniMax-H3 canvas_multiple), got {v}"
            )
        return v

    @field_validator("num_frames")
    @classmethod
    def _check_aligned_duration(cls, v: int) -> int:
        """Reject a count whose ALIGNED duration falls outside the window.

        This is the pipeline's own check, not an approximation of it: the count
        is snapped up to the next ``17 * n + 5`` and the duration of THAT is
        what has to land in 5-15 s. Doing it any other way is wrong at both
        ends — see the note on ``_MIN_DURATION_S``.

        Args:
            v: The requested frame count.

        Returns:
            *v* unchanged.

        Raises:
            ValueError: The aligned duration is outside the window.
        """
        aligned = _align_frames(v)
        duration = aligned / _FPS
        if not _MIN_DURATION_S <= duration <= _MAX_DURATION_S:
            raise ValueError(
                f"num_frames={v} aligns up to {aligned} ({duration:.3f} s at "
                f"{_FPS} fps), outside MiniMax-H3's {_MIN_DURATION_S:g}-"
                f"{_MAX_DURATION_S:g} s window. The frame count is snapped to the "
                f"next 17*n+5 the video VAE can decode, so the longest clip that "
                f"exists is {_MAX_FRAMES} frames ({_MAX_FRAMES / _FPS:.3f} s) — "
                f"{_MAX_DURATION_S:g} s itself is not reachable."
            )
        return v

    @field_validator("fps")
    @classmethod
    def _check_fps_is_the_models_own(cls, v: int | None) -> int | None:
        """Reject an fps other than H3's fixed 24.

        Args:
            v: The declared fps, or None.

        Returns:
            *v* unchanged.

        Raises:
            ValueError: *v* is neither None nor 24.
        """
        if v is not None and v != _FPS:
            raise ValueError(
                f"MiniMax-H3 generates at a fixed {_FPS} fps; got {v}. Frames are "
                "resampled onto it, so a different value here would mislabel the "
                "container and desync the jointly generated soundtrack."
            )
        return v


def _log_memory_facts() -> None:
    """Log host and device memory before the load. Never raises.

    Under ``enable_auto_cpu_offload`` the 123.8 GiB of bf16 weights live in HOST
    RAM and only the component a block currently needs is moved onto the
    accelerator. Modal's default container memory *request* is 128 MiB, with
    "can exceed this if the worker has available memory" — so the host-RAM
    headroom is a property of the machine we landed on, not of our request.

    That makes these two numbers the difference between a legible shortfall and
    an unexplained container death: if MemAvailable is well under ~130 GiB, the
    load will be OOM-killed and this line is the only place that says why.
    """
    try:
        meminfo = dict(
            (parts[0].rstrip(":"), parts[1])
            for parts in (
                line.split() for line in Path("/proc/meminfo").read_text().splitlines()
            )
            if len(parts) >= 2
        )
        total = int(meminfo.get("MemTotal", 0)) / 1024 / 1024
        avail = int(meminfo.get("MemAvailable", 0)) / 1024 / 1024
        _log.info(
            "memory: host MemTotal=%.1f GiB MemAvailable=%.1f GiB "
            "(weights need ~124 GiB of host RAM under auto offload)",
            total,
            avail,
        )
    except Exception as exc:  # noqa: BLE001 — diagnostics must never block a load
        _log.warning("memory: could not read /proc/meminfo: %s", exc)
    try:
        import torch

        if torch.cuda.is_available():
            free, capacity = torch.cuda.mem_get_info()
            _log.info(
                "memory: device free=%.1f GiB capacity=%.1f GiB (offload margin %s)",
                free / 1024**3,
                capacity / 1024**3,
                _OFFLOAD_MARGIN,
            )
        else:
            _log.warning("memory: torch reports no CUDA device")
    except Exception as exc:  # noqa: BLE001
        _log.warning("memory: could not read device memory: %s", exc)


def _torch_build() -> dict[str, str | None]:
    """Return the installed torch wheel's identity, or empty on failure.

    Returns:
        ``{"version": ..., "cuda": ...}``. ``torch.__version__`` verbatim, since
        ``2.6.0`` and ``2.6.0+cu124`` are different wheels with different CUDA
        vendoring and are otherwise indistinguishable in a log. On Modal the
        startup log is unreadable once an ephemeral app stops, so ``/health`` is
        the only surface that can answer "which torch?" after the fact.
    """
    try:
        import torch

        return {"version": str(torch.__version__), "cuda": torch.version.cuda}
    except Exception:  # noqa: BLE001
        return {}


def _load() -> tuple[Any, Any]:
    """Build the H3 t2va pipeline and enable auto CPU offload.

    Two things here are load-bearing and expensive to get wrong.

    **``workflow="t2va"``.** The diffusers blocks' own description says it in
    terms: *"Without a ``workflow=``, loading the components pulls both 61.7GB
    transformer partitions."* The t2va workflow uses ``transformer/``;
    ``transformer_ref/`` belongs to ``ref2va`` and was deliberately NOT
    prefetched onto the Volume. So omitting this kwarg is a 66 GB download on an
    H200 at $4.54/hr — the most expensive single mistake available here.

    **The offload mechanism.** ``ModularPipeline`` does not subclass
    ``DiffusionPipeline`` and has NO ``enable_model_cpu_offload`` method —
    calling it is an ``AttributeError``, and it would be one on a booked card.
    The mechanism is ``ComponentsManager.enable_auto_cpu_offload``: every model
    starts on CPU, moves onto the accelerator when a block calls it, and is
    evicted when another needs the room. This is the recipe the diffusers H3 doc
    gives for a single card, and it matters because the two large components are
    61.7 GiB (transformer) and 62.1 GiB (Qwen3-VL conditioner) against an H200's
    131.3 GiB: resident together they leave ~7 GiB for activations.

    Test seam: when ``KINOFORGE_H3_LOAD_STUB`` names a dotted path
    (``pkg.mod.callable``), that callable is imported and called instead, so the
    HTTP contract and the mux can be exercised without torch or CUDA.

    Returns:
        ``(pipe, manager)`` — the pipeline and the ComponentsManager holding its
        offload hooks. The manager is returned rather than dropped because
        dropping it is how the hooks get garbage-collected.

    Raises:
        ImportError: ``KINOFORGE_H3_LOAD_STUB`` is set but unimportable.
    """
    import importlib

    stub_path = os.environ.get("KINOFORGE_H3_LOAD_STUB", "")
    if stub_path:
        try:
            module_name, _, attr = stub_path.rpartition(".")
            if not module_name:
                raise ImportError(f"invalid dotted path: {stub_path!r}")
            mod = importlib.import_module(module_name)
            loaded: tuple[Any, Any] = getattr(mod, attr)()
            return loaded
        except (ImportError, AttributeError) as exc:
            raise ImportError(f"KINOFORGE_H3_LOAD_STUB={stub_path!r}: {exc}") from exc

    import torch
    from diffusers import ComponentsManager, ModularPipeline

    components_manager = ComponentsManager()
    # Repo ROOT, not the nested FL2VA/ layout: FL2VA's manifest declares
    # `MiniMaxH3Pipeline`, a class diffusers does not export.
    pipe_obj = ModularPipeline.from_pretrained(
        MODEL_ID,
        workflow="t2va",  # <- see the docstring. Do not remove.
        components_manager=components_manager,
    )
    pipe_obj.load_components(dtype=torch.bfloat16)
    components_manager.enable_auto_cpu_offload(
        device="cuda", memory_reserve_margin=_OFFLOAD_MARGIN
    )
    return pipe_obj, components_manager


def _apply_attention_backend(pipe_obj: Any) -> str:  # noqa: ANN401 — a ModularPipeline; diffusers is pod-only
    """Opt into a faster attention backend, without ever failing the boot.

    ``_flash_3_hub`` is documented at roughly 3x on Hopper (which H200 is), but
    setting it FETCHES KERNELS FROM THE HUB. An unguarded call therefore puts a
    network fetch on the critical path of the FastAPI startup handler: if it
    raises, uvicorn never binds the port, ``/health`` never answers, and the
    orchestrator waits its full ``boot_timeout`` — 45 minutes on a $4.54/hr card,
    up to $3.40, to discover that an OPTIONAL speed-up was unavailable.

    Degrading to the stock SDPA backend costs a slower run. Aborting the boot
    costs the whole pod. So this degrades, loudly: the WARNING is the deliverable,
    because a silent fallback would make a 3x regression look like the model
    simply being slow.

    Called from ``_startup`` rather than from inside ``_load`` so that it runs on
    the stub path too and is reachable from a test.

    Args:
        pipe_obj: The loaded pipeline.

    Returns:
        The backend actually in force: the requested name, or ``"default"``.
    """
    if not _ATTENTION_BACKEND:
        return "default"
    try:
        pipe_obj.transformer.set_attention_backend(_ATTENTION_BACKEND)
    except Exception as exc:  # noqa: BLE001 — an optional speed-up must not brick a boot
        _log.warning(
            "startup: attention backend %r could NOT be set (%s: %s); continuing on "
            "the default backend. Expect roughly 3x slower denoise on Hopper — this "
            "is a degraded run, not a slow model.",
            _ATTENTION_BACKEND,
            type(exc).__name__,
            exc,
        )
        return "default"
    _log.info("startup: attention backend set to %s", _ATTENTION_BACKEND)
    return _ATTENTION_BACKEND


def _seed_to_generator(seed: int | None) -> Any:  # noqa: ANN401 — torch.Generator is opaque here (torch is pod-only)
    """Return a seeded CPU generator, or None.

    A request draws its conditioning noise, then video noise, then audio noise
    from this one generator, so two runs from the same seed return the same
    video AND the same soundtrack.

    Args:
        seed: The requested seed, or None for an unseeded run.

    Returns:
        A ``torch.Generator`` seeded with *seed*, or None.
    """
    if seed is None:
        return None
    import torch

    # CPU generator on purpose: under auto offload the execution device moves
    # per component, so a device-pinned generator would be bound to whichever
    # device happened to be current at construction time.
    return torch.Generator().manual_seed(seed)


def _to_uint8_frames(videos: Any) -> np.ndarray:  # noqa: ANN401 — the pipeline output is np OR a list of PIL images
    """Coerce the pipeline's ``videos`` output to ``(T, H, W, 3)`` uint8.

    With ``output_type="np"`` the decode step returns
    ``(batch, frames, H, W, 3)`` float in [0, 1]; with ``"pil"`` it returns a
    list of per-batch lists of images. Both are handled so that a future
    ``output_type`` change degrades into a correct conversion rather than into
    silently mis-scaled pixels.

    Args:
        videos: The ``videos`` output, batch dimension first.

    Returns:
        A ``(T, H, W, 3)`` uint8 numpy array.
    """
    first = videos[0]
    if hasattr(first, "shape"):
        arr = np.asarray(first)
    else:  # list of PIL images
        arr = np.stack([np.asarray(im) for im in first], axis=0)
    if arr.dtype != np.uint8:
        arr = (arr * 255).clip(0, 255).astype(np.uint8)
    return arr


def _to_interleaved_audio(audio: Any) -> np.ndarray:  # noqa: ANN401 — the pipeline output is a torch.Tensor on the pod, ndarray under the stub
    """Coerce the pipeline's ``audio`` output to ``(samples, channels)`` float.

    The audio decode step returns ``(1, 2, num_samples)`` — batch, then the two
    stereo channels channel-major, which is how the mono audio VAE emits them.
    ``_av_io.write_wav`` wants ``(samples, channels)`` because that is what
    interleaved PCM is. The transpose between those two layouts is the whole
    function, and getting it wrong does not raise: it would present 2 samples of
    165k-channel audio.

    Args:
        audio: The ``audio`` output, shaped ``(1, channels, samples)``.

    Returns:
        A ``(samples, channels)`` float32 numpy array.
    """
    arr = np.asarray(
        audio.float().cpu() if hasattr(audio, "cpu") else audio, dtype=np.float32
    )
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim == 1:
        arr = arr[None, :]
    # (channels, samples) -> (samples, channels). `.T` permutes; a reshape here
    # would interleave the two channels into each other.
    return np.ascontiguousarray(arr.T)


def _run_job(state: JobState) -> None:
    """Generate one clip and write it, audio included.

    Args:
        state: The job to run; mutated in place with the outcome.
    """
    state.status = "running"
    state.started_at = time.time()
    try:
        params = state.params
        results = pipe(
            prompt=state.prompt,
            height=params["height"],
            width=params["width"],
            num_frames=params["num_frames"],
            num_inference_steps=params["num_inference_steps"],
            generator=_seed_to_generator(params.get("seed")),
            output_type="np",
            # The three outputs the blocks declare. `output=` as a LIST returns
            # a dict of exactly these; the single-string form would return the
            # frames alone and drop the soundtrack one layer above the mux.
            output=["videos", "audio", "sampling_rate"],
        )
        frames = _to_uint8_frames(results["videos"])
        audio = _to_interleaved_audio(results["audio"])
        # The audio VAE reports its own rate. Reading it back rather than
        # hardcoding 32000 is what keeps the mux honest if a checkpoint ships a
        # different one — a wrong rate plays at the wrong speed while staying
        # perfectly in sync with itself, so no duration check would catch it.
        sample_rate = int(results["sampling_rate"])
        filename = f"{state.job_id}.mp4"
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        write_mp4_with_audio(
            frames,
            audio,
            fps=_FPS,
            sample_rate=sample_rate,
            path=ARTIFACT_DIR / filename,
        )
        _log.info(
            "job %s done: %d frames %dx%d, %d audio samples at %d Hz",
            state.job_id,
            frames.shape[0],
            frames.shape[2],
            frames.shape[1],
            audio.shape[0],
            sample_rate,
        )
        state.filename = filename
        state.status = "done"
    except Exception as e:  # noqa: BLE001 — one bad job must not kill the worker
        _log.exception("job %s failed", state.job_id)
        state.error = f"{type(e).__name__}: {e}"
        state.status = "error"
    finally:
        state.finished_at = time.time()


def _worker_loop() -> None:
    """Drain the job queue forever, one job at a time."""
    while True:
        job_id = _q.get()
        state = jobs.get(job_id)
        if state is None:
            _log.warning("worker: job %s vanished from registry", job_id)
            continue
        _run_job(state)


@app.on_event("startup")
def _startup() -> None:
    """Log the environment, load the pipeline, spawn the worker, report ready."""
    global pipe, manager, _worker_thread
    _log.info("startup: torch build %s", _torch_build())
    _log_memory_facts()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    _log.info("startup: loading %s workflow=t2va", MODEL_ID)
    t0 = time.monotonic()
    pipe, manager = _load()
    global attention_backend
    attention_backend = _apply_attention_backend(pipe)
    _log.info("startup: loaded in %.1f s", time.monotonic() - t0)
    _log_memory_facts()
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    _worker_thread.start()
    ready.set()
    _log.info("startup: worker spawned, server ready")


@app.get("/health")
def health() -> dict[str, Any]:
    """Return readiness, model identity, capabilities and the torch build.

    Returns:
        The health payload. ``capabilities`` carries ``t2va`` once the pipeline
        is up — the matcher's pre-flight treats that list as a closed vocabulary.
    """
    return {
        "ready": ready.is_set(),
        "model": MODEL_ID,
        "capabilities": ["t2va"] if ready.is_set() else [],
        "attention_backend": attention_backend,
        "torch": _torch_build(),
    }


@app.get("/util")
def util() -> dict[str, Any]:
    """Per-tick GPU/CPU/mem stats for the provider util probe.

    Sync def on purpose: FastAPI runs sync handlers in a threadpool, so the
    blocking pynvml / nvidia-smi / psutil reads cannot stall the event loop —
    which matters because this route is the only health signal a long generation
    has, and a stalled loop would make a busy pod look dead.

    Returns:
        The five ``UtilSnapshot`` fields as a plain dict.
    """
    from kinoforge.engines.diffusers.servers._util_stats import read_gpu_stats

    return read_gpu_stats()


@app.post("/generate")
def generate(req: GenerateRequest) -> dict[str, str]:
    """Enqueue a t2va job; return its server-assigned id.

    Args:
        req: The validated request.

    Returns:
        ``{"job_id": ...}``.

    Raises:
        HTTPException: 503 while the pipeline is still loading.
    """
    if not ready.is_set():
        raise HTTPException(status_code=503, detail="model loading")
    job_id = uuid.uuid4().hex
    jobs[job_id] = JobState(
        job_id=job_id,
        status="queued",
        prompt=req.prompt,
        params=req.model_dump(),
    )
    _q.put(job_id)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str) -> dict[str, Any]:
    """Return the current state of ``job_id``.

    Args:
        job_id: The id returned by ``/generate``.

    Returns:
        The status payload; ``filename``/``url`` when done, ``error`` on failure.

    Raises:
        HTTPException: 404 for an unknown id.
    """
    state = jobs.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    out: dict[str, Any] = {"status": state.status, "progress": state.progress}
    if state.status == "done" and state.filename is not None:
        out["filename"] = state.filename
        out["url"] = f"http://localhost:8000/artifacts/{state.filename}"
    elif state.status == "error" and state.error is not None:
        out["error"] = state.error
    return out


@app.get("/artifacts/{filename}")
def artifact(filename: str) -> Any:  # noqa: ANN401 — returns FileResponse, opaque here
    """Serve a generated MP4 by filename with a path-traversal guard.

    Args:
        filename: The bare filename from ``/status``.

    Returns:
        A ``FileResponse`` for the MP4.

    Raises:
        HTTPException: 400 on a traversal attempt, 404 when absent.
    """
    from fastapi.responses import FileResponse

    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    target = (ARTIFACT_DIR / filename).resolve()
    try:
        target.relative_to(ARTIFACT_DIR.resolve())
    except ValueError as e:
        raise HTTPException(status_code=400, detail="path escapes artifact dir") from e
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(str(target), media_type="video/mp4", filename=filename)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
