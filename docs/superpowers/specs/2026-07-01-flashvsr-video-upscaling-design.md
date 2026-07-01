# Design — FlashVSR video upscaling (v1 default swap)

**Date:** 2026-07-01
**Status:** Approved (pre-plan)
**Supersedes:** `docs/superpowers/specs/2026-06-29-upscaler-packaging-pivot-design.md` §default engine choice
**Related:** `docs/superpowers/specs/2026-06-28-video-upscaling-design.md` (original P1 SeedVR2-first spec),
`docs/superpowers/specs/2026-06-29-upscale-pod-upload-design.md` (P3 pod-upload seam)

---

## 1. Why this exists

P2 shipped `SpandrelEngine + RealESRGAN x2 fp16` as the v1 default upscaler. Spandrel is a
per-frame image super-resolution runtime: it processes each decoded frame independently
with zero temporal-consistency layer. Applied to Wan 2.2 output, this produces visible
flicker on motion + fine texture — a materially different behavior from the original P1
ask, which specified "video upscaling, SeedVR2-first". SeedVR2 is a video-native diffusion
super-resolution model with learned temporal coherence.

The P2 pivot to spandrel was a packaging call, not a quality call: ByteDance-Seed/SeedVR
carried no `setup.py` / `pyproject.toml`; `pip install git+...` failed against every
commit; spandrel was the fastest pip-installable path to validate the `UpscalerEngine`
ABC + provisioning seam end-to-end with real spend. P2 was successful on those merits:
Blockers A/B/C all resolved; the drop-in seam is validated by two implementers
(SpandrelEngine live-GREEN; SeedVR2Engine stub gated behind `[seedvr]` extras).

This spec swaps the v1 default upscaler to **FlashVSR v1.1** (OpenImagingLab), a
streaming diffusion VSR distilled from Wan 2.1 — the same weight family as kinoforge's
existing Wan 2.2 T2V-A14B path. Rationale (per 2026-06-30 OSS video-SR survey):

- Apache-2.0 both code + weights (`hf:JunhaoZhuang/FlashVSR-v1.1`).
- Ships an actual `setup.py` (fixes the P1 blocker that killed SeedVR).
- Streaming architecture → no chunk-seam flicker on long clips.
- ~17 FPS at 768×1408 on A100 80GB → ~7 s for a 5-second clip (vs 15-20 min for SeedVR2).
- Wan 2.1 backbone → VAE + Diffusers idioms already familiar to the codebase.
- Comfortable on the A6000 48 GB / A100 80 GB pods kinoforge already provisions.

Fallback #2 (STAR, MIT) and fallback #3 (SeedVR2-3B, Apache-2.0) remain candidates
but are not implemented in this workstream.

## 2. Goals + non-goals

**Goals:**

1. New `FlashVSREngine(UpscalerEngine)` registered as `"flashvsr"`; becomes v1 default via
   `cfg.upscale.engine = "flashvsr"` on the shipped example cfgs.
2. Streaming-inference path served via `wan_t2v_server`'s existing `/upscale` +
   `/upscale/status` surface — reuse P3 file-upload + LRU + warm-reuse verbatim; zero
   changes to server plumbing beyond the model-prefix dispatch table.
3. `SpandrelEngine` stays registered as fallback (`cfg.upscale.engine = "spandrel"`) for
   tiny-VRAM or anime-only cases.
4. `SeedVR2Engine` stays dormant behind `[seedvr]` extras — no change.
5. Block-Sparse-Attention CUDA kernel compiled once at pod cold-boot via
   `render_provision` script; subsequent warm-reuse calls skip via `.so` cache on the
   RunPod `/workspace` bind-mount.
6. End-to-end multi-stage live smoke: Wan 2.2 T2V generate → FlashVSR upscale
   (720p → 1440p on 5-second clip) → single MP4 sunk to `/workspace/output/`. Warm-reuse
   confirmed across two prompts.
7. Live budget ceiling $3 total across the workstream; happy-path ~$0.65.

**Non-goals (this workstream):**

- STAR / SeedVR2 / VEnhancer integration — future spec if FlashVSR proves inadequate on
  Wan output.
