# Design — video upscaling (engine-agnostic, SeedVR2-first)

**Date:** 2026-06-28
**Status:** Brainstorm output; pending implementation plan
**Brainstorm transcript inputs:** session of 2026-06-28 (eight clarifying questions, six approved design sections)

## 1. Problem statement and goals

Add a video upscaling capability to kinoforge that:

1. Is **engine-agnostic.** SeedVR2 ships as the v1 implementation; FlashVSR (and any future upscaler) drops in via config change only — no core API churn.
2. Runs **both ways:**
   - As a final stage of the existing video pipeline (`kinoforge generate` followed automatically by upscale when the cfg has an `upscale:` block).
   - As a **standalone** `kinoforge upscale` command operating on an already-existing video file or URI. This is the expected primary mode of use — the operator picks low-res videos worth upscaling rather than blowing compute on every clip.
3. Activates the **same warm-reuse machinery** that `kinoforge generate` already uses, so back-to-back `kinoforge upscale` invocations do not pay cold-boot per call.
4. Lays a foundation that lets a future session drop in **frame interpolation** as another pipeline stage with the same shape, sharing the same VRAM-tiering and disk-floor budget on the pod.

The default v1 model is **SeedVR2 3B FP8** (~10 GB VRAM). The 7B FP16 variant is accepted by the config schema but not part of the v1 live-smoke matrix.

## 2. Non-goals (v1)

- Frame interpolation — separate future design; the seams in this design are written *to support* it but no interp code lands now.
- `--scale 1080p` / `--scale 720p` height-targeted upscale with auto-downscale-to-fit — `ScaleTarget` parses these tokens and raises `NotYetImplementedError` when consumed. The CLI surface is final on day one; the future session fills in the height-branch arithmetic plus the swappable downscale method.
- FlashVSR concrete engine — the drop-in seam is exercised by the SeedVR2 path; actual FlashVSR install lives in a later session.
- ComfyUI-graph SeedVR2 path. The only working Wan 2.2 system in kinoforge runs Diffusers; we extend that server rather than bolt ComfyUI alongside.
- Multi-clip batch upscale (`kinoforge upscale --video a.mp4 --video b.mp4`). One clip per invocation in v1; warm-reuse amortizes the cold-boot tax across repeated single-clip calls.
- Audio mux from source on the upscaled output. SeedVR2 outputs silent video; downstream ffmpeg mux is future work.
- SeedVR2 7B FP16 live-validated. Cfg accepts it; verification is owner-driven on a beefier pod once 3B is green.

## 3. Architecture overview

Upscaling is a third primary capability tier in kinoforge, sibling to video generation and image generation. The pattern mirrors the existing two tiers exactly:

| Tier | ABC | Registry | Cfg block |
|---|---|---|---|
| Video generation | `GenerationEngine` | `register_engine` | `engine:` |
| Image generation | `ImageEngine` | `register_image_engine` | (under `keyframe:`) |
| **Video upscaling (new)** | **`UpscalerEngine`** | **`register_upscaler`** | **`upscale:`** |

### 3.1 Module layout (new files in **bold**)

```
src/kinoforge/
  core/
    interfaces.py                       (edit) UpscalerEngine ABC + UpscaleJob/
                                               UpscaleResult/ScaleTarget; add
                                               `stages` + `upscaler` +
                                               `upscaler_precision` factors to
                                               CapabilityKey
    config.py                           (edit) UpscaleConfig + SeedVR2EngineConfig;
                                               wire onto Config; capability_key()
                                               populates new factors
    registry.py                         (edit) register_upscaler / get_upscaler /
                                               upscaler_names
    errors.py                           (edit) UnsupportedScaleError,
                                               NotYetImplementedError,
                                               UpscaleFailed,
                                               VRAMEvictionFailed, StageMismatch
  **upscalers/**                        (new package)
    **__init__.py**
    **seedvr2/**
      **__init__.py**                    SeedVR2Engine + self-register
      **_runtime.py**                    Direct-Python wrapper around upstream
                                         ByteDance-Seed/SeedVR
      **_fetch_weights.py**              CLI module invoked by provision
                                         script to materialize weights
  pipeline/
    **upscale.py**                      UpscaleStage: PipelineState in,
                                         PipelineState out; reads
                                         artifacts["clip"], writes
                                         artifacts["upscaled"]
  engines/diffusers/servers/
    wan_t2v_server.py                   (edit) /upscale + /upscale/status/{id}
                                               endpoints; in-process model
                                               registry + LRU CPU eviction;
                                               /health capabilities advertise
  cli/
    _commands.py                        (edit) _cmd_upscale
    _main.py                            (edit) wire "upscale" subcommand
  _adapters.py                          (edit) import kinoforge.upscalers.seedvr2
```

