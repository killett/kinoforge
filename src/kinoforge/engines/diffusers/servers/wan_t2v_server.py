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
from pydantic import (  # noqa: E402
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from kinoforge.engines.diffusers.servers._video_io import write_mp4  # noqa: E402

_log = logging.getLogger("kinoforge.diffusers.wan_t2v_server")
# Wire root logging so module _log.info/warning calls actually appear
# in bootstrap.log (uvicorn configures its own logger but not ours).
# Idempotent: basicConfig is a no-op when root already has handlers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

MODEL_ID: str = os.environ.get("WAN_MODEL_ID", "Wan-AI/Wan2.2-T2V-A14B-Diffusers")
ARTIFACT_DIR: Path = Path(
    os.environ.get("KINOFORGE_ARTIFACT_DIR", "/workspace/artifacts")
)
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


# ---------------------------------------------------------------------------
# P2 — pipeline arity detection + per-transformer LoRA routing.
#
# See docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md
# §3 (`_resolve_transformer` single dispatch point) and §5.2.
# ---------------------------------------------------------------------------


class BranchAutoNotAllowedOnMoE(Exception):
    """Raised when /lora/set_stack receives ``branch="auto"`` on a MoE pipe.

    HTTP 400 surface body:
    ``{"reason": "branch_auto_disallowed_on_moe", ...}``. Spec §6.1.
    """

    def __init__(self, arity: int) -> None:
        """Capture pipeline arity for the HTTP 400 surface body."""
        super().__init__(f"pipeline has {arity} transformers; branch=auto disallowed")
        self.arity = arity


class BranchUnsupportedOnSingleTransformer(Exception):
    """Raised on explicit branch against a non-MoE pipeline.

    HTTP 400 surface body:
    ``{"reason": "branch_unsupported_single_transformer", ...}``.
    """

    def __init__(self, branch: str, arity: int) -> None:
        """Capture branch + arity for the HTTP 400 surface body."""
        super().__init__(
            f"pipeline has {arity} transformer(s); branch={branch} not applicable"
        )
        self.branch = branch
        self.arity = arity


class BranchUnknown(Exception):
    """Defensive — Pydantic Literal should make this unreachable from HTTP."""

    def __init__(self, branch: str) -> None:
        """Capture the off-Literal branch value for the 500 surface body."""
        super().__init__(f"unknown branch value: {branch!r}")
        self.branch = branch


def _detect_moe_arity(pipe_obj: Any) -> int:  # noqa: ANN401
    """Count ``transformer*`` attrs on the pipeline.

    Returns 1 for non-MoE (Wan 2.1, etc.), 2 for Wan 2.2 dual-transformer,
    N for any future N-expert pipeline. Generalizes the routing decision
    without a hardcoded list of stage names.
    """
    return sum(
        1
        for attr in dir(pipe_obj)
        if attr == "transformer" or attr.startswith("transformer_")
    )


# Module-level arity cache populated during ``_load_pipeline`` before
# ``ready.set()``. Tests monkeypatch this directly. Default ``1`` so a
# Wan-2.1-style pipeline routes correctly even if the cold-boot path
# forgets to refresh it (defensive — a Wan 2.2 boot path that forgets
# this would surface as an attribute miss on ``transformer_2``).
_pipe_arity: int = 1


def _resolve_transformer(pipe_obj: Any, branch: str) -> Any:  # noqa: ANN401
    """Map ``(pipe_obj, branch)`` to the target transformer attribute.

    Single dispatch point — every LoRA-load call site (``/lora/set_stack``
    handler, cold-boot loop, VRAM-OOM rollback) routes through this
    helper. No branch-aware duck typing scattered elsewhere.

    Raises:
        BranchAutoNotAllowedOnMoE: ``branch="auto"`` on a MoE pipe.
        BranchUnsupportedOnSingleTransformer: explicit branch on a
            single-transformer pipe.
        BranchUnknown: off-Literal value reached the resolver
            (defensive — Pydantic should reject these earlier).
    """
    arity = _pipe_arity
    if arity == 1:
        if branch == "auto":
            return pipe_obj.transformer
        raise BranchUnsupportedOnSingleTransformer(branch=branch, arity=arity)
    if branch == "auto":
        raise BranchAutoNotAllowedOnMoE(arity=arity)
    if branch == "high_noise":
        return pipe_obj.transformer
    if branch == "low_noise":
        return pipe_obj.transformer_2
    raise BranchUnknown(branch=branch)


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
    # P1 (2026-06-21): per-adapter set_adapters weight active on the
    # pod. ``None`` for pre-P1 entries (never activated under the new
    # set_adapters(adapter_weights=) path).
    last_strength: float | None = None


class SwapRejectedDetails(BaseModel):
    """Why a /lora/set_stack call could not be honored as requested."""

    reason: str
    target_refs_dropped: list[str]


class LoraTarget(BaseModel):
    """One entry in ``/lora/set_stack`` target list.

    Schema-equivalent to :class:`kinoforge.core.lora.LoraEntry` but
    defined in the server module so the server has no import-time
    dependency on ``kinoforge.core.lora`` (server runs on the pod with
    a minimal dependency set). The lockstep invariant is locked by
    ``tests/test_lora_schema_parity.py``.

    See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §6.3
    and docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md §2.3.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    branch: Literal["high_noise", "low_noise", "auto"] = Field(default="auto")

    @field_validator("branch", mode="before")
    @classmethod
    def _normalize_branch_alias(cls, v: Any) -> Any:  # noqa: ANN401
        """Mirror of ``LoraEntry._normalize_branch_alias`` in core/lora.py.

        Parity is load-bearing — ``tests/test_lora_schema_parity.py``
        asserts both classes normalize identically. DO NOT diverge.
        """
        if v == "h":
            return "high_noise"
        if v == "l":
            return "low_noise"
        return v


class SetStackRequest(BaseModel):
    """Declarative target LoRA stack for the pod.

    Order of ``target`` defines pipeline adapter ordering. Every ref in
    ``target`` that is not already in the pod's inventory must have a
    matching entry in ``download_specs``.

    Each :class:`LoraTarget` carries its own ``strength`` which is
    plumbed to ``set_adapters(adapter_weights=...)`` server-side
    (P1, 2026-06-21).

    Migration: ``model_validator(mode="before")`` auto-promotes legacy
    ``target_refs: list[str]`` payloads (every promoted entry gets
    ``strength=1.0``) during a one-window transition. Removed in the
    release after every in-flight pod has rolled to a P1+ image. See
    spec §12.10 for removal criteria.
    """

    model_config = ConfigDict(extra="forbid")

    target: list[LoraTarget]
    download_specs: dict[str, ArtifactDownloadSpec]

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_target_refs(cls, data: Any) -> Any:  # noqa: ANN401
        """Auto-migrate legacy ``target_refs: list[str]`` payloads.

        Both keys present in the same request is a client bug — refuse
        rather than guess intent.
        """
        if not isinstance(data, dict):
            return data
        has_legacy = "target_refs" in data
        has_new = "target" in data
        if has_legacy and has_new:
            raise ValueError(
                "set_stack request carries BOTH legacy `target_refs` AND "
                "new `target` keys; specify exactly one"
            )
        if has_legacy:
            data["target"] = [{"ref": r, "strength": 1.0} for r in data["target_refs"]]
            del data["target_refs"]
        return data


class SetStackResponse(BaseModel):
    """Post-swap pod inventory + free disk + optional rejection details."""

    inventory: list[LoraInventoryEntry]
    free_bytes: int
    swap_rejected: SwapRejectedDetails | None = None


def _snapshot_inventory_as_targets() -> list[LoraTarget]:
    """Return the current inventory as an ordered ``LoraTarget`` list.

    Used by ``set_stack``'s VRAM-OOM rollback path: snapshots both refs
    AND ``last_strength`` values so the rollback restores the full prior
    state. Missing ``last_strength`` (pre-P1 entry) defaults to 1.0.
    """
    return [
        LoraTarget(ref=v["ref"], strength=v.get("last_strength") or 1.0)
        for v in _inventory.values()
    ]


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

    Test seam: when ``KINOFORGE_DIFFUSERS_LOAD_STUB`` env is set to a
    dotted path (``pkg.mod.callable``), imports + calls that callable
    instead of ``WanPipeline.from_pretrained``. The Tier-1 local CPU
    smoke uses this to swap in a faithful in-memory stub that
    exercises the LoRA-swap HTTP contract without CUDA.
    """
    import importlib

    stub_path = os.environ.get("KINOFORGE_DIFFUSERS_LOAD_STUB", "")
    if stub_path:
        try:
            module_name, _, attr = stub_path.rpartition(".")
            if not module_name:
                raise ImportError(f"invalid dotted path: {stub_path!r}")
            mod = importlib.import_module(module_name)
            return getattr(mod, attr)()
        except (ImportError, AttributeError) as exc:
            raise ImportError(
                f"KINOFORGE_DIFFUSERS_LOAD_STUB={stub_path!r}: {exc}"
            ) from exc

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
    # Civitai (and other Cloudflare-fronted vendors) 403 the default
    # Python-urllib UA. Inject kinoforge-pod-download UA unless the
    # caller's spec.headers already pins one explicitly. Same class
    # of fix as src/kinoforge/sources/civitai (commit 53a1e6e).
    download_headers = {"User-Agent": "kinoforge-pod-download/0.1", **spec.headers}
    req = urllib.request.Request(spec.url, headers=download_headers)  # noqa: S310 — vendor-resolved URL
    bytes_written = 0
    # 600s timeout guards against indefinite hangs on vendor stalls;
    # urlopen's default is socket._GLOBAL_DEFAULT_TIMEOUT (None) which
    # blocks forever and burns the smoke's wall-clock + budget.
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as out:  # noqa: S310
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


def _replace_adapter_stack(target: list[LoraTarget]) -> None:
    """Replace the active pipeline adapter stack with ``target`` in order.

    Calls ``unload_lora_weights()`` first to clear any active adapters,
    then re-loads each target ref as ``lora_{i}``, then ``set_adapters``
    with paired ``adapter_weights=[t.strength for t in target]``. Empty
    ``target`` → unload only.

    Persists ``last_strength`` onto each inventory entry so the warm-
    attach matcher's same-refs / different-strength path observes the
    current state.

    Synchronous: callers must wrap in ``asyncio.to_thread(...)`` when
    invoked from an async FastAPI handler. ``pipe.load_lora_weights``
    blocks on disk IO + CUDA work and would otherwise stall the event
    loop, causing /health probes to time out and the RunPod edge proxy
    to return "Waiting for service to respond" (HTTP 502).
    """
    pipe.unload_lora_weights()
    if not target:
        return
    names: list[str] = []
    weights: list[float] = []
    for i, t in enumerate(target):
        entry = _inventory[t.ref]
        name = f"lora_{i}"
        pipe.load_lora_weights(entry["loras_dir_path"], adapter_name=name)
        names.append(name)
        weights.append(t.strength)
        entry["adapter_name"] = name
        entry["last_strength"] = t.strength
    pipe.set_adapters(names, adapter_weights=weights)


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
        adapter_weights: list[float] = []
        for i, (ref, spec) in enumerate(initial_lora_stack):
            try:
                path, actual_bytes = _download_one(spec, LORAS_DIR)
            except Exception as e:
                raise RuntimeError(f"failed to download LoRA {ref}: {e}") from e
            adapter_name = f"lora_{i}"
            pipe_obj.load_lora_weights(path, adapter_name=adapter_name)
            adapter_names.append(adapter_name)
            # P1 (2026-06-21): cold-boot defaults strength=1.0 until the
            # KINOFORGE_INITIAL_LORA_STACK_JSON env shape is extended to
            # carry strength (deferred to Task 8 / DiffusersEngine
            # integration). Inventory carries last_strength so the
            # matcher reads consistent state.
            adapter_weights.append(1.0)
            now = datetime.now().isoformat()
            _inventory[ref] = {
                "ref": ref,
                "filename": spec.filename,
                "size_bytes": actual_bytes,
                "loras_dir_path": path,
                "downloaded_at_local": now,
                "last_used_at_local": now,
                "adapter_name": adapter_name,
                "last_strength": 1.0,
            }
        if adapter_names:
            pipe_obj.set_adapters(adapter_names, adapter_weights=adapter_weights)
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
    """Apply ``req.target`` as the pod's active LoRA stack.

    Idempotent in the no-op case (target == current). On a partial-overlap
    request the handler downloads only the new refs, optionally evicting
    LRU losers first if free disk is insufficient, then reloads the
    pipeline so ``set_adapters`` matches ``req.target`` order.

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
        # P1 shim: compute refs once; Task 4 rewrites _reload_pipeline_loras
        # to take LoraTarget directly so strength reaches set_adapters.
        target_refs_list = [t.ref for t in req.target]
        target_set = set(target_refs_list)
        current_set = set(_inventory.keys())
        mandatory_evict = current_set - target_set
        to_download_refs = [r for r in target_refs_list if r not in current_set]

        initial_free = _disk_free_bytes(LORAS_DIR)
        target_dl_bytes = sum(
            (req.download_specs[r].size_hint or 0) for r in to_download_refs
        )
        mandatory_freed = sum(_inventory[r]["size_bytes"] for r in mandatory_evict)
        # Snapshot pre-swap state for VRAM-OOM rollback (P1: refs AND
        # strengths so rollback is fully reversible).
        previous_state = _snapshot_inventory_as_targets()
        previous_refs = [t.ref for t in previous_state]

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
        _log.info(
            "set_stack handler: target=%s evict=%s download=%s",
            target_refs_list,
            list(mandatory_evict),
            to_download_refs,
        )
        for ref in to_download_refs:
            spec = req.download_specs[ref]
            _log.info(
                "set_stack download starting: ref=%s url=%s filename=%s size_hint=%s",
                ref,
                spec.url[:80],
                spec.filename,
                spec.size_hint,
            )
            try:
                # asyncio.to_thread: _download_one is sync urllib +
                # blocking file IO. Running it inline blocks the FastAPI
                # event loop for the duration of the download, causing
                # /health requests to time out and RunPod's edge proxy
                # to return "Waiting for service to respond" (HTTP 502)
                # even though uvicorn is alive. Offloading to a thread
                # keeps the event loop responsive.
                path, actual_bytes = await asyncio.to_thread(
                    _download_one, spec, LORAS_DIR
                )
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
                # Log before raising so the bootstrap sidecar log carries
                # the failure cause (raised HTTPException only travels in
                # the response body which the harness's HTTPError catch
                # path discards by default).
                _log.warning("set_stack download failed for ref=%s: %r", ref, e)
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
            await asyncio.to_thread(_replace_adapter_stack, req.target)
        except (RuntimeError, ValueError) as e:
            msg = str(e).lower()
            is_oom = "out of memory" in msg or "oom" in msg
            is_value = isinstance(e, ValueError)
            if not (is_oom or is_value):
                raise
            dropped = [r for r in target_refs_list if r not in previous_refs]
            for ref in dropped:
                _inventory.pop(ref, None)
                dropped_spec = req.download_specs.get(ref)
                if dropped_spec is not None:
                    try:
                        (LORAS_DIR / dropped_spec.filename).unlink(missing_ok=True)
                    except OSError:
                        pass
            try:
                await asyncio.to_thread(_replace_adapter_stack, previous_state)
            except Exception as rb_err:  # noqa: BLE001
                # Rollback ITSELF failed — pod state unknown. Surface
                # explicitly so the orchestrator destroys + cold-boots
                # rather than trusting an inventory we can't validate.
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "rollback_failed",
                        "phase": "rollback",
                        "rollback_failed": True,
                        "underlying": str(e),
                        "rollback_error": str(rb_err),
                    },
                ) from rb_err
            return SetStackResponse(
                inventory=_inventory_snapshot(),
                free_bytes=_disk_free_bytes(LORAS_DIR),
                swap_rejected=SwapRejectedDetails(
                    reason="vram_oom" if is_oom else "set_adapters_value_error",
                    target_refs_dropped=dropped,
                ),
            )

        return SetStackResponse(
            inventory=_inventory_snapshot(),
            free_bytes=_disk_free_bytes(LORAS_DIR),
            swap_rejected=None,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
