"""FastAPI inference server for Wan 2.2 T2V-A14B.

Runs on the GPU pod. Exposes the DiffusersBackend HTTP contract:

  GET  /health                  -> {"ready": bool, "model": str}
  POST /generate                -> {"job_id": str}
  GET  /status/{job_id}         -> {"status": ..., ...}
  GET  /artifacts/{filename}    -> MP4 bytes (added in Task 6)

Model loaded once at startup, persists across requests.
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

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from kinoforge.engines.diffusers.servers._video_io import write_mp4

_log = logging.getLogger("kinoforge.diffusers.wan_t2v_server")

MODEL_ID: str = os.environ.get("WAN_MODEL_ID", "Wan-AI/Wan2.2-T2V-A14B")
ARTIFACT_DIR: Path = Path("/workspace/artifacts")

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


def _load_pipeline() -> Any:  # noqa: ANN401 — diffusers.WanPipeline has no public TypeAlias.
    """Construct and return the WanPipeline.

    Separated out so tests can patch this seam without importing
    diffusers (which would otherwise pull torch + CUDA at test time).
    """
    import torch
    from diffusers import WanPipeline

    pipe_obj = WanPipeline.from_pretrained(MODEL_ID, torch_dtype=torch.bfloat16)
    return pipe_obj.to("cuda")


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
    """Load the pipeline, spawn worker, mark server ready."""
    global pipe, _worker_thread
    _log.info("startup: loading pipeline %s", MODEL_ID)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