### 3.2 Dependency direction

`core/` knows only the ABC + registry — no concrete upscaler imports. `upscalers/seedvr2/` imports from `core/`. `pipeline/upscale.py` calls `registry.get_upscaler(name)` and the returned engine. The Diffusers server gets a thin adapter so the same SeedVR2 inference module can be invoked over HTTP; the runtime module is itself testable without spinning up the server. FlashVSR later: drop `upscalers/flashvsr/` + self-register; zero changes elsewhere.

## 4. `UpscalerEngine` interface

### 4.1 Core types

```python
@dataclass(frozen=True)
class ScaleTarget:
    """Polymorphic scale target. v1 supports kind="factor"; kind="height"
    parses but raises NotYetImplementedError when consumed.

    Grammar:
      "2x", "4x", "1.5x"   -> ScaleTarget(kind="factor", value=2.0)
      "1080p", "720p"      -> ScaleTarget(kind="height", value=1080)
    """
    kind: Literal["factor", "height"]
    value: float

    @classmethod
    def parse(cls, raw: str) -> ScaleTarget: ...


@dataclass(frozen=True)
class UpscaleJob:
    """One unit of upscale work — engine-agnostic shape.

    No prompt, no segments, no LoRA stack — upscaling is video-in/video-out.
    """
    source: Artifact
    scale: ScaleTarget
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UpscaleResult:
    artifact: Artifact
    input_resolution: tuple[int, int]
    output_resolution: tuple[int, int]
    elapsed_s: float
    engine_meta: dict = field(default_factory=dict)


class UpscalerEngine(ABC):
    """A swappable video upscaler; owns env setup; declares supported scales."""

    name: str
    requires_compute: bool
    requires_local_weights: bool
    supported_scales: tuple[ScaleTarget, ...]

    @abstractmethod
    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None: ...

    @abstractmethod
    def upscale(
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult: ...

    @abstractmethod
    def validate_spec(self, job: UpscaleJob) -> None:
        """Raise on engine-unsupportable job. E.g. SeedVR2 3B refusing scale="3x"."""

    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Sink-filename slug (e.g. 'seedvr2-3b-fp8'). MUST NOT raise."""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Default raises; engines with remote-capable providers override."""
        raise NotImplementedError(...)
```

Rationale: a separate ABC (not `GenerationEngine` extension) keeps the surface minimal. Upscaling has no prompt, no segments, no Splitter, no LoRA stack, no `ModelProfile` discovery — forcing those through `GenerationEngine` would leave inapplicable fields in `GenerationJob.spec`.

### 4.2 Registry (additive)

`core/registry.py` gains a parallel namespace mirroring `register_image_engine`:

```python
_UPSCALERS: dict[str, Callable[[], UpscalerEngine]] = {}

def register_upscaler(name: str, factory: Callable[[], UpscalerEngine]) -> None: ...
def get_upscaler(name: str) -> Callable[[], UpscalerEngine]: ...
def upscaler_names() -> list[str]: ...
```

### 4.3 Config block

