"""FastAPI text-generation server for ``kinoforge text``.

Runs on the GPU pod. Loads ONE open-weight checkpoint with ``transformers`` and
serves chat completions over the ``submit_and_poll`` contract
(``engines/_pod_http.py``)::

  GET  /health                 -> {"ready", "model", "supported_modes", "capabilities", ...}
  GET  /util                   -> the five UtilSnapshot fields
  PUT  /upload                 -> {"path", "size", "sha256"}   (PNG / JPEG only)
  POST /text                   -> {"job_id"}
  GET  /text/status/{job_id}   -> {"state", "result"?, "error"?}

Built on the ``minimax_h3_server`` skeleton (queue, one worker thread, startup
ordering with ``ready.set()`` LAST) and none of its body. The completion
travels INLINE in ``result`` — no artifact file, no ``/artifacts`` route.

Modes are DERIVED at load, never declared: a checkpoint whose processor carries
an ``image_processor`` loads through ``AutoModelForImageTextToText`` and serves
``t2t`` + ``it2t``; anything else loads through ``AutoModelForCausalLM`` and
serves ``t2t`` only. ``/health`` reports that set so the controller can compare
it with what the config declared (design §4.2).

Import discipline: stdlib, fastapi, pydantic and the two sibling helpers at
module scope. ``torch`` and ``transformers`` are pod-only — the controller env
that unit-tests this module through ``_LOADER`` has neither installed — so
both are imported ONLY inside functions. ``PIL`` IS installed in the
controller env (unlike the other two), but it stays function-local in
``_open_images`` for the same import-discipline reason, not because the
controller lacks it: a module-level ``import PIL`` would be harmless here but
is still the shape U66 warns against, so the rule is applied uniformly rather
than relying on which pod-only packages happen to also be present locally.
(U66: a module-level pod-only import is invisible to the controller's suite
and kills every pod at boot.)

``KINOFORGE_UPLOAD_DIR`` is a test seam — the suite points it at a
``tmp_path`` — with a pod-local default (``/tmp/kf-uploads``) when unset; no
provision fragment exports it today. ``KINOFORGE_MAX_UPLOAD_MB`` defaults to
64 (not the Wan server's 2048): this route only ever receives a single still
image per request, never video, so the ceiling is deliberately far tighter.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from kinoforge.engines.diffusers.servers._upload import receive_upload  # noqa: E402
from kinoforge.engines.diffusers.servers._util_stats import read_gpu_stats  # noqa: E402

_log = logging.getLogger("kinoforge.diffusers.text_server")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

#: Exported by TransformersTextEngine.render_provision; no default on purpose —
#: a missing export is a boot fault, not a silent fallback to some model.
MODEL_ID: str = os.environ.get("KINOFORGE_TEXT_MODEL_ID", "")
PORT: int = int(os.environ.get("KINOFORGE_TEXT_PORT", "8000"))
#: Reported in /health so a run is reproducible without reading this file.
DEFAULT_MAX_NEW_TOKENS = 256
_UPLOAD_DIR: Path = Path(os.environ.get("KINOFORGE_UPLOAD_DIR", "/tmp/kf-uploads"))  # noqa: S108 — pod-local scratch
_UPLOAD_CONTENT_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg"})
_UPLOAD_MAX_BYTES = int(os.environ.get("KINOFORGE_MAX_UPLOAD_MB", "64")) * 1024 * 1024
TEXT_ONLY_MODES: tuple[str, ...] = ("t2t",)
VISION_MODES: tuple[str, ...] = ("t2t", "it2t")


@dataclass
class LoadedModel:
    """What the loader hands the server.

    Attributes:
        model: The generation model (``generate()`` + ``device``).
        processor: Tokenizer or processor (``apply_chat_template``, ``__call__``, ``decode``).
        supported_modes: Derived from the processor — see :func:`modes_for`.
        model_id: The checkpoint id, echoed in ``/health`` and every result.
    """

    model: Any
    processor: Any
    supported_modes: tuple[str, ...]
    model_id: str


def modes_for(processor: Any) -> tuple[str, ...]:  # noqa: ANN401 — tokenizer or processor
    """Derive the served modes from the processor.

    ``AutoProcessor`` returns a tokenizer for a text-only checkpoint and a
    processor carrying an ``image_processor`` for a vision-language one; that
    attribute IS the derivation.
    """
    accepts_images = getattr(processor, "image_processor", None) is not None
    return VISION_MODES if accepts_images else TEXT_ONLY_MODES


def _load_transformers(model_id: str) -> LoadedModel:
    """Load *model_id* with transformers onto the CUDA device in bf16."""
    import torch
    from transformers import (
        AutoModelForCausalLM,
        AutoModelForImageTextToText,
        AutoProcessor,
    )

    processor = AutoProcessor.from_pretrained(model_id)
    modes = modes_for(processor)
    cls = AutoModelForImageTextToText if "it2t" in modes else AutoModelForCausalLM
    model = cls.from_pretrained(model_id, dtype=torch.bfloat16, device_map="cuda")
    model.eval()
    return LoadedModel(
        model=model, processor=processor, supported_modes=modes, model_id=model_id
    )


#: Test seam: the suite replaces this with a loader returning fakes; the pod
#: never touches it. A module attribute rather than an env-var import path so
#: test_pod_embed_closure's dynamic-import list stays untouched.
_LOADER: Callable[[str], LoadedModel] = _load_transformers


@dataclass
class JobState:
    """One queued/running/finished completion."""

    job_id: str
    status: str  # queued | running | done | error
    prompt: str
    system: str | None
    images: list[str]
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class TextRequest(BaseModel):
    """``POST /text`` body. ``extra="forbid"`` so a typo'd knob is a 422, not a no-op."""

    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1)
    system: str | None = None
    images: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


