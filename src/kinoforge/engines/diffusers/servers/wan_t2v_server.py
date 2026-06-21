"""FastAPI inference server for Wan 2.2 T2V-A14B.

Runs on the GPU pod. Exposes the DiffusersBackend HTTP contract:

  GET  /health                  -> {"ready": bool, "model": str}
  POST /generate                -> {"job_id": str}
  GET  /status/{job_id}         -> {"status": ..., ...}
  GET  /artifacts/{filename}    -> MP4 bytes (added in Task 6)

Model loaded once at startup, persists across requests.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import shutil
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

# Force-disable huggingface_hub's xet transport before any HF import.
# Task 8 attempt #8 surfaced a hard xet failure:
#   RuntimeError: Task error: File reconstruction error:
#   Internal Writer Error: Background writer channel closed
# during from_pretrained(MODEL_ID) on the freshly-provisioned pod.
# xet is HF's newer content-addressed transport; the legacy HTTP
# transport is reliable and the 70 GB cost of "less efficient" is a
# rounding error compared to the smoke budget. Set BEFORE any
# huggingface_hub import so the global xet kill-switch is honored.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# Pin HF cache onto the /workspace volume so the 70 GB shard download
# does not exhaust the 50 GB container disk. /workspace is the RunPod
# volume mount (volumeInGb in the cfg's requirements.disk_gb).
os.environ.setdefault("HF_HOME", "/workspace/.hf_cache")

from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from kinoforge.engines.diffusers.servers._video_io import write_mp4  # noqa: E402

_log = logging.getLogger("kinoforge.diffusers.wan_t2v_server")

MODEL_ID: str = os.environ.get("WAN_MODEL_ID", "Wan-AI/Wan2.2-T2V-A14B-Diffusers")
ARTIFACT_DIR: Path = Path("/workspace/artifacts")
LORAS_DIR: Path = Path(os.environ.get("KINOFORGE_LORAS_DIR", "/workspace/loras"))

_DEFAULT_NEGATIVE_PROMPT = (
    "Bright tones, overexposed, static, blurred details, subtitles, "
    "style, works, paintings, images, static, overall gray, worst "
    "quality, low quality, JPEG compression residue, ugly, incomplete, "
    "extra fingers, poorly drawn hands, poorly drawn faces, deformed, "
    "disfigured, misshapen limbs, fused fingers, still picture, messy "
    "background, three legs, many people in the background, walking "
    "backwards"
)

app = FastAPI(title="kinoforge wan-t2v server", version="0.1.0")
ready: threading.Event = threading.Event()
pipe: Any = None  # set in _startup
jobs: dict[str, JobState] = {}
_q: queue.Queue[str] = queue.Queue()
_worker_thread: threading.Thread | None = None

# LoRA-flexible warm-reuse: pod-side inventory of loaded LoRA weights.
# Entries are keyed by vendor-neutral ref (e.g. "civitai:2197303@2474081").
# Populated cold-boot in _load_pipeline; mutated by /lora/set_stack.
_inventory: dict[str, dict[str, Any]] = {}

# Serializes /lora/set_stack handler invocations so two concurrent swaps
# cannot fight over _inventory + pipeline adapter state. Acquired for the
# duration of (diff + evict + download + reload).
_swap_lock: asyncio.Lock = asyncio.Lock()


class ArtifactDownloadSpec(BaseModel):
    """Pre-resolved LoRA download instruction sent by the orchestrator.

    The orchestrator resolves vendor-specific download URLs + headers
    (CivitAI bearer tokens, HF auth, etc.) on its side and ships an
    opaque spec to the pod. The pod fetches the bytes verbatim with
    no vendor-specific code paths.
    """

    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    filename: str
    size_hint: int | None = None


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


class LoraInventoryEntry(BaseModel):
    """One row of the pod's LoRA inventory exposed over HTTP."""

    ref: str
    filename: str
    size_bytes: int
    downloaded_at_local: str
    last_used_at_local: str
    adapter_name: str


class SwapRejectedDetails(BaseModel):
    """Why a /lora/set_stack call could not be honored as requested."""

    reason: str
    target_refs_dropped: list[str]


class SetStackRequest(BaseModel):
    """Declarative target LoRA stack for the pod.

    Order of ``target_refs`` defines pipeline adapter ordering. Every ref in
    ``target_refs`` that is not already in the pod's inventory must have a
    matching entry in ``download_specs``.
    """

    target_refs: list[str]
    download_specs: dict[str, ArtifactDownloadSpec]