```python
class SeedVR2EngineConfig(BaseModel):
    """SeedVR2-specific block; required when upscale.engine == "seedvr2".

    weights_ref defaults to None; when None, SeedVR2Engine derives the ref
    from (variant, precision) via a model_validator (e.g. variant="3B" +
    precision="fp8" -> "hf:ByteDance-Seed/SeedVR2-3B"). Explicit override
    is supported for users who fork the upstream weights repo.
    """
    variant: Literal["3B", "7B"] = "3B"
    precision: Literal["fp8", "fp16"] = "fp8"
    tile_size: int | None = None
    steps: int | None = None
    weights_ref: str | None = None


class UpscaleConfig(BaseModel):
    """Top-level upscale block. Presence activates in-pipeline UpscaleStage.

    Attributes:
        engine: Upscaler name (registry key). v1 supports "seedvr2".
        scale: ScaleTarget grammar string ("2x"|"4x"|"1080p"|...).
        seedvr2: Required when engine == "seedvr2"; ignored otherwise.
    """
    engine: str
    scale: str
    seedvr2: SeedVR2EngineConfig | None = None


class Config(BaseModel):
    # ... existing fields ...
    upscale: UpscaleConfig | None = None
```

The `upscale:` block is the single opt-in. `kinoforge upscale` cfgs always carry it; `kinoforge generate` cfgs carry it only when in-pipeline upscale is wanted; pure-generate cfgs omit it.

## 5. CLI surface

### 5.1 New `kinoforge upscale` subcommand

```
kinoforge upscale --video <uri-or-path> --config <cfg> [flags...]

Required:
  --video <uri-or-path>   Source video. Accepts local path, file://, gs://, s3://
                          (resolved via existing ArtifactStore).
  --config <cfg>          YAML cfg with `upscale:` block.

Optional:
  --scale <target>        Override cfg.upscale.scale ("2x", "4x", "1080p"...).
  --no-reuse              Pod auto-destroys at end of run (mirrors generate).
  --attach-pod <id>       Attach an existing warm pod (mirrors generate).
  --force-attach          Bypass matcher verdicts (mirrors generate).
  --output <dir>          Output sink directory override.
  --ephemeral             Skip successful-generations.md logging.
  --dry-run               Print resolved plan; no provision, no spend.
```

Warm-reuse helpers (`_scan_warm_candidates`, `_resolve_attach_pod`, ephemeral-index reads, mutual-exclusion guards between `--no-reuse` and `--attach-pod`) are reused from `_cmd_generate` without modification — `_cmd_upscale` is a thin shell that calls the same helpers with an upscale cfg.

### 5.2 No `--upscale` flag on `kinoforge generate`

Pipeline activation is purely cfg-driven (`upscale:` block present in cfg → `UpscaleStage` appended after `GenerateClipStage`). One activation path; no flag-vs-cfg drift to debug later.

### 5.3 `--scale` CLI override is the only flag that touches engine params

Everything else (variant, precision, tile_size, steps) is cfg-only. Matches kinoforge's existing ergonomic — model tuning is cfg, run shaping is CLI.

### 5.4 Validation timing

Parse `--scale` (or `cfg.upscale.scale`) via `ScaleTarget.parse()` at CLI startup, *before* pod provision. `ScaleTarget(kind="height")` raises `NotYetImplementedError("--scale 1080p deferred to a later session; use --scale Nx for v1")`. Mistakes caught pre-spend.

### 5.5 Docs

- `kinoforge upscale --help` documents the standalone path.
- `docs/warm-reuse.md` gains an "Upscale-only pods" subsection.
- `examples/configs/upscale-seedvr2-3b.yaml` ships as a tracked example.

## 6. Server endpoints and runtime

### 6.1 New endpoints on existing FastAPI

`wan_t2v_server.py` gains two endpoints. Existing routes unchanged.

```
POST /upscale                  -> {"job_id": str}
GET  /upscale/status/{job_id}  -> {"state": "queued"|"running"|"done"|"error",
                                   "progress": float,
                                   "result": {...} | null,
                                   "error": str | null}
GET  /artifacts/{filename}     (existing — also serves upscaled mp4s)
GET  /health                   (existing — payload extended; see 6.4)
```

`UpscaleRequest` body:

```python
class UpscaleRequest(BaseModel):
    source_url: str
    source_filename: str
    scale: str
    engine: str   # registry key; v1 server dispatches "seedvr2" only.
                  # FlashVSR drop-in extends the server's engine-dispatch table
                  # without changing this schema.
    seedvr2: SeedVR2Params | None = None
    job_id: str | None = None
```