- `--scale` values other than what the FlashVSR weights encode (checkpoint dictates
  supported scales; validate at cfg-time, reject others with `UnsupportedScaleError`).
- Text-prompt-guided upscale — FlashVSR has no text encoder; `job.params["prompt"]`
  ignored with a `logger.warning`.
- 4K output target — v1 targets 1440p; 4K deferred pending pod-VRAM validation on
  longer clips.
- Batch multi-clip upscale (`--video a.mp4 --video b.mp4`). One clip per invocation
  (unchanged from P2/P3).
- Deprecating spandrel — stays available; only the default moves.
- Audio mux from source on the upscaled output (deferred, same as P1 non-goal).
- Height-target scale (`--scale 1080p`) — `ScaleTarget.kind == "height"` continues to
  raise `NotYetImplementedError` in `FlashVSRRuntime.upscale`.

## 3. Architecture overview

Additive. Zero changes to core ABCs, registry, orchestrator, CLI, or server surface.
The seam validated by two implementers in P2 (spandrel + SeedVR2-stub) takes a third
real implementer. Module layout — new files in **bold**:

```
src/kinoforge/
  upscalers/
    spandrel/                       (existing, unchanged)
    seedvr2/                        (existing stub, unchanged)
    flashvsr/                       (NEW package)
      **__init__.py**               FlashVSREngine + self-register("flashvsr")
      **_engine.py**                Split-out HTTP dispatch shim (mirrors
                                    spandrel/_engine.py from P3)
      **_runtime.py**               FlashVSRRuntime (streaming diffusion loop)
      **_fetch_weights.py**         CLI: fetches 4-file bundle from
                                    hf:JunhaoZhuang/FlashVSR-v1.1
  engines/diffusers/servers/
    wan_t2v_server.py               (edit) _load_model_to_gpu prefix dispatch +
                                            _capability_for_model + optional
                                            HF_HUB_OFFLINE=1 flip post-fetch
  core/
    config.py                       (edit) FlashVSREngineConfig; UpscaleConfig.flashvsr;
                                            capability_key() populates when
                                            engine == "flashvsr"
    errors.py                       (edit) BSACompileFailed,
                                            FlashVSRWeightsIncomplete,
                                            UnsupportedGpuArch
  _adapters.py                      (edit) import kinoforge.upscalers.flashvsr

examples/configs/
  **runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml**   multi-stage: Wan 14B + FlashVSR
  **runpod-upscale-only-flashvsr.yaml**               standalone kinoforge upscale
  runpod-diffusers-wan22-t2v-a14b-spandrel.yaml       (existing; add comment header
                                                        pointing at flashvsr as v1
                                                        default)

tests/upscalers/flashvsr/          (NEW)
  test_config.py
  test_engine.py
  test_runtime.py
  test_fetch_weights.py
  test_server_dispatch.py

tests/test_adapters.py             (edit) test_flashvsr_registered_on_import
tests/test_examples.py             (edit) lockdown tests for two new cfgs
tests/live/
  **test_flashvsr_live.py**         F-single + F-multi + F-warm smokes (RED xfail
                                    until scaffolds land; unxfailed at spend time)
```

**Dependency direction unchanged.** `core/` never imports `upscalers/flashvsr/`. Server
dispatches by slug prefix. Provision composition already wires
`UpscalerEngine.render_provision` output before server exec (P2 T8 seam). Warm-reuse
LRU already handles model swap-in/swap-out.

## 4. Config surface

Config block under `cfg.upscale.flashvsr`:

```yaml
upscale:
  engine: flashvsr
  scale: 2x                        # must match FlashVSR checkpoint's native scale
  flashvsr:
    weights_bundle: "hf:JunhaoZhuang/FlashVSR-v1.1"
    precision: fp16                # fp16 | fp32; fp16 default (DMD ckpt is fp16-native)
    window_size: 24                # streaming attention window (frames); 24 = paper default
    tile_size: 0                   # 0 = whole-frame; >0 = spatial tiling for VRAM headroom
    long_video_mode: false         # true = LCSA+TCDecoder path; false = plain streaming DMD
```

### 4.1 Cfg-time validation (`FlashVSREngineConfig.__post_init__`)