class SetStackResponse(BaseModel):
    """Post-swap pod inventory + free disk + optional rejection details."""

    inventory: list[LoraInventoryEntry]
    free_bytes: int
    swap_rejected: SwapRejectedDetails | None = None


def _inventory_snapshot() -> list[LoraInventoryEntry]:
    """Return a Pydantic snapshot of ``_inventory`` in current dict order."""
    return [LoraInventoryEntry(**v) for v in _inventory.values()]


class GenerateRequest(BaseModel):
    """JSON body for ``POST /generate``."""

    prompt: str
    negative_prompt: str | None = None
    width: int = Field(480, ge=8, le=2048)
    height: int = Field(480, ge=8, le=2048)
    num_frames: int = Field(81, ge=1, le=1024)
    fps: int = Field(16, ge=1, le=120)
    num_inference_steps: int = Field(20, ge=1, le=200)
    guidance_scale: float = Field(6.0, ge=0.0, le=20.0)
    seed: int | None = None


def _diffusers_load() -> Any:  # noqa: ANN401 — diffusers.WanPipeline has no public TypeAlias.
    """Construct and return the bare WanPipeline.

    Loads weights with ``device_map="cuda"`` so each component streams
    DIRECTLY to GPU memory and is never held in CPU RAM. The Wan 2.2
    MoE has two 14B transformers plus an 11 GB UMT5-XXL text encoder
    — staging all of that in CPU first OOM-kills any pod with less
    than ~80 GB CPU RAM (Task 8 attempts #17 / #19 / #21 — pod CPU
    RAM allocation varies machine-to-machine even with the same
    ``minMemoryInGb`` filter). Streaming straight to the 80 GB A100
    sidesteps the variable-RAM problem entirely.

    Separated from ``_load_pipeline`` so tests can patch this seam
    without importing diffusers (which would otherwise pull torch +
    CUDA at test time).
    """
    import torch
    from diffusers import WanPipeline

    pipe_obj = WanPipeline.from_pretrained(
        MODEL_ID,
        torch_dtype=torch.bfloat16,
        device_map="cuda",
    )
    return pipe_obj


