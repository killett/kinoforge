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
import gc
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

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


# --- T11: in-process LRU model registry -----------------------------------
#
# Multiple pipelines (Wan T2V, SeedVR2) co-resident on one pod's GPU. The
# registry tracks which models are on which device + their last-used
# timestamp; ``_ensure_on_gpu`` evicts LRU CUDA-resident models to CPU
# when headroom for a target drops below ``_HEADROOM_MARGIN_BYTES``.
#
# Hard floor: when a target alone exceeds total GPU capacity minus the
# headroom margin, ``VRAMEvictionFailed`` is raised — surfaced as 503 by
# the FastAPI handler.


class LoadedModel(TypedDict):
    """Registry entry describing one loaded pipeline + its placement."""

    name: str
    pipe: Any
    vram_bytes: int
    last_used_monotonic: float
    on_device: Literal["cuda", "cpu", "disk"]


_LOADED: dict[str, LoadedModel] = {}
_REGISTRY_LOCK = asyncio.Lock()
_HEADROOM_MARGIN_BYTES = (
    int(os.environ.get("KINOFORGE_HEADROOM_MARGIN_GB", "2")) * 1024**3
)


def _load_model_to_gpu(name: str) -> Any:  # noqa: ANN401 — diffusers/SeedVR2 pipe
    """Engine-specific loader dispatched on name prefix.

    The single seam where ``wan_t2v_server`` knows which loader to call
    for which prefix. SeedVR2 lazily imports its runtime so the upstream
    package isn't required for module import.
    """
    if name.startswith("wan-t2v-"):
        return _diffusers_load()
    if name.startswith("seedvr2-"):
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        # Slug: "seedvr2-{variant}-{precision}" → variant + precision tail.
        parts = name.split("-")
        variant, precision = parts[-2], parts[-1]
        return SeedVR2Runtime(
            weights_dir=Path("/workspace/models/seedvr2"),
            variant=variant.upper(),  # type: ignore[arg-type]
            precision=precision,  # type: ignore[arg-type]
        )
    raise ValueError(f"unknown model name {name!r}; no loader registered")


async def _ensure_on_gpu(name: str) -> LoadedModel:
    """Ensure ``name`` is on CUDA with sufficient headroom.

    LRU CPU eviction is opportunistic; hard-floor refusal happens when the
    target's ``vram_bytes`` exceeds GPU capacity minus the headroom margin.
    """
    from kinoforge.core.errors import VRAMEvictionFailed  # noqa: F401

    async with _REGISTRY_LOCK:
        entry = _LOADED.get(name)
        if entry is not None and entry["on_device"] == "cuda":
            entry["last_used_monotonic"] = time.monotonic()
            return entry

        if entry is None:
            new_pipe = _load_model_to_gpu(name)
            entry = LoadedModel(
                name=name,
                pipe=new_pipe,
                vram_bytes=getattr(new_pipe, "vram_bytes", 0),
                last_used_monotonic=time.monotonic(),
                on_device="cuda",
            )
            _LOADED[name] = entry
        else:
            entry["pipe"].to("cuda")
            entry["on_device"] = "cuda"
            entry["last_used_monotonic"] = time.monotonic()

        await _enforce_headroom(name)
        return entry