Handler concurrency: serialized by a fresh `_upscale_lock` mirroring `_set_stack_lock`. Same `asyncio.to_thread` wrap rule applies (per project memory `feedback_wan_server_async_blocking`) — every synchronous CUDA call inside `async def` handlers must wrap in `asyncio.to_thread` to keep `/health` responsive and avoid RunPod-proxy 502s.

### 6.2 In-process model registry + LRU CPU eviction

```python
class LoadedModel(TypedDict):
    name: str
    pipe: Any
    vram_bytes: int
    last_used_monotonic: float
    on_device: Literal["cuda", "cpu", "disk"]

_LOADED: dict[str, LoadedModel] = {}
_REGISTRY_LOCK = asyncio.Lock()
```

`_ensure_on_gpu(name) -> LoadedModel` contract:

1. If model not in `_LOADED`: load from disk → CPU → GPU; record `vram_bytes`.
2. Otherwise: refresh `last_used_monotonic`.
3. If post-load `torch.cuda.mem_get_info()` headroom < `HEADROOM_MARGIN_BYTES` (default 2 GB):
   - Evict LRU other models from CUDA → CPU via `pipe.to("cpu")` until headroom met.
   - If still insufficient after evicting every other CUDA-resident model: evict CPU-resident models to disk by deleting `pipe`, then `gc.collect()` + `torch.cuda.empty_cache()`. Disk reload on next activation.
   - **Hard floor:** if the target model itself does not fit on the GPU even when it is the sole CUDA-resident pipeline (i.e. its `vram_bytes` alone exceeds available GPU memory minus the headroom margin), raise `VRAMEvictionFailed(model=name, reason="target exceeds GPU capacity")`. The server returns 503 to the caller; no infinite-loop eviction.
4. Return the activated entry.

`/generate` calls `_ensure_on_gpu("wan-t2v-...")`. `/upscale` calls `_ensure_on_gpu("seedvr2-...")`. Wan stays warm across upscale calls (and vice versa) when VRAM allows; falls back to swap when it doesn't.

Tunables exposed as server env vars:
- `KINOFORGE_HEADROOM_MARGIN_GB` (default `2`)
- `KINOFORGE_SEEDVR2_VARIANT` (default `3B`)
- `KINOFORGE_SEEDVR2_PRECISION` (default `fp8`)

### 6.3 SeedVR2 runtime: direct upstream Python

`src/kinoforge/upscalers/seedvr2/_runtime.py` is the thin import-and-call layer. The upstream SeedVR repo is **not** vendored; pod provision installs it from a pinned commit. The specific commit SHA is selected during plan-writing (most recent green commit on the upstream main at plan time) and committed alongside the provision script as a constant; the `<pinned-commit-sha>` placeholder below is a stand-in for that selection step, not a TBD in this design:

```bash
# Provision script appends:
pip install --no-build-isolation \
  "seedvr @ git+https://github.com/ByteDance-Seed/SeedVR@<pinned-commit-sha>"
python -m kinoforge.upscalers.seedvr2._fetch_weights \
  --variant 3B --precision fp8 --dest /workspace/models/seedvr2
```

`_runtime.py` shape:

```python
class SeedVR2Runtime:
    """Wraps upstream SeedVR2 inference; held inside _LOADED[name].pipe."""

    def __init__(
        self,
        weights_dir: Path,
        variant: Literal["3B", "7B"],
        precision: Literal["fp8", "fp16"],
    ):
        from seedvr.inference import SeedVR2Inferencer  # upstream
        self._inferencer = SeedVR2Inferencer.from_pretrained(
            weights_dir, variant=variant, dtype=precision
        )

    def upscale(self, video_path: Path, scale: ScaleTarget, params: dict) -> Path:
        """Returns path to upscaled mp4.
        ScaleTarget(kind="height") raises NotYetImplementedError.
        """
        ...

    def to(self, device: str) -> None:
        """LRU eviction hook — moves underlying nn.Modules to device."""
        ...
```