- `weights_bundle` must resolve via known resolver chain (`hf:`, `civitai:`,
  `civarchive:`, `http(s)://`); else `ConfigError`.
- `precision ∈ {"fp16","fp32"}`; else `ConfigError`.
- `window_size` clamped to `[8, 64]` inclusive; out-of-range → `ConfigError` (no
  silent clamp — surfaces cfg mistakes loud).
- `tile_size` allowlist `{0, 256, 384, 512, 768}` (documented FlashVSR-safe values);
  else `ConfigError`.
- `long_video_mode == True` requires the 4-file bundle path in `_fetch_weights`
  (i.e. the fetcher's `--include-long-video 1`). Cfg lockdown test asserts this.

### 4.2 Cross-cfg validation

- `cfg.upscale.engine == "flashvsr"` + `cfg.upscale.scale.kind == "height"` →
  `ConfigError("flashvsr: height-target scale not yet supported; use factor form (2x, 4x)")`
  raised at cfg-time (fail fast; do not defer to runtime).
- Provision-side GPU allowlist enforced via `gpu_preference` on the example cfg
  (Ampere+ only — see §5.5).

## 5. Runtime + engine layers

### 5.1 Runtime — `src/kinoforge/upscalers/flashvsr/_runtime.py`

```python
class FlashVSRRuntime:
    """Streaming diffusion VSR. Loads DMD + VAE (+ optional LCSA/TCDecoder).
    Satisfies the LoadedModel contract used by the server LRU."""

    def __init__(
        self,
        weights_dir: Path,
        precision: Literal["fp16", "fp32"],
        window_size: int,
        tile_size: int,
        long_video_mode: bool,
    ) -> None:
        from flashvsr.pipeline import StreamingDMDPipeline   # lazy import
        self._dtype = torch.float16 if precision == "fp16" else torch.float32
        self._pipe = StreamingDMDPipeline.from_pretrained(
            weights_dir,
            torch_dtype=self._dtype,
            enable_lcsa=long_video_mode,
        )
        self._window = window_size
        self._tile = tile_size
        # Native scale from checkpoint. Attribute name to verify at plan-phase:
        # if StreamingDMDPipeline exposes a different attr (e.g. `.upscale_factor`),
        # adjust here. RED runtime test locks the contract.
        self._native_scale = self._pipe.scale         # 2 or 4

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        if scale.kind == "height":
            raise NotYetImplementedError(
                "flashvsr: height-target not yet wired (see spec §2 non-goals)"
            )
        if scale.value != self._native_scale:
            raise UnsupportedScaleError(
                requested=scale.value, supported=(self._native_scale,)
            )
        if params.get("prompt"):
            logger.warning(
                "flashvsr: params['prompt'] ignored — model has no text encoder"
            )
        out = video_path.with_suffix(".flashvsr.mp4")
        self._pipe.stream_upscale(
            input_path=str(video_path),
            output_path=str(out),
            window_size=self._window,
            tile=self._tile or None,
        )
        return out

    def to(self, device: str) -> None:
        self._pipe.to(device)

    @property
    def vram_bytes(self) -> int:
        # Wan 2.1 1.3B backbone fp16 ≈ 2.6 GB weights + streaming state ≈ 4-8 GB peak
        return int(8 * 1024**3)
```

### 5.2 Engine — `src/kinoforge/upscalers/flashvsr/__init__.py`

```python
class FlashVSREngine(UpscalerEngine):
    name = "flashvsr"
    requires_compute = True
    requires_local_weights = True
    supported_scales = ()          # runtime declares at load-time from checkpoint

    def render_provision(self, cfg: dict) -> RenderedProvision:
        block = cfg["upscale"]["flashvsr"]
        script = (
            # SM80+ guard (Ampere+ required for Block-Sparse-Attention)
            'set -euo pipefail\n'
            'python -c "import torch; '
            'assert torch.cuda.get_device_capability()[0] >= 8, '
            'f\'flashvsr: BSA needs SM80+, got {torch.cuda.get_device_capability()}\'" '
            '|| exit 87\n'
            # Compile-cache lives on the persistent /workspace bind-mount
            'export TORCH_EXTENSIONS_DIR=/workspace/.cache/bsa\n'
            'export MAX_JOBS=4\n'
            'mkdir -p "$TORCH_EXTENSIONS_DIR"\n'
            # BSA (nvcc compile ~5-10 min cold; hits cache on warm reuse)
            'pip install '
            '"git+https://github.com/mit-han-lab/Block-Sparse-Attention@main" '
            '--no-build-isolation --no-cache-dir\n'
            # FlashVSR itself
            'pip install '
            '"git+https://github.com/OpenImagingLab/FlashVSR@v1.1" '
            '"imageio[ffmpeg]>=2.34"\n'
            # Weights
            f'python -m kinoforge.upscalers.flashvsr._fetch_weights '
            f'--bundle {block["weights_bundle"]} '
            f'--dest /workspace/models/flashvsr '
            f'--include-long-video {"1" if block["long_video_mode"] else "0"}\n'
            # Hermetic-pod flip: reject any secondary HF download at /upscale time
            'export HF_HUB_OFFLINE=1\n'
        )
        return RenderedProvision(
            script=script,
            run_cmd=[], image="", ports=[],
            env_required=["HF_TOKEN"],
        )

    def upscale(self, instance, job, cfg, *, cancel_token=None) -> UpscaleResult:
        # HTTP dispatch identical to SpandrelEngine — POST /upscale, poll
        # /upscale/status. Server dispatches by slug prefix "flashvsr-".
        ...

    def model_identity(self, cfg: dict) -> str:
        return f"flashvsr-wan21-{cfg['upscale']['flashvsr']['precision']}"
```

### 5.3 HTTP dispatch shim — `_engine.py`

Mirrors P3's `spandrel/_engine.py` split — separates HTTP client concerns
(POST /upload, POST /upscale, poll /upscale/status, materialize result artifact)
from the engine class. Enables independent unit testing of the transport layer.

### 5.4 Weights fetch — `_fetch_weights.py`

Fetches from `hf:JunhaoZhuang/FlashVSR-v1.1` via `huggingface_hub.hf_hub_download`:

Base bundle (always fetched):
- `diffusion_pytorch_model_streaming_dmd.safetensors`
- `Wan2.1_VAE.pth`

Long-video extension (fetched only when `--include-long-video 1`):
- `LQ_proj_in.ckpt`
- `TCDecoder.ckpt`

Post-download SHA256 verification against a `weights_manifest.json` shipped in
the package (`src/kinoforge/upscalers/flashvsr/weights_manifest.json` — NEW file;
one entry per bundle file, SHA256 read at fetch time from the HF revision pinned
in `weights_bundle`). Mismatch → `FlashVSRWeightsIncomplete` raised with the
offending file name; do not attempt silent retry (surfaces upstream tampering
or corrupted CDN).

CLI shape mirrors existing `_fetch_weights` pattern (spandrel, seedvr2):

```bash
python -m kinoforge.upscalers.flashvsr._fetch_weights \
  --bundle hf:JunhaoZhuang/FlashVSR-v1.1 \
  --dest /workspace/models/flashvsr \
  --include-long-video 0
```

Note: `_fetch_weights` must not import `kinoforge.core.registry` on the pod (P2
lesson: keeps the module tree pod-safe without embedding the full kinoforge
package via the bootstrap embed-modules mechanism).

### 5.5 GPU compatibility guard

- Provision-script preamble asserts `torch.cuda.get_device_capability()[0] >= 8`.
  Exit code 87 documented in `errors.py` → surfaces as `UnsupportedGpuArch`.
- Example cfg pins `gpu_preference: ["A100 80GB", "A6000", "L40S", "A100 40GB"]` —
  excludes T4/A10/AMD Instinct (P2 lesson: offer chooser silently picked AMD
  Instinct without an explicit NVIDIA allowlist).

### 5.6 Server dispatch delta (`wan_t2v_server.py`)

- `_load_model_to_gpu` prefix table extended:
  ```python
  {"spandrel-": SpandrelRuntime,
   "flashvsr-": FlashVSRRuntime}
  ```
- `_capability_for_model`: `"flashvsr-*"` returns `"upscale"`.
- Zero other changes — `/upscale`, `/upscale/status`, `/upload`, LRU eviction,
  warm-reuse all reused verbatim from P2/P3.

### 5.7 LRU + warm-reuse integration

- `FlashVSRRuntime.vram_bytes = 8 GiB` (conservative — 2.6 GB weights + streaming
  state + tile buffers). Server LRU evicts to CPU when total VRAM budget hits
  ceiling.
- `FlashVSRRuntime.to(device)` moves the full `StreamingDMDPipeline` (backbone +
  VAE + optional LCSA/TCDecoder) as a unit. This is a normal `nn.Module`
  composite; the descriptor-unwrap fix from P3 spandrel (`9274c5c`) is NOT
  required here.
- Model slug: `flashvsr-wan21-fp16`. Three-token shape preserved; server-side
  `_load_model_to_gpu` parse contract unchanged.
- Cross-cap-key isolation: `CapabilityKey.upscaler = "flashvsr"` distinct from
  `"spandrel"` → same-pod switch between the two upscalers forces server-side
  reload (correct; LRU tracks each separately).

## 6. Error handling

New named exceptions in `src/kinoforge/core/errors.py`:

| Exception | Raised where | Recovery |
|---|---|---|
| `BSACompileFailed` | Server-side, at first `/upscale` after cold boot if `import block_sparse_attention` fails | Pod destroyed; operator sees named error via `kinoforge logs`; retry with different GPU family or fall back to `SpandrelEngine` |
| `FlashVSRWeightsIncomplete` | `_fetch_weights` post-download hash check | Retry weight fetch; if persistent → HF network or model-repo problem, surface loudly |
| `UnsupportedGpuArch` | Provision-script preamble (exit 87) | Fail-fast at pod boot; operator picks better GPU via `gpu_preference` |

Existing exceptions reused unchanged: `UnsupportedScaleError`, `NotYetImplementedError`,
`UpscaleFailed`, `VRAMEvictionFailed`, `StageMismatch`.

## 7. Testing strategy

### 7.1 Unit tests (`tests/upscalers/flashvsr/`)

Every test is RED-first (user's TDD rule). Every test docstring states (a) the specific
behavior under test, (b) a concrete bug the assertion would catch (per `test-design`
skill).

| Test file | Coverage |
|---|---|
| `test_config.py` | `FlashVSREngineConfig` validation: resolver-chain check; `precision` allowlist; `window_size` clamp; `tile_size` allowlist; `long_video_mode ↔ weight-file-set` consistency; height-target rejection at cfg-time. |
| `test_engine.py` | `FlashVSREngine.render_provision`: BSA-then-FlashVSR install order; `MAX_JOBS=4`; `TORCH_EXTENSIONS_DIR=/workspace/.cache/bsa`; SM80+ preamble with exit 87; `--include-long-video` threading; `HF_HUB_OFFLINE=1` at tail; script size ≤ 12 KB (headroom under 64 KB pod env ceiling). Also `model_identity` slug shape + HTTP dispatch shape. |
| `test_runtime.py` | Constructor lazy-imports `flashvsr`; `.to(device)` moves pipe; `.vram_bytes` returns 8 GiB; `upscale()` raises `UnsupportedScaleError` on mismatched scale; raises `NotYetImplementedError` on `kind="height"`; logs (not raises) on `params["prompt"]`. `stream_upscale` mocked. |
| `test_fetch_weights.py` | 4-file bundle with `--include-long-video 1`; 2-file bundle with `--include-long-video 0`; hash-mismatch → `FlashVSRWeightsIncomplete`; unknown-resolver rejection. |
| `test_server_dispatch.py` | `_load_model_to_gpu` dispatches `"flashvsr-wan21-fp16"` → `FlashVSRRuntime`; `_capability_for_model` returns `"upscale"` for `flashvsr-*`. Extends existing spandrel dispatch table test. |
| `tests/test_examples.py` (edit) | Lockdown: `runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml` pins `gpu_preference` NVIDIA SM80+ allowlist; `upscale.engine == "flashvsr"`; `long_video_mode: false`. Same for `runpod-upscale-only-flashvsr.yaml`. |
| `tests/test_adapters.py` (edit) | `test_flashvsr_registered_on_import`: importing `kinoforge` registers `flashvsr` under `get_upscaler`; slug appears in `upscaler_names()`. |

### 7.2 Live-smoke matrix (`tests/live/test_flashvsr_live.py`)

Single RED PyTest file, `xfail`-gated until scaffolds land, unxfailed atomically at
spend time (P2 lesson: commit scaffold before spend).

| Smoke | Cfg | Prompt | Scale | Expected VRAM | Expected wall | Verify | Budget |
|---|---|---|---|---|---|---|---|
| **F-single** | `runpod-upscale-only-flashvsr.yaml` | (N/A — video-in) | 2x | ~8 GB | ~60 s (cold) + ~30 s upscale | Output MP4 exists; dims == 2× input; ffprobe valid H.264; `flashvsr-wan21-fp16` in `kinoforge logs`; pod destroyed post-run | ~$0.05 |
| **F-multi** | `runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml` | `/workspace/examples/configs/prompts/field-realistic.txt` | 2x | ~50 GB peak (Wan 14B + FlashVSR) | ~15 min cold + ~2 min stage-2 | Two MP4s land (Wan 720p + FlashVSR 1440p); dims 2×; warm-reuse confirmed on second prompt | ~$0.50 |
| **F-warm** | Same as F-multi, second `kinoforge generate` same session | Second prompt | 2x | Same peak | ~3 min (no cold-boot tax) | New MP4 dated after first; log shows LRU hit; NO BSA recompile (cache hit in `/workspace/.cache/bsa`) | ~$0.10 |

**Total live budget: ~$0.65** happy path; **ceiling $3** — hard-stop if a single smoke
exceeds $1 (matches P3 budget shape).

### 7.3 Live-smoke rules (existing memory, restated)

- `pixi run preflight` before each spend: clean tree + zero pods + creds.
- `--no-reuse` on F-single (one-shot). F-multi + F-warm warm-reuse across two runs;
  `--no-reuse` on the final invocation.
- Post-run verify `kinoforge list` shows no pods AND no ledger entries; explicit
  `kinoforge destroy --id <pod>` on any leak.
- Poll pod stats every 60-90 s during any live smoke (RunPod GraphQL:
  `gpuUtilPercent`, `cpuPercent`, `memoryPercent`, `costPerHr`). GPU at 0% for ≥3
  probes during compute-phase → kill fast, fail loud.
- Standard test prompt read verbatim from
  `/workspace/examples/configs/prompts/field-realistic.txt` (cross-model comparison).
- Log qualifying successful generation to `/workspace/successful-generations.md` per
  its schema (new capability axis: `flashvsr` engine + `flashvsr-wan21-fp16` model
  + upscale mode).

## 8. Rollout + documentation

- `PROGRESS.md` "Active workstream" section updated at spec-write time; pointer to
  this spec + implementation plan.
- `docs/engines.md` (if present) gets a `flashvsr` section mirroring the spandrel
  section from P2.
- `examples/configs/runpod-diffusers-wan22-t2v-a14b-spandrel.yaml` gets a comment
  header pointing at `-flashvsr.yaml` as the v1 default.
- Post-ship: log to `/workspace/successful-generations.md`.
- Success criterion: F-multi live-GREEN with both artifacts materialized + pod
  destroyed + warm-reuse confirmed. Then `PROGRESS.md` flips to "FlashVSR SHIPPED".

## 9. Order of execution (informative — full plan comes next)

1. T0-T10 unit-GREEN, RED-first, atomic commits.
2. `pixi run preflight`.
3. F-single spend → evidence commit.
4. F-multi spend (warm-reuse enabled — pod survives).
5. F-warm spend (same pod, second prompt) → combined evidence commit.
6. Log to `successful-generations.md`.
7. `PROGRESS.md` update: FlashVSR SHIPPED; spandrel demoted to fallback in cfg
   comments.

## 10. Open questions

None at spec approval time. If FlashVSR quality on Wan 2.2 output proves inadequate,
STAR (MIT) is the pre-vetted #2 fallback — separate spec.
