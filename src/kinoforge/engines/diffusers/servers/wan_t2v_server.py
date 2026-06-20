"""FastAPI inference server for Wan 2.2 T2V-A14B.

Runs on the GPU pod. Exposes the DiffusersBackend HTTP contract:

  GET  /health                  -> {"ready": bool, "model": str}
  POST /generate                -> {"job_id": str}            (Task 5)
  GET  /status/{job_id}         -> {"status": ..., ...}        (Task 5)
  GET  /artifacts/{filename}    -> MP4 bytes                   (Task 6)

Model is loaded once at startup, persists across requests. See
``docs/superpowers/specs/2026-06-19-wan22-native-t2v-a14b-design.md``
for the full design.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI

_log = logging.getLogger("kinoforge.diffusers.wan_t2v_server")

MODEL_ID: str = os.environ.get("WAN_MODEL_ID", "Wan-AI/Wan2.2-T2V-A14B")
ARTIFACT_DIR: Path = Path("/workspace/artifacts")

app = FastAPI(title="kinoforge wan-t2v server", version="0.1.0")
ready: threading.Event = threading.Event()
pipe: Any = None  # set in _startup; opaque type to avoid diffusers import here


def _load_pipeline() -> Any:  # noqa: ANN401 — diffusers.WanPipeline has no public TypeAlias.
    """Construct and return the WanPipeline.

    Separated out so tests can patch this seam without importing
    diffusers (which would otherwise pull torch + CUDA at test time).
    """
    import torch
    from diffusers import WanPipeline

    pipe_obj = WanPipeline.from_pretrained(MODEL_ID, torch_dtype=torch.bfloat16)
    return pipe_obj.to("cuda")


@app.on_event("startup")
def _startup() -> None:
    """Load the pipeline and mark the server ready.

    Failures here propagate out of uvicorn — the pod dies, the
    orchestrator's wait_for_ready hits boot_timeout and surfaces a
    clear ProvisionTimeout. No silent retry.
    """
    global pipe
    _log.info("startup: loading pipeline %s", MODEL_ID)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    pipe = _load_pipeline()
    ready.set()
    _log.info("startup: pipeline loaded, server ready")


@app.get("/health")
def health() -> dict[str, Any]:
    """Return readiness + model identity."""
    return {"ready": ready.is_set(), "model": MODEL_ID}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