`SeedVR2Engine` (the public `UpscalerEngine` impl) owns:

- `requires_compute = True`, `requires_local_weights = True`.
- `supported_scales = (ScaleTarget(kind="factor", value=2.0), ScaleTarget(kind="factor", value=4.0))`. Finer factors error in `validate_spec` with `UnsupportedScaleError`.
- `provision()`: stages weights into the pod's weight directory via the existing `kinoforge.sources.huggingface` source-resolver; renders provision-script extension that installs the upstream repo.
- `upscale()`: POSTs to `/upscale`, polls `/upscale/status/{id}` (using the existing `_retry_proxy_call` helper to absorb RunPod startup-window 404/502s — see project memory `task7_comfyui_404_regression`), downloads result via `/artifacts/{filename}`. Mirrors `DiffusersBackend.submit/result`.

Justification for direct-Python over ComfyUI graph:
1. Single Python process, single CUDA allocator, single eviction policy.
2. No localhost-ComfyUI hop and no second event loop.
3. SeedVR2 weights ship on HuggingFace (`ByteDance-Seed/SeedVR2-3B`), so existing source-resolver path handles weight provisioning.

### 6.4 `/health` payload extension

Today: `{"ready": bool, "model": str}`.

After: `{"ready": bool, "model": str, "models": [{"name", "on_device", "ready"}], "capabilities": [...]}`.

The pre-existing `model` field is **retained** — it carries the primary t2v model identity for backward compatibility with any client that reads it. New fields are additive:

- `models`: full per-model state (one entry per loaded pipeline; `on_device` ∈ `"cuda"|"cpu"|"disk"`). The matcher pre-flight uses this for VRAM-budget visibility.
- `capabilities`: list of stage tags this pod can actually serve right now (e.g. `["t2v","upscale"]` for a multi-stage pod, `["upscale"]` for an upscale-only pod, `["t2v"]` for a legacy generate-only pod). Derived from which engines have been successfully provisioned, not from cfg intent — so a half-failed provision reports the partial truth rather than the optimistic cfg.

The matcher gains a `capabilities`-based pre-flight (§7.4) that uses this list to verify pod-state matches ledger intent before claiming an attach.

### 6.5 Hardware floor

| Use case | VRAM target | Reason |
|---|---|---|
| In-pipeline (Wan FP8 + SeedVR2 3B FP8 co-resident) | 48 GB | Wan ~20-24 + SeedVR2 ~10 + headroom 2 ≈ 36 GB working set |
| Upscale-only (SeedVR2 3B FP8 alone) | 24 GB | Fits 3090/A10 tier |
| Upscale-only (SeedVR2 7B FP16 alone) | 32 GB | Fits A6000/L40 tier; not in v1 live-smoke matrix |

Disk floor: Wan T2V-A14B ~30 GB + SeedVR2 3B ~6 GB + container + outputs ≈ 50-60 GB models, ≈ 100 GB total. Existing `disk_gb: int = 100` default already covers this. Future frame-interp may need 120-150 GB.

## 7. Warm-reuse: `stages` factor on `CapabilityKey`

### 7.1 CapabilityKey change with hash backward compatibility