def _download_one(spec: ArtifactDownloadSpec, dest_dir: Path) -> tuple[str, int]:
    """Download one LoRA spec to dest_dir.

    Streams to a temp ``.partial`` file and renames on success so partial
    downloads never present as complete LoRA files. Raises ``RuntimeError``
    on any HTTP / IO error after cleaning up the partial.

    Args:
        spec: Vendor-resolved download instruction.
        dest_dir: Directory to land the file in (created if missing).

    Returns:
        Tuple of (absolute path on disk, actual bytes written).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / spec.filename
    tmp = dest_dir / f"{spec.filename}.partial"
    req = urllib.request.Request(spec.url, headers=spec.headers)  # noqa: S310 — vendor-resolved URL
    bytes_written = 0
    try:
        with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:  # noqa: S310
            while True:
                chunk = resp.read(64 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                bytes_written += len(chunk)
        tmp.replace(target)
        return str(target), bytes_written
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _disk_free_bytes(path: Path) -> int:
    """Return free bytes on the filesystem containing ``path``."""
    return shutil.disk_usage(path).free


def _pick_lru_evict(
    candidates: set[str], inventory: dict[str, dict[str, Any]], need: int
) -> list[str] | None:
    """Return refs to evict in LRU order, popping until cumulative size ≥ need.

    Args:
        candidates: Refs eligible for eviction (i.e. not in target stack).
        inventory: Current ``_inventory`` snapshot.
        need: Bytes that must be freed. ``<= 0`` → no eviction needed.

    Returns:
        List of refs in LRU-ascending order, or ``None`` if even evicting
        every candidate would not free ``need`` bytes. Returns ``[]`` when
        ``need <= 0``.
    """
    if need <= 0:
        return []
    ordered = sorted(
        (ref for ref in candidates if ref in inventory),
        key=lambda r: inventory[r]["last_used_at_local"],
    )
    freed = 0
    plan: list[str] = []
    for ref in ordered:
        plan.append(ref)
        freed += inventory[ref]["size_bytes"]
        if freed >= need:
            return plan
    return None


async def _evict_one(ref: str) -> None:
    """Unload one LoRA from the pipeline + remove its file + drop inventory.

    Best-effort: filesystem unlink errors are swallowed because the
    inventory is the source of truth for future swap decisions; a leaked
    file gets cleaned up by the next disk-pressure eviction or by the
    reaper.
    """
    entry = _inventory.get(ref)
    if entry is None:
        return
    adapter = entry["adapter_name"]
    if hasattr(pipe, "delete_adapters"):
        pipe.delete_adapters([adapter])
    try:
        Path(entry["loras_dir_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    _inventory.pop(ref, None)


async def _reload_pipeline_loras(target_refs: list[str]) -> None:
    """Replace the active pipeline adapter stack with ``target_refs`` in order.

    Calls ``unload_lora_weights()`` first to clear any active adapters,
    then re-loads each target ref as ``lora_{i}``, then ``set_adapters``
    with the positional list. Empty ``target_refs`` → unload only.
    """
    pipe.unload_lora_weights()
    if not target_refs:
        return
    names: list[str] = []
    for i, ref in enumerate(target_refs):
        entry = _inventory[ref]
        name = f"lora_{i}"
        pipe.load_lora_weights(entry["loras_dir_path"], adapter_name=name)
        names.append(name)
        entry["adapter_name"] = name
    pipe.set_adapters(names)


def _load_pipeline(
    initial_lora_stack: list[tuple[str, ArtifactDownloadSpec]] | None = None,
) -> Any:  # noqa: ANN401 — diffusers.WanPipeline has no public TypeAlias.
    """Load the Wan pipeline + optionally cold-boot a LoRA stack.

    Args:
        initial_lora_stack: Optional list of (ref, download_spec) tuples
            to download + load before the first /generate. Order matters —
            adapter names are assigned positionally as ``lora_{i}``.

    Returns:
        The constructed pipeline with any initial LoRAs already attached.
    """
    pipe_obj = _diffusers_load()
    if initial_lora_stack:
        adapter_names: list[str] = []
        for i, (ref, spec) in enumerate(initial_lora_stack):
            try:
                path, actual_bytes = _download_one(spec, LORAS_DIR)
            except Exception as e:
                raise RuntimeError(f"failed to download LoRA {ref}: {e}") from e
            adapter_name = f"lora_{i}"
            pipe_obj.load_lora_weights(path, adapter_name=adapter_name)
            adapter_names.append(adapter_name)
            now = datetime.now().isoformat()
            _inventory[ref] = {
                "ref": ref,
                "filename": spec.filename,
                "size_bytes": actual_bytes,
                "loras_dir_path": path,
                "downloaded_at_local": now,
                "last_used_at_local": now,
                "adapter_name": adapter_name,
            }
        if adapter_names:
            pipe_obj.set_adapters(adapter_names)
    return pipe_obj


def _seed_to_generator(seed: int | None) -> Any:  # noqa: ANN401 — torch.Generator opaque here.
    if seed is None:
        return None
    import torch

    g = torch.Generator(device="cuda")
    g.manual_seed(seed)
    return g


def _worker_loop() -> None:
    """Drain the job queue, running pipeline + writing MP4 per job.

    Belt-and-braces try/except keeps the thread alive on any exception
    so one broken job does not block the queue.
    """
    import numpy as np

    while True:
        job_id = _q.get()
        state = jobs.get(job_id)
        if state is None:
            _log.warning("worker: job %s vanished from registry", job_id)
            continue
        state.status = "running"
        state.started_at = time.time()
        try:
            output = pipe(
                prompt=state.prompt,
                negative_prompt=state.params.get("negative_prompt")
                or _DEFAULT_NEGATIVE_PROMPT,
                height=state.params["height"],
                width=state.params["width"],
                num_frames=state.params["num_frames"],
                num_inference_steps=state.params["num_inference_steps"],
                guidance_scale=state.params["guidance_scale"],
                generator=_seed_to_generator(state.params.get("seed")),
            )
            frames = output.frames[0]
            # diffusers returns either a list of PIL images or a numpy
            # array depending on output_type. Coerce to (T, H, W, 3) uint8.
            if hasattr(frames, "shape"):
                arr = np.asarray(frames)
                if arr.dtype != np.uint8:
                    arr = (arr * 255).clip(0, 255).astype(np.uint8)
            else:
                arr = np.stack([np.asarray(im) for im in frames], axis=0)
                if arr.dtype != np.uint8:
                    arr = (arr * 255).clip(0, 255).astype(np.uint8)
            filename = f"{job_id}.mp4"
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            write_mp4(arr, fps=state.params["fps"], path=ARTIFACT_DIR / filename)
            state.filename = filename
            state.status = "done"
        except Exception as e:  # noqa: BLE001
            _log.exception("worker: job %s failed", job_id)
            state.error = f"{type(e).__name__}: {e}"
            state.status = "error"
        finally:
            state.finished_at = time.time()


@app.on_event("startup")
def _startup() -> None:
    """Load the pipeline, spawn worker, mark server ready.

    If ``KINOFORGE_INITIAL_LORA_STACK_JSON`` points to a readable JSON
    file shaped ``[[ref, {spec...}], ...]``, those LoRAs are downloaded
    + loaded before the server reports ready.
    """
    global pipe, _worker_thread
    _log.info("startup: loading pipeline %s", MODEL_ID)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    LORAS_DIR.mkdir(parents=True, exist_ok=True)
    stack_path = os.environ.get("KINOFORGE_INITIAL_LORA_STACK_JSON")
    if stack_path and Path(stack_path).exists():
        import json

        raw = json.loads(Path(stack_path).read_text())
        initial: list[tuple[str, ArtifactDownloadSpec]] = [
            (ref, ArtifactDownloadSpec(**spec_dict)) for ref, spec_dict in raw
        ]
        _log.info("startup: cold-boot LoRA stack size=%d", len(initial))
        pipe = _load_pipeline(initial_lora_stack=initial)
    else:
        pipe = _load_pipeline()
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    _worker_thread.start()
    ready.set()
    _log.info("startup: pipeline loaded + worker spawned, server ready")


@app.get("/health")
def health() -> dict[str, Any]:
    """Return readiness + model identity."""
    return {"ready": ready.is_set(), "model": MODEL_ID}


@app.post("/generate")
def generate(req: GenerateRequest) -> dict[str, str]:
    """Enqueue a job; return its server-assigned id."""
    if not ready.is_set():
        raise HTTPException(status_code=503, detail="model loading")
    job_id = uuid.uuid4().hex
    state = JobState(
        job_id=job_id,
        status="queued",
        prompt=req.prompt,
        params=req.model_dump(),
    )
    jobs[job_id] = state
    _q.put(job_id)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str) -> dict[str, Any]:
    """Return the current state of ``job_id``; 404 if unknown."""
    state = jobs.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    out: dict[str, Any] = {
        "status": state.status,
        "progress": state.progress,
    }
    if state.status == "done" and state.filename is not None:
        out["filename"] = state.filename
        out["url"] = f"http://localhost:8000/artifacts/{state.filename}"
    elif state.status == "error" and state.error is not None:
        out["error"] = state.error
    return out


@app.get("/artifacts/{filename}")
def artifact(filename: str) -> Any:  # noqa: ANN401 — returns FileResponse, opaque here.
    """Serve a generated MP4 by filename with path-traversal guard."""
    from fastapi.responses import FileResponse

    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    target = (ARTIFACT_DIR / filename).resolve()
    artifact_dir_resolved = ARTIFACT_DIR.resolve()
    try:
        target.relative_to(artifact_dir_resolved)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="path escapes artifact dir") from e
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(str(target), media_type="video/mp4", filename=filename)


class InventoryResponse(BaseModel):
    """Read-only snapshot of the pod's LoRA inventory + free disk bytes."""

    inventory: list[LoraInventoryEntry]
    free_bytes: int