async def _enforce_headroom(target_name: str) -> None:
    """Evict LRU CUDA models to CPU until ``target_name`` has headroom.

    Raises VRAMEvictionFailed when the target alone exceeds capacity OR
    when every other CUDA + CPU model has been evicted and headroom is
    still insufficient.
    """
    import torch

    from kinoforge.core.errors import VRAMEvictionFailed

    free, total = torch.cuda.mem_get_info()
    target = _LOADED[target_name]

    if target["vram_bytes"] > total - _HEADROOM_MARGIN_BYTES:
        raise VRAMEvictionFailed(
            model=target_name,
            reason=(
                f"target exceeds GPU capacity: {target['vram_bytes']} bytes "
                f"> {total - _HEADROOM_MARGIN_BYTES} (total={total}, "
                f"margin={_HEADROOM_MARGIN_BYTES})"
            ),
        )

    while free < _HEADROOM_MARGIN_BYTES:
        victims = [
            n
            for n, e in _LOADED.items()
            if e["on_device"] == "cuda" and n != target_name
        ]
        if not victims:
            cpu_victims = [n for n, e in _LOADED.items() if e["on_device"] == "cpu"]
            if not cpu_victims:
                raise VRAMEvictionFailed(
                    model=target_name,
                    reason="exhausted eviction targets with insufficient headroom",
                )
            evict = min(cpu_victims, key=lambda n: _LOADED[n]["last_used_monotonic"])
            _LOADED[evict]["pipe"] = None
            _LOADED[evict]["on_device"] = "disk"
            gc.collect()
            torch.cuda.empty_cache()
            free, _ = torch.cuda.mem_get_info()
            continue

        evict = min(victims, key=lambda n: _LOADED[n]["last_used_monotonic"])
        _LOADED[evict]["pipe"].to("cpu")
        _LOADED[evict]["on_device"] = "cpu"
        gc.collect()
        torch.cuda.empty_cache()
        free, _ = torch.cuda.mem_get_info()


# LoRA-flexible warm-reuse: pod-side inventory of loaded LoRA weights.
# P2 (2026-06-22): keyed by composite ``(ref, branch)`` so the same ref
# can co-exist in two transformer branches on a Wan-2.2-style MoE pipe
# (Q6 Option 1 — spec §3.2). Pre-P2 was ``dict[str, ...]`` keyed by ref.
# Each entry value carries ``"branch"`` alongside the existing fields so
# the rollback snapshot + matcher have the routing instruction without a
# second lookup.
# Populated cold-boot in _load_pipeline; mutated by /lora/set_stack.
_inventory: dict[tuple[str, str], dict[str, Any]] = {}

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


class VRAMRollbackFailure(Exception):
    """Raised when the VRAM-OOM rollback re-load itself fails.

    Distinct from the in-band swap exception so the HTTP handler can
    map this specifically to a ``rollback_failed`` 500 body (with
    ``rollback_failed: true``) and the orchestrator destroys the pod
    instead of trusting an inventory it can't validate.
    """


_TRANSFORMER_ATTR_PAT = re.compile(r"^transformer(?:_\d+)?$")