```python
@dataclass(frozen=True)
class CapabilityKey:
    base_model: str
    loras: tuple[str, ...] = ()
    engine: str = ""
    precision: str = ""
    stages: tuple[str, ...] = ()         # NEW
    upscaler: str = ""                   # NEW
    upscaler_precision: str = ""         # NEW

    def derive(self) -> str:
        """Stable, order-sensitive sha256.

        Backward-compat invariant: when stages == () AND upscaler == ""
        AND upscaler_precision == "", derive() returns byte-identical
        output to the pre-change implementation. Implementation: only
        append the new fields to the JSON payload when any is non-default.
        """
        base = [self.base_model, list(self.loras), self.engine, self.precision]
        if self.stages or self.upscaler or self.upscaler_precision:
            base.extend([list(self.stages), self.upscaler, self.upscaler_precision])
        payload = json.dumps(base, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

This conditional-extend trick is essential: every committed ledger entry written before this change has `stages = ()`. They must keep matching a fresh `cfg.capability_key().derive()` on a generate-only cfg. The conditional extend guarantees byte-identical hash output for the legacy case while letting upscale-touching cfgs derive a different hash space they share only with each other.

`WarmAttachKey` gains the same fields with the same backward-compat conditional-extend in its derive. `LoraStack` is unchanged.

### 7.2 `Config.capability_key()` derivation

```python
def capability_key(self) -> CapabilityKey:
    stages: list[str] = []
    if self.engine is not None:
        stages.append("t2v")  # or whichever mode the engine declares
    upscaler = ""
    upscaler_precision = ""
    if self.upscale is not None:
        stages.append("upscale")
        upscaler = self.upscale.engine
        if self.upscale.seedvr2 is not None:
            upscaler_precision = (
                f"{self.upscale.seedvr2.variant.lower()}-"
                f"{self.upscale.seedvr2.precision}"
            )
    return CapabilityKey(
        base_model=_extract_base_model_ref(self),   # existing helper, unchanged
        loras=_extract_lora_refs(self),             # existing helper, unchanged
        engine=self.engine.kind if self.engine else "",
        precision=self.engine.precision if self.engine else "",
        stages=tuple(stages),
        upscaler=upscaler,
        upscaler_precision=upscaler_precision,
    )