@app.get("/lora/inventory")
async def inventory() -> InventoryResponse:
    """Return the pod's current LoRA inventory + free disk under the swap lock.

    Holding ``_swap_lock`` for the read guarantees the snapshot cannot race a
    concurrent ``/lora/set_stack`` mid-mutation.
    """
    async with _swap_lock:
        return InventoryResponse(
            inventory=_inventory_snapshot(),
            free_bytes=_disk_free_bytes(LORAS_DIR),
        )


@app.post("/lora/set_stack")
async def set_stack(req: SetStackRequest) -> SetStackResponse:
    """Apply ``req.target_refs`` as the pod's active LoRA stack.

    Idempotent in the no-op case (target == current). On a partial-overlap
    request the handler downloads only the new refs, optionally evicting
    LRU losers first if free disk is insufficient, then reloads the
    pipeline so ``set_adapters`` matches ``req.target_refs`` order.

    Args:
        req: Declarative target stack + per-new-ref download specs.

    Returns:
        Post-swap inventory snapshot + free disk bytes. ``swap_rejected``
        stays ``None`` on the happy path; failure-path subclasses set it in
        Task 8.

    Raises:
        RuntimeError: When even evicting every eligible candidate would not
            free enough disk for the requested downloads. The matcher is
            expected to catch this upstream; reaching it here indicates a
            matcher / pod inventory drift.
    """
    async with _swap_lock:
        target_set = set(req.target_refs)
        current_set = set(_inventory.keys())
        mandatory_evict = current_set - target_set
        to_download_refs = [r for r in req.target_refs if r not in current_set]

        initial_free = _disk_free_bytes(LORAS_DIR)
        target_dl_bytes = sum(
            (req.download_specs[r].size_hint or 0) for r in to_download_refs
        )
        mandatory_freed = sum(_inventory[r]["size_bytes"] for r in mandatory_evict)
        # Snapshot pre-swap state for VRAM-OOM rollback.
        previous_refs = list(_inventory.keys())

        for ref in mandatory_evict:
            await _evict_one(ref)

        post_mandatory_free = initial_free + mandatory_freed
        evict_completed: list[str] = list(mandatory_evict)
        if target_dl_bytes > post_mandatory_free:
            picked = _pick_lru_evict(
                set(_inventory.keys()) - target_set,
                _inventory,
                need=target_dl_bytes - post_mandatory_free,
            )
            if picked is None:
                raise HTTPException(
                    status_code=507,
                    detail={
                        "error": "disk_full",
                        "phase": "plan",
                        "evict_completed": evict_completed,
                        "download_completed": [],
                        "download_failed": None,
                        "underlying": "insufficient disk even after full eviction",
                    },
                )
            for ref in picked:
                await _evict_one(ref)
                evict_completed.append(ref)

        download_completed: list[str] = []
        for ref in to_download_refs:
            spec = req.download_specs[ref]
            try:
                path, actual_bytes = _download_one(spec, LORAS_DIR)
            except OSError as e:
                if e.errno == 28:  # ENOSPC
                    raise HTTPException(
                        status_code=507,
                        detail={
                            "error": "disk_full",
                            "phase": "download",
                            "evict_completed": evict_completed,
                            "download_completed": download_completed,
                            "download_failed": ref,
                            "underlying": str(e),
                        },
                    ) from e
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "lora_download_failed",
                        "phase": "download",
                        "evict_completed": evict_completed,
                        "download_completed": download_completed,
                        "download_failed": ref,
                        "underlying": str(e),
                    },
                ) from e
            except Exception as e:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "lora_download_failed",
                        "phase": "download",
                        "evict_completed": evict_completed,
                        "download_completed": download_completed,
                        "download_failed": ref,
                        "underlying": str(e),
                    },
                ) from e
            now = datetime.now().isoformat()
            _inventory[ref] = {
                "ref": ref,
                "filename": spec.filename,
                "size_bytes": actual_bytes,
                "loras_dir_path": path,
                "downloaded_at_local": now,
                "last_used_at_local": now,
                "adapter_name": f"lora_pending_{ref}",
            }
            download_completed.append(ref)

        try:
            await _reload_pipeline_loras(req.target_refs)
        except RuntimeError as e:
            msg = str(e).lower()
            if "out of memory" in msg or "oom" in msg:
                dropped = [r for r in req.target_refs if r not in previous_refs]
                for ref in dropped:
                    _inventory.pop(ref, None)
                    dropped_spec = req.download_specs.get(ref)
                    if dropped_spec is not None:
                        try:
                            (LORAS_DIR / dropped_spec.filename).unlink(missing_ok=True)
                        except OSError:
                            pass
                await _reload_pipeline_loras(previous_refs)
                return SetStackResponse(
                    inventory=_inventory_snapshot(),
                    free_bytes=_disk_free_bytes(LORAS_DIR),
                    swap_rejected=SwapRejectedDetails(
                        reason="vram_oom", target_refs_dropped=dropped
                    ),
                )
            raise

        return SetStackResponse(
            inventory=_inventory_snapshot(),
            free_bytes=_disk_free_bytes(LORAS_DIR),
            swap_rejected=None,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