def _detect_moe_arity(pipe_obj: Any) -> int:  # noqa: ANN401
    """Count non-None ``transformer*`` slots on the pipeline.

    Returns 1 for non-MoE (Wan 2.1 — ``transformer`` populated,
    ``transformer_2`` is None on the class), 2 for Wan 2.2 dual-
    transformer (both populated), N for any future N-expert pipeline.

    Diffusers ``WanLoraLoaderMixin`` declares
    ``_lora_loadable_modules = ["transformer", "transformer_2"]`` as
    its canonical loadable surface — that list is consulted first.
    Each module is then probed via ``getattr`` and counted only if the
    actual attribute value is not ``None``. This sidesteps two over-
    count failure modes the Tier-3 live fire (2026-06-23) surfaced:

      1. Wan 2.1 ``WanPipeline`` carries ``transformer_2 = None`` as a
         class default (so the slot can be populated by future MoE
         subclasses). The naive ``startswith`` / regex-match-only
         scan saw arity=2 and rejected every ``branch="auto"`` request
         with ``branch_auto_disallowed_on_moe``.
      2. Same scan also matched ``transformer_name`` (a string
         constant on the loader mixin) and reported arity=3.

    Fallback for test stubs that don't declare ``_lora_loadable_modules``:
    pattern-match attribute names (``transformer`` exact /
    ``transformer_<digits>``) and require the value to be not None.
    """
    modules = getattr(pipe_obj, "_lora_loadable_modules", None)
    if modules:
        return sum(
            1
            for name in modules
            if _TRANSFORMER_ATTR_PAT.match(name)
            and getattr(pipe_obj, name, None) is not None
        )
    return sum(
        1
        for attr in dir(pipe_obj)
        if _TRANSFORMER_ATTR_PAT.match(attr)
        and getattr(pipe_obj, attr, None) is not None
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


_BRANCH_SHORT: dict[str, str] = {
    "high_noise": "h",
    "low_noise": "l",
    "auto": "a",
}


def _adapter_name(position: int, branch: str) -> str:
    """Build a unique adapter name from ``(position, branch)``.

    Position prefix preserves activation order; branch suffix avoids
    collisions when the same ref is loaded into both transformer branches
    (Q6 Option 1 composite identity). Returns ``"lora_{i}_{h|l|a}"``.
    """
    return f"lora_{position}_{_BRANCH_SHORT[branch]}"


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
    # P2 (2026-06-22): per-LoRA routing branch. ``"auto"`` for pre-P2
    # entries + every entry on a single-transformer pipe (Wan 2.1);
    # ``"high_noise"`` / ``"low_noise"`` for explicit MoE routing on
    # Wan 2.2. The orchestrator's matcher reads this field as part of
    # the ``(ref, strength, branch)`` tuple comparison.
    branch: str = "auto"


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

    Used by ``set_stack``'s VRAM-OOM rollback path: snapshots refs,
    ``last_strength``, AND ``branch`` (P2 §6.4) so the rollback restores
    the full prior state including per-transformer routing. Missing
    ``last_strength`` (pre-P1 entry) defaults to 1.0. Missing ``branch``
    (pre-P2 entry) defaults to ``"auto"``.
    """
    return [
        LoraTarget(
            ref=v["ref"],
            strength=v.get("last_strength") or 1.0,
            branch=v.get("branch", "auto"),
        )
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
    candidates: set[tuple[str, str]],
    inventory: dict[tuple[str, str], dict[str, Any]],
    need: int,
) -> list[tuple[str, str]] | None:
    """Return ``(ref, branch)`` keys to evict in LRU order, until ≥ ``need``.

    Args:
        candidates: Composite keys eligible for eviction (i.e. not in
            target stack).
        inventory: Current ``_inventory`` snapshot (P2 composite-key
            shape).
        need: Bytes that must be freed. ``<= 0`` → no eviction needed.

    Returns:
        List of ``(ref, branch)`` keys in LRU-ascending order, or
        ``None`` if even evicting every candidate would not free
        ``need`` bytes. Returns ``[]`` when ``need <= 0``.
    """
    if need <= 0:
        return []
    ordered = sorted(
        (key for key in candidates if key in inventory),
        key=lambda k: inventory[k]["last_used_at_local"],
    )
    freed = 0
    plan: list[tuple[str, str]] = []
    for key in ordered:
        plan.append(key)
        freed += inventory[key]["size_bytes"]
        if freed >= need:
            return plan
    return None


async def _evict_one(ref: str, branch: str) -> None:
    """Unload one LoRA from the pipeline + remove its file + drop inventory.

    P2 (2026-06-22): takes composite ``(ref, branch)`` key so the same
    ref can be evicted out of one branch while staying loaded in the
    other (Q6 Option 1 composite identity).

    File-aware unlink (2026-06-23, swap-gap fix): the on-disk
    safetensors file is shared across every ``(ref, *)`` inventory row;
    only unlink it after the LAST surviving sibling is popped. Without
    this guard, a same-ref branch swap evicts ``(ref, old_branch)``,
    unlinks the file, then ``_replace_adapter_stack`` tries to load
    ``(ref, new_branch)`` from a now-missing path.

    Best-effort: filesystem unlink errors are swallowed because the
    inventory is the source of truth for future swap decisions; a leaked
    file gets cleaned up by the next disk-pressure eviction or by the
    reaper.
    """
    key = (ref, branch)
    entry = _inventory.get(key)
    if entry is None:
        return
    adapter = entry["adapter_name"]
    if hasattr(pipe, "delete_adapters"):
        # diffusers' LoraBaseMixin.delete_adapters auto-iterates
        # ``_lora_loadable_modules`` (=transformer + transformer_2 on Wan
        # 2.2) and no-ops on adapter names that don't exist in a given
        # transformer's peft_config — Task 0 Q3. No per-transformer
        # dispatch needed at our level.
        pipe.delete_adapters([adapter])
    file_path = entry["loras_dir_path"]
    _inventory.pop(key, None)
    sibling_survives = any(other_ref == ref for other_ref, _ in _inventory)
    if not sibling_survives:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            pass


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
    # P2 Task 6: pre-load validation gate. Walk every entry through
    # `_resolve_transformer` BEFORE any state mutation so an invalid
    # branch leaves inventory + pipeline untouched.
    resolved: list[tuple[LoraTarget, Any, str]] = []
    for t in target:
        target_transformer = _resolve_transformer(pipe, t.branch)
        target_attr = (
            "transformer_2"
            if target_transformer is getattr(pipe, "transformer_2", None)
            else "transformer"
        )
        resolved.append((t, target_transformer, target_attr))

    pipe.unload_lora_weights()
    if not target:
        return

    # Per-transformer activation buckets (Task 0 Q2 — peft raises on
    # unknown adapter names when the pipe-level set_adapters helper passes
    # the full name list to each transformer, so we activate per-
    # transformer with only the names that actually landed there).
    per_transformer_names: dict[str, list[str]] = {}
    per_transformer_weights: dict[str, list[float]] = {}

    for i, (t, _target_transformer, target_attr) in enumerate(resolved):
        entry = _inventory[(t.ref, t.branch)]
        name = _adapter_name(i, t.branch)
        # Task 0 Q1 LOCKED: boolean ``load_into_transformer_2`` kwarg on
        # WanLoraLoaderMixin.load_lora_weights (diffusers v0.36
        # lora_pipeline.py:4078).
        pipe.load_lora_weights(
            entry["loras_dir_path"],
            adapter_name=name,
            load_into_transformer_2=(target_attr == "transformer_2"),
        )
        per_transformer_names.setdefault(target_attr, []).append(name)
        per_transformer_weights.setdefault(target_attr, []).append(t.strength)
        entry["adapter_name"] = name
        entry["last_strength"] = t.strength
        entry["branch"] = t.branch

    for attr, names in per_transformer_names.items():
        model = getattr(pipe, attr, None)
        if model is None or not names:
            continue
        model.set_adapters(names, per_transformer_weights[attr])


def _normalize_initial_stack_entry(entry: Any) -> dict[str, Any]:  # noqa: ANN401
    """Normalize one cold-boot stack entry into canonical dict form.

    Canonical (P2): ``{"ref": str, "download_spec": dict|ArtifactDownloadSpec,
    "strength": float, "branch": str}``.

    Legacy (pre-P2): ``(ref, ArtifactDownloadSpec)`` tuple auto-promoted
    with ``strength=1.0, branch="auto"`` so a pod cfg pre-dating the dict
    shape keeps booting through the same code path.
    """
    if isinstance(entry, dict):
        return {
            "ref": entry["ref"],
            "download_spec": entry["download_spec"],
            "strength": float(entry.get("strength", 1.0)),
            "branch": entry.get("branch", "auto"),
        }
    ref, spec = entry  # legacy tuple — raises TypeError if 2-arity mismatch
    return {"ref": ref, "download_spec": spec, "strength": 1.0, "branch": "auto"}


def _load_pipeline(
    initial_lora_stack: list[Any] | None = None,
) -> Any:  # noqa: ANN401 — diffusers.WanPipeline has no public TypeAlias.
    """Load the Wan pipeline + optionally cold-boot a LoRA stack.

    Args:
        initial_lora_stack: Optional list of cold-boot stack entries.
            Canonical dict form
            ``{"ref": str, "download_spec": ..., "strength": float, "branch": str}``
            (P2). Legacy tuple form ``(ref, ArtifactDownloadSpec)`` is
            auto-promoted to ``strength=1.0, branch="auto"``. Order matters —
            adapter names are assigned positionally via
            :func:`_adapter_name`.

    Returns:
        The constructed pipeline with any initial LoRAs already attached.

    Raises:
        BranchAutoNotAllowedOnMoE: An entry carries ``branch="auto"`` and
            the pipeline is multi-transformer (Wan 2.2). Server NEVER
            reports ready in this case — orchestrator treats the pod as
            failed.
        BranchUnsupportedOnSingleTransformer: An entry carries an explicit
            ``h``/``l`` branch and the pipeline is single-transformer
            (Wan 2.1).
        RuntimeError: A LoRA download itself failed.
    """
    global _pipe_arity
    pipe_obj = _diffusers_load()
    # P2: refresh module-level arity cache so every load site routes
    # through the same arity decision. Must happen BEFORE the validation
    # pass below.
    _pipe_arity = _detect_moe_arity(pipe_obj)

    if not initial_lora_stack:
        return pipe_obj

    normalized = [_normalize_initial_stack_entry(e) for e in initial_lora_stack]

    # Pre-load validation gate (mirrors _replace_adapter_stack — single
    # source of truth in _resolve_transformer). Raise BEFORE downloading
    # any LoRA bytes so a misconfigured cfg fails fast instead of burning
    # disk + bandwidth on a stack the pod cannot serve.
    for entry in normalized:
        _resolve_transformer(pipe_obj, entry["branch"])

    per_transformer_names: dict[str, list[str]] = {}
    per_transformer_weights: dict[str, list[float]] = {}
    for i, entry in enumerate(normalized):
        ref = entry["ref"]
        branch = entry["branch"]
        strength = entry["strength"]
        raw_spec = entry["download_spec"]
        spec = (
            raw_spec
            if isinstance(raw_spec, ArtifactDownloadSpec)
            else ArtifactDownloadSpec.model_validate(raw_spec)
        )
        try:
            path, actual_bytes = _download_one(spec, LORAS_DIR)
        except Exception as e:
            raise RuntimeError(f"failed to download LoRA {ref}: {e}") from e
        adapter_name = _adapter_name(i, branch)
        # Task 0 Q1 LOCKED: boolean kwarg on diffusers WanLoraLoaderMixin.
        target_transformer = _resolve_transformer(pipe_obj, branch)
        target_attr = (
            "transformer_2"
            if target_transformer is getattr(pipe_obj, "transformer_2", None)
            else "transformer"
        )
        pipe_obj.load_lora_weights(
            path,
            adapter_name=adapter_name,
            load_into_transformer_2=(target_attr == "transformer_2"),
        )
        per_transformer_names.setdefault(target_attr, []).append(adapter_name)
        per_transformer_weights.setdefault(target_attr, []).append(strength)
        now = datetime.now().isoformat()
        _inventory[(ref, branch)] = {
            "ref": ref,
            "filename": spec.filename,
            "size_bytes": actual_bytes,
            "loras_dir_path": path,
            "downloaded_at_local": now,
            "last_used_at_local": now,
            "adapter_name": adapter_name,
            "last_strength": strength,
            "branch": branch,
        }

    # Per-transformer activation (Task 0 Q2 — see _replace_adapter_stack
    # for the same pattern).
    for attr, names in per_transformer_names.items():
        model = getattr(pipe_obj, attr, None)
        if model is None or not names:
            continue
        model.set_adapters(names, per_transformer_weights[attr])

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
        # P2: env file may carry either the canonical dict shape
        # (``{"ref":..., "download_spec":..., "strength":..., "branch":...}``)
        # or the legacy ``[ref, {spec}]`` tuple shape; ``_load_pipeline``
        # normalizes both into the same internal form.
        _log.info("startup: cold-boot LoRA stack size=%d", len(raw))
        pipe = _load_pipeline(initial_lora_stack=raw)
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
        # P2 (2026-06-22): inventory is keyed by composite (ref, branch);
        # so are every diff/eviction set. The download_specs map stays
        # keyed by ref (one download serves multiple branches of the same
        # ref) — we dedup downloads by ref but track inventory entries
        # by composite key.
        target_keys_list = [(t.ref, t.branch) for t in req.target]
        target_keys = set(target_keys_list)

        # Swap-gap fix (2026-06-23): seed pending inventory entries for
        # every target (ref, branch) whose ref already has any (ref, *)
        # entry on disk, BEFORE computing mandatory_evict. This anchors
        # the on-disk file via a surviving sibling so the file-aware
        # unlink in `_evict_one` does NOT delete artifacts the new
        # branches still need. See
        # docs/superpowers/specs/2026-06-23-p2-swap-gap-design.md §3.3.
        on_disk_by_ref: dict[str, dict[str, Any]] = {}
        for (ref, _br), entry in _inventory.items():
            on_disk_by_ref.setdefault(ref, entry)
        for tref, tbranch in target_keys_list:
            if (tref, tbranch) in _inventory:
                continue
            source = on_disk_by_ref.get(tref)
            if source is None:
                continue
            now = datetime.now().isoformat()
            _inventory[(tref, tbranch)] = {
                "ref": tref,
                "filename": source["filename"],
                "size_bytes": source["size_bytes"],
                "loras_dir_path": source["loras_dir_path"],
                "downloaded_at_local": source["downloaded_at_local"],
                "last_used_at_local": now,
                "adapter_name": f"lora_pending_{tref}_{_BRANCH_SHORT[tbranch]}",
                "branch": tbranch,
            }

        current_keys = set(_inventory.keys())
        mandatory_evict = current_keys - target_keys
        already_downloaded_refs = {ref for (ref, _br) in current_keys}
        to_download_refs: list[str] = []
        _seen_dl: set[str] = set()
        for ref, _br in target_keys_list:
            if ref in already_downloaded_refs or ref in _seen_dl:
                continue
            to_download_refs.append(ref)
            _seen_dl.add(ref)

        initial_free = _disk_free_bytes(LORAS_DIR)
        target_dl_bytes = sum(
            (req.download_specs[r].size_hint or 0) for r in to_download_refs
        )
        # mandatory_freed only counts bytes that are actually reclaimable
        # — i.e. evicted (ref, branch) entries whose ref will NOT survive
        # in any other inventory key (target-seeded sibling or current
        # non-evicted key). Without this guard, a same-ref branch swap
        # would double-count the shared file's size as freed even though
        # `_evict_one`'s file-aware unlink correctly keeps it on disk.
        post_evict_keys = (current_keys - mandatory_evict) | target_keys
        mandatory_freed = sum(
            _inventory[k]["size_bytes"]
            for k in mandatory_evict
            if not any(other_ref == k[0] for other_ref, _ in post_evict_keys)
        )
        # Snapshot pre-swap state for VRAM-OOM rollback (P1: refs AND
        # strengths; P2: AND branch — _snapshot_inventory_as_targets emits
        # full LoraTarget triples so rollback is fully reversible).
        previous_state = _snapshot_inventory_as_targets()
        previous_keys = {(t.ref, t.branch) for t in previous_state}

        for key in mandatory_evict:
            await _evict_one(key[0], key[1])

        post_mandatory_free = initial_free + mandatory_freed
        evict_completed: list[str] = [ref for (ref, _br) in mandatory_evict]
        if target_dl_bytes > post_mandatory_free:
            picked = _pick_lru_evict(
                set(_inventory.keys()) - target_keys,
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
            for key in picked:
                await _evict_one(key[0], key[1])
                evict_completed.append(key[0])

        download_completed: list[str] = []
        _log.info(
            "set_stack handler: target=%s evict=%s download=%s",
            target_keys_list,
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
            # P2: write one inventory entry per (ref, branch) that the
            # target stack asks for. ``branch`` is stamped with the
            # target's value; ``_replace_adapter_stack`` will overwrite
            # ``adapter_name`` + ``last_strength`` + ``branch`` once the
            # actual load runs.
            for tref, tbranch in target_keys_list:
                if tref != ref:
                    continue
                if (tref, tbranch) in _inventory:
                    continue
                _inventory[(tref, tbranch)] = {
                    "ref": tref,
                    "filename": spec.filename,
                    "size_bytes": actual_bytes,
                    "loras_dir_path": path,
                    "downloaded_at_local": now,
                    "last_used_at_local": now,
                    "adapter_name": f"lora_pending_{tref}_{_BRANCH_SHORT[tbranch]}",
                    "branch": tbranch,
                }
            download_completed.append(ref)

        try:
            await asyncio.to_thread(_replace_adapter_stack, req.target)
        except BranchAutoNotAllowedOnMoE as e:
            # Pre-load gate atomic-reject. _replace_adapter_stack raised
            # BEFORE any unload/load fired, so inventory + pipeline are
            # untouched and no rollback is needed.
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "branch_routing",
                    "reason": "branch_auto_disallowed_on_moe",
                    "arity": e.arity,
                },
            ) from e
        except BranchUnsupportedOnSingleTransformer as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "branch_routing",
                    "reason": "branch_unsupported_single_transformer",
                    "branch": e.branch,
                    "arity": e.arity,
                },
            ) from e
        except BranchUnknown as e:
            # Defensive — Pydantic Literal should make this unreachable.
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "branch_routing",
                    "reason": "branch_unknown",
                    "branch": e.branch,
                },
            ) from e
        except (RuntimeError, ValueError) as e:
            msg = str(e).lower()
            is_oom = "out of memory" in msg or "oom" in msg
            is_value = isinstance(e, ValueError)
            if not (is_oom or is_value):
                raise
            dropped_keys = [k for k in target_keys_list if k not in previous_keys]
            dropped = [ref for (ref, _br) in dropped_keys]
            for key in dropped_keys:
                _inventory.pop(key, None)
            # Files are keyed by ref (one download serves multiple branch
            # entries). Only unlink each ref's file once, after every
            # branch entry that referenced it has been popped.
            dropped_refs_unique = {ref for (ref, _br) in dropped_keys}
            for ref in dropped_refs_unique:
                if any(k[0] == ref for k in _inventory):
                    continue  # another branch still references the file
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


# --- T12: /upscale + /upscale/status/{id} ---------------------------------
#
# Co-resident with /generate so SeedVR2 upscale and Wan T2V generation
# share one process and one model registry (the T11 _LOADED map).
# Heavy CUDA / download / probe calls go through asyncio.to_thread per
# the wan_server_async_blocking rule: synchronous work in `async def`
# handlers blocks the event loop → /health hangs → RunPod proxy 502s.


class SeedVR2Params(BaseModel):
    """Engine-specific overrides for a SeedVR2 upscale request."""

    variant: Literal["3B", "7B"] = "3B"
    precision: Literal["fp8", "fp16"] = "fp8"
    tile_size: int | None = None
    steps: int | None = None


class UpscaleRequest(BaseModel):
    """JSON body for ``POST /upscale``.

    ``engine`` is a plain ``str`` (not ``Literal``) so future drop-in
    upscalers (e.g. FlashVSR) extend the dispatch table inside the
    handler without touching this schema.
    """

    source_url: str
    source_filename: str
    scale: str
    engine: str
    seedvr2: SeedVR2Params | None = None
    job_id: str | None = None


_upscale_lock: asyncio.Lock = asyncio.Lock()
_upscale_jobs: dict[str, dict[str, Any]] = {}


def _download_to_local_temp(source_url: str, source_filename: str) -> Path:
    """Fetch ``source_url`` to a local mp4 keyed by ``source_filename``.

    Handles ``file://`` (local copy) and ``http(s)://`` (urllib stream).
    Files land in ``ARTIFACT_DIR`` so the post-upscale FileResponse
    serving path (``/artifacts/{filename}``) can also reach the input
    if the caller wants to compare frames side-by-side.
    """
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    target = ARTIFACT_DIR / source_filename
    if source_url.startswith("file://"):
        src = Path(source_url[len("file://") :])
        shutil.copyfile(src, target)
        return target
    req = urllib.request.Request(  # noqa: S310 — caller-resolved URL
        source_url, headers={"User-Agent": "kinoforge-pod-upscale/0.1"}
    )
    with urllib.request.urlopen(req, timeout=600) as resp, target.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out)
    return target


def _sha256_file(p: Path) -> str:
    """Stream-hash ``p`` with sha256."""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _probe_resolution(p: Path) -> tuple[int, int]:
    """Return ``(width, height)`` for ``p`` via ffprobe.

    Returns ``(0, 0)`` if ffprobe is unavailable so a probe failure
    does not poison the whole result block. The caller's ledger writes
    a literal ``[0, 0]`` which is unambiguous in evidence files.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(p),
    ]
    try:
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            return (0, 0)
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return (int(stream["width"]), int(stream["height"]))
    except (FileNotFoundError, json.JSONDecodeError, KeyError, IndexError):
        return (0, 0)