```

| Cfg shape | `stages` | `upscaler` | Pods matched |
|---|---|---|---|
| Generate-only (legacy + new) | `()` *(empty for legacy hash compat)* | `""` | All legacy + new generate pods |
| Generate + upscale | `("t2v","upscale")` | `"seedvr2"` | Multi-stage pods only |
| Upscale-only | `("upscale",)` | `"seedvr2"` | Upscale-only pods only (primary pass) |

### 7.3 Matcher rule: subset semantics for upscale-only cfgs

A standalone `kinoforge upscale` cfg has `stages=("upscale",)`. It must also be able to attach to a multi-stage `(t2v,upscale)` pod, which is fully capable. We add a secondary matcher pass:

1. **Primary (existing):** `derive() == derive()` exact hash equality. Common-path fast match.
2. **Secondary (new, only for upscale-only cfgs):** Pod's ledger-recorded `kinoforge_stages` is a superset of cfg's `stages`, AND `upscaler` + `upscaler_precision` match. Cheap O(pods) scan; only runs when primary returns nothing AND `cfg.upscale is not None` AND `cfg.engine is None`.

`_scan_warm_candidates` gains one conditional branch. Ledger writes a new `kinoforge_stages: list[str]` field per pod (alongside existing `kinoforge_key`). Pods written before this change have no `kinoforge_stages` → secondary matcher skips them, which is correct: pre-change pods were generate-only.

### 7.4 `/health`-driven pre-flight

Matcher pre-flight reads pod `/health`'s `capabilities` before claiming a warm-attach candidate. If the candidate's `capabilities` does not cover the cfg's `stages`, matcher refuses with verdict `STAGE_MISMATCH`. Guards against ledger / pod-state drift (a pod written as multi-stage but actually serving an older build).

### 7.5 Ledger migration

None required. Existing entries lack the new fields; matcher treats them as legacy generate-only via the conditional-extend invariant. New writes include the new fields. Zero risk of orphaning existing warm pods.

## 8. Error handling

New exceptions in `core/errors.py`:

| Exception | Raised by | Caught at |
|---|---|---|
| `UnsupportedScaleError(scale, engine_name)` | `UpscalerEngine.validate_spec` | CLI; pre-spend exit 2 |
| `NotYetImplementedError(message)` | `ScaleTarget` consumer when `kind="height"` | CLI; pre-spend exit 2 |
| `UpscaleFailed(job_id, server_error)` | server `/upscale/status` returns `state="error"` | engine `.upscale()`; reraised as `KinoforgeError` |
| `VRAMEvictionFailed(model, reason)` | `_ensure_on_gpu` after exhausting eviction targets | server returns 503; engine reraises |
| `StageMismatch(want, have)` | matcher pre-flight when `/health` capabilities disagree | matcher; reverts to cold-create or emits `MISMATCH` verdict |

Each error type carries enough context (cfg path, capability key, ledger entry id where applicable) to reconstruct the offending state without session memory.

## 9. Testing strategy

Per-module unit tests live alongside source per kinoforge convention. For each, the test states the behaviour under test and a concrete failure that should kill it (test-design skill requirement).

| Test file | Behaviour under test | Failure that should kill it |
|---|---|---|
| `tests/test_scale_target.py` | `ScaleTarget.parse("2x")` → `kind="factor"`, `value=2.0` | regex anchors to `^[0-9.]+x$`; rejects `2x.0` |
| `tests/test_scale_target.py` | `ScaleTarget.parse("1080p")` → `kind="height"`, `value=1080` | accept-list mirrors common heights only |
| `tests/test_scale_target.py` | `UpscaleStage.run(state)` raises `NotYetImplementedError` on `kind="height"` | catches accidental enablement of `Np` |
| `tests/test_scale_target.py` | `SeedVR2Engine.validate_spec` rejects `ScaleTarget(kind="factor", value=3.0)` with `UnsupportedScaleError` | locks SeedVR2 to declared `supported_scales` |
| `tests/test_upscaler_registry.py` | `register_upscaler("seedvr2", factory)` + `get_upscaler("seedvr2")()` returns engine | duplicate-name registration raises (mirrors `register_engine`) |
| `tests/test_capability_key_stages.py` | `CapabilityKey(base_model="x", engine="diffusers", precision="fp8").derive()` == pre-change golden hash | byte-equal frozen golden |
| `tests/test_capability_key_stages.py` | `CapabilityKey(..., stages=("t2v","upscale"), upscaler="seedvr2", upscaler_precision="3b-fp8").derive()` != legacy hash AND stable | round-trip + golden |
| `tests/test_warm_matcher_stages.py` | upscale-only cfg matches `(t2v,upscale)` ledger entry via secondary pass | secondary path returns the multi-stage pod when primary is empty |
| `tests/test_warm_matcher_stages.py` | upscale-only cfg refuses `(t2v,)` ledger entry | superset check rejects non-upscale pods |
| `tests/test_upscale_stage.py` | `UpscaleStage.run(state)` writes `artifacts["upscaled"]` and preserves `artifacts["clip"]` | immutability + key contract |
| `tests/test_upscale_stage.py` | `UpscaleStage.run(state)` raises `KeyError` when `artifacts["clip"]` absent | order-dependency contract |
| `tests/test_cmd_upscale.py` | `kinoforge upscale --video x.mp4 --config c.yaml --no-reuse --dry-run` exits 0 | end-to-end CLI wiring; no spend |
| `tests/test_cmd_upscale.py` | `kinoforge upscale` without `--video` exits 2 | argparse contract |
| `tests/test_cmd_upscale.py` | `--no-reuse` + `--attach-pod` mutually-exclusive — exits 2 | mirrors `_cmd_generate` |
| `tests/test_lru_eviction.py` | `_ensure_on_gpu("seedvr2")` while wan on GPU + VRAM tight evicts wan to CPU; `_ensure_on_gpu("wan")` re-promotes | LRU policy correctness with fake `torch.cuda.mem_get_info` |
| `tests/test_lru_eviction.py` | repeated `_ensure_on_gpu` of same model is a no-op | no spurious churn |

### 9.1 Live smokes

Both committed RED first per project rule "commit RED scaffolds before any live spend":

1. **`tests/live/test_seedvr2_3b_fp8_upscale_smoke.py`** — `kinoforge upscale --no-reuse` against a known 480p clip from `examples/`. Asserts output dimensions == 2x input, sha256(input frame) ≠ sha256(output frame), RunPod ledger empty post-exit. Polling cadence per `Live smoke monitoring` rule: GPU/CPU/mem probe every 60-90 s.

2. **`tests/live/test_wan_then_upscale_warm_reuse_smoke.py`** — `kinoforge generate` against a multi-stage cfg, asserts both `clip` AND `upscaled` artifacts land in the output dir, pod's `/health` shows `capabilities: ["t2v","upscale"]`, a follow-up `kinoforge upscale --video <other-clip>` against the same cfg's capability key attaches the warm pod (no cold-boot).

## 10. Extensibility seams

These are locked in for v1 and exercised — *not* speculative.

1. **FlashVSR drop-in.** Add `src/kinoforge/upscalers/flashvsr/` with `register_upscaler("flashvsr", ...)`. Cfg sets `upscale.engine: flashvsr` and adds a sibling `flashvsr:` engine block. Zero changes elsewhere. Verification: one new live smoke; the rest of the surface is inherited.

2. **`--scale 1080p` activation (future session).** `ScaleTarget.parse` already returns `kind="height"`. Future work replaces the `NotYetImplementedError` raise with: probe input clip via ffprobe for current height, derive native factor via `ceil(target_height / input_height)`, run native upscale at that factor, then ffmpeg downscale to exactly `target_height`. The swappable downscale method becomes a new `cfg.upscale.downscale: Literal["lanczos","bicubic","area"] = "lanczos"`. CLI `--scale 1080p` is already wired.

3. **Frame-interp stage (future session, non-goal here).** Same pattern: new `InterpolatorEngine` ABC + `register_interpolator` + `pipeline/interpolate.py`'s `InterpolateStage` + new `interpolate:` cfg block + new stage tag `"interp"` in `CapabilityKey.stages` + `/interpolate` endpoint sharing the same `_LOADED` LRU registry. The LRU registry, `stages` factor, `/health` capabilities advertisement, and multi-stage matcher pass shipped in *this* design are exactly the foundation that work will need. No retrofit cost.

4. **VRAM-tier autotune.** Server `/health` reports total VRAM. Future work can auto-select SeedVR2 variant from headroom rather than cfg-pinning. Not in v1.

## 11. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Upstream SeedVR repo lacks stable inference module / API churn | M | Pin to a specific commit SHA in provision script. Smoke test catches regressions before live spend |
| RunPod proxy 502/404 startup window applies to new endpoints (per `task7_comfyui_404_regression`) | H | Reuse existing `_retry_proxy_call` helper on the SeedVR2 engine's submit/result; warm `/health` before first call (per `wan_server_set_stack_proxy_warmup`) |
| `CapabilityKey.derive()` change accidentally breaks existing ledger entries | M (catastrophic if hit) | Frozen golden-hash test (`tests/test_capability_key_stages.py`) is a mandatory pre-merge gate; CI fail blocks the change |
| VRAM eviction races between `/generate` and `/upscale` | M | `_REGISTRY_LOCK` serializes all transitions; tested with fake CUDA backend |
| SeedVR2 weight pull hangs first-boot (Wan has a history of this) | M | Reuse `wan_server_set_stack_proxy_warmup` pattern — warm `/health` first, recover from transient 502 via inventory convergence poll |

## 12. Open questions to be answered in the plan

None for the design layer — every question raised during brainstorm was answered before the spec was written. The implementation plan will turn each table-row test in §9 into a numbered task and sequence them per dependency.

---

**Brainstorm acceptance log:**
- Q1 engine surface: `UpscalerEngine` ABC + registry — accepted
- Q2 pod topology: same FastAPI, new endpoints — accepted
- Q3 CLI shape: `kinoforge upscale` + in-cfg pipeline opt-in — accepted
- Q4 scale grammar: polymorphic `ScaleTarget`, height parses-then-raises — accepted
- Q5 SeedVR2 runtime: direct upstream Python — accepted
- Q6 warm-reuse: `stages` factor on `CapabilityKey` — accepted
- Q7 VRAM strategy: LRU CPU eviction — accepted
- Q8 default variant: SeedVR2 3B FP8 — accepted
- Section A architecture: accepted
- Section B `UpscalerEngine` interface + cfg: accepted
- Section C CLI surface: accepted
- Section D server + runtime + VRAM: accepted
- Section E warm-reuse via `stages` factor: accepted
- Section F testing / errors / extensibility / out-of-scope: accepted