loaded: LoadedModel | None = None
ready = threading.Event()
jobs: dict[str, JobState] = {}
_q: queue.Queue[str] = queue.Queue()
_worker_thread: threading.Thread | None = None
app = FastAPI(title="kinoforge text server")


def _torch_build() -> dict[str, Any]:
    """Torch/CUDA facts for ``/health``; ``{}`` where torch is absent (tests)."""
    try:
        import torch
    except ImportError:
        return {}
    return {"version": torch.__version__, "cuda": torch.version.cuda}


def _build_messages(
    prompt: str, system: str | None, images: list[Any]
) -> list[dict[str, Any]]:
    """Assemble the chat.

    The user content is a plain string for ``t2t`` (text tokenizers' templates
    expect one and would render a list's repr) and an interleaved list —
    every image, then the text — for ``it2t``.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    content: Any
    if images:
        content = [{"type": "image", "image": im} for im in images]
        content.append({"type": "text", "text": prompt})
    else:
        content = prompt
    messages.append({"role": "user", "content": content})
    return messages


def _open_images(paths: list[str]) -> list[Any]:
    """Open uploaded images as RGB PIL images (PIL imported here, pod-only)."""
    from PIL import Image

    return [Image.open(p).convert("RGB") for p in paths]


def _validate_image_paths(paths: list[str]) -> None:
    """400 unless every path is an existing file under the upload dir."""
    root = _UPLOAD_DIR.resolve()
    for p in paths:
        target = Path(p).resolve()
        try:
            target.relative_to(root)
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"image path {p!r} is outside the upload dir"
            ) from e
        if not target.is_file():
            raise HTTPException(
                status_code=400, detail=f"image path {p!r} does not exist on the pod"
            )


def _to_device(inputs: Any, device: Any) -> Any:  # noqa: ANN401 — BatchEncoding or a test dict
    return inputs.to(device) if device is not None and hasattr(inputs, "to") else inputs


def _generate(model: Any, inputs: Any, gen_kwargs: dict[str, Any]) -> Any:  # noqa: ANN401
    """``model.generate`` under ``torch.inference_mode`` when torch is present."""
    try:
        import torch
    except ImportError:  # the controller test env; fakes need no grad guard
        return model.generate(**inputs, **gen_kwargs)
    with torch.inference_mode():
        return model.generate(**inputs, **gen_kwargs)


def _run_job(state: JobState) -> None:
    """Run one completion; mutate *state* with the outcome. Never raises."""
    assert loaded is not None  # noqa: S101 — /text refuses before ready
    state.status = "running"
    state.started_at = time.time()
    try:
        params = dict(state.params)
        chat_kwargs = dict(params.pop("chat_template_kwargs", None) or {})
        gen_kwargs: dict[str, Any] = {
            "max_new_tokens": DEFAULT_MAX_NEW_TOKENS,
            **params,
        }
        images = _open_images(state.images) if state.images else []
        messages = _build_messages(state.prompt, state.system, images)
        proc = loaded.processor
        text = proc.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False, **chat_kwargs
        )
        inputs = (
            proc(text=[text], images=images, return_tensors="pt")
            if images
            else proc(text=[text], return_tensors="pt")
        )
        inputs = _to_device(inputs, getattr(loaded.model, "device", None))
        prompt_tokens = int(inputs["input_ids"].shape[1])
        out = _generate(loaded.model, inputs, gen_kwargs)
        new_ids = out[0][prompt_tokens:]
        completion_tokens = int(len(new_ids))
        max_new = int(gen_kwargs["max_new_tokens"])
        state.result = {
            "text": proc.decode(new_ids, skip_special_tokens=True),
            "finish_reason": "length" if completion_tokens >= max_new else "stop",
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "model": loaded.model_id,
            "max_new_tokens": max_new,
        }
        state.status = "done"
        _log.info(
            "job %s done: %d prompt + %d new tokens",
            state.job_id,
            prompt_tokens,
            completion_tokens,
        )
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
    """Load the model, spawn the worker, flip ``ready`` LAST."""
    global loaded, _worker_thread
    if not MODEL_ID:
        raise RuntimeError(
            "KINOFORGE_TEXT_MODEL_ID is not set; TransformersTextEngine.render_provision exports it"
        )
    _log.info("startup: loading %s", MODEL_ID)
    t0 = time.monotonic()
    loaded = _LOADER(MODEL_ID)
    _log.info(
        "startup: loaded in %.1f s; modes=%s",
        time.monotonic() - t0,
        list(loaded.supported_modes),
    )
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    _worker_thread.start()
    ready.set()


@app.get("/health")
def health() -> dict[str, Any]:
    """Readiness, model identity, DERIVED modes, and the closed capability vocabulary."""
    is_ready = ready.is_set() and loaded is not None
    return {
        "ready": is_ready,
        "model": loaded.model_id if loaded is not None else MODEL_ID,
        "supported_modes": list(loaded.supported_modes) if loaded is not None else [],
        "capabilities": ["text", "upload"] if is_ready else [],
        "default_max_new_tokens": DEFAULT_MAX_NEW_TOKENS,
        "torch": _torch_build(),
    }


@app.get("/util")
def util() -> dict[str, Any]:
    """Per-tick GPU/CPU/mem stats; sync def so the NVML read runs in the threadpool."""
    return read_gpu_stats()


@app.put("/upload")
async def upload_handler(request: Request) -> dict[str, Any]:
    """PNG/JPEG upload with the shared contract; see ``_upload.receive_upload``."""
    return await receive_upload(
        request,
        upload_dir=_UPLOAD_DIR,
        content_types=_UPLOAD_CONTENT_TYPES,
        max_bytes=_UPLOAD_MAX_BYTES,
        fallback_suffix=".png",
    )


@app.post("/text")
def submit_text(req: TextRequest) -> dict[str, str]:
    """Enqueue one completion; 503 while loading, 400 on images a text-only model cannot take."""
    if not ready.is_set() or loaded is None:
        raise HTTPException(status_code=503, detail="model loading")
    if req.images and "it2t" not in loaded.supported_modes:
        raise HTTPException(
            status_code=400,
            detail=(
                f"model {loaded.model_id} serves {list(loaded.supported_modes)}; it has "
                "no image path, so a request carrying images is refused"
            ),
        )
    _validate_image_paths(req.images)
    job_id = uuid.uuid4().hex
    jobs[job_id] = JobState(
        job_id=job_id,
        status="queued",
        prompt=req.prompt,
        system=req.system,
        images=list(req.images),
        params=dict(req.params),
    )
    _q.put(job_id)
    return {"job_id": job_id}


@app.get("/text/status/{job_id}")
def text_status(job_id: str) -> dict[str, Any]:
    """``submit_and_poll`` schema: ``state`` plus ``result`` when done or ``error`` on failure."""
    state = jobs.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    out: dict[str, Any] = {"state": state.status}
    if state.status == "done" and state.result is not None:
        out["result"] = state.result
    elif state.status == "error" and state.error is not None:
        out["error"] = state.error
    return out


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)  # noqa: S104 — pod-internal bind