@app.post("/upscale")
async def upscale_handler(req: UpscaleRequest) -> dict[str, str]:
    """Enqueue an upscale job; return ``{"job_id": ...}``.

    Engine dispatch reads ``req.engine``; v1 only handles ``"seedvr2"``.
    Unknown engines fail fast at submit time with 400 so the caller
    does not burn a warm-pod attach cycle on a job destined for an
    async error.
    """
    if req.engine != "seedvr2":
        raise HTTPException(status_code=400, detail=f"unsupported engine: {req.engine}")
    job_id = req.job_id or f"u-{uuid.uuid4().hex}"
    _upscale_jobs[job_id] = {
        "state": "queued",
        "progress": 0.0,
        "result": None,
        "error": None,
    }
    asyncio.create_task(_run_upscale_job(job_id, req))
    return {"job_id": job_id}


async def _run_upscale_job(job_id: str, req: UpscaleRequest) -> None:
    """Run one upscale under ``_upscale_lock``; mutate ``_upscale_jobs[job_id]``."""
    from kinoforge.core.scale_target import ScaleTarget

    async with _upscale_lock:
        try:
            _upscale_jobs[job_id]["state"] = "running"
            variant = (req.seedvr2.variant if req.seedvr2 else "3B").lower()
            precision = req.seedvr2.precision if req.seedvr2 else "fp8"
            model_name = f"seedvr2-{variant}-{precision}"
            entry = await _ensure_on_gpu(model_name)

            local = await asyncio.to_thread(
                _download_to_local_temp, req.source_url, req.source_filename
            )
            scale = ScaleTarget.parse(req.scale)
            params = req.seedvr2.model_dump() if req.seedvr2 else {}

            out_path = await asyncio.to_thread(
                entry["pipe"].upscale, local, scale, params
            )
            out_path = Path(out_path)
            sha = await asyncio.to_thread(_sha256_file, out_path)
            in_res = await asyncio.to_thread(_probe_resolution, local)
            out_res = await asyncio.to_thread(_probe_resolution, out_path)

            # Assign result + progress BEFORE flipping state to "done"
            # so a poller that catches state=="done" is guaranteed to
            # observe a populated result block (no read-mid-write race).
            _upscale_jobs[job_id]["result"] = {
                "filename": out_path.name,
                "sha256": sha,
                "size": out_path.stat().st_size,
                "input_resolution": list(in_res),
                "output_resolution": list(out_res),
                "engine_meta": {},
            }
            _upscale_jobs[job_id]["progress"] = 1.0
            _upscale_jobs[job_id]["state"] = "done"
        except Exception as exc:  # noqa: BLE001 — surface any failure to caller
            _upscale_jobs[job_id]["error"] = str(exc)
            _upscale_jobs[job_id]["state"] = "error"


@app.get("/upscale/status/{job_id}")
def upscale_status_handler(job_id: str) -> dict[str, Any]:
    """Return current state of ``job_id``; 404 if unknown."""
    payload = _upscale_jobs.get(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    return payload


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
