# MiniMax-H3 text-to-audio-video on Modal — design

**Date:** 2026-09-17
**Status:** approved in brainstorm; not yet planned
**Budget:** $20 operator-authorised for the whole effort

## Goal

Add MiniMax-H3 (`MiniMaxAI/MiniMax-H3`, a.k.a. Hailuo 3.0) as a kinoforge video
model on Modal, generating **native 2K / 24 fps video with its joint stereo
audio intact**, driven by the same `kinoforge generate` command shape already
used for Wan 2.2.

## What this is NOT

**The Wan 2.2 post-chain is deliberately out of scope for H3.** That chain
(480² → FlashVSR 1080p → RIFE 60 fps) exists to compensate for Wan's low native
output. H3 emits up to 2K at 24 fps already, so upscaling it is at best a no-op,
and RIFE would desynchronise a soundtrack that was generated in lockstep with
the frames. Operator decision 2026-09-17: **t2va only, keep the audio.**

Also out of scope: `fl2va` (first/last-frame), image-to-video, `ref2va`
(reference) modes, INT8/NVFP4 quantised weights, and multi-GPU SGLang serving.
Each is a later increment; none is needed for a first working clip.

**Two of those deferrals are much cheaper to reverse than the third**, and the
plan order should reflect it. The **FL2VA checkpoint already covers
text-to-video *and* image-to-video with optional first/last-frame inputs** — so
once Sub-project B has cached it, adding `i2v` and `fl2va` costs no new download
and no new hardware, only mode wiring. **`ref2va` is the expensive one**: it is a
separate checkpoint of comparable size (multi-reference — up to 9 images, 3 video
clips and 3 audio clips at once), so enabling it means a second ~144 GB fetch.
Sequence accordingly: i2v/fl2va are natural next increments after C; ref2va is
its own budgeted project.

## Grounding — where these facts come from

**This model postdates the assistant's training data.** Every claim below was
read from the live model card, the live repo tree, or Modal's live docs during
the 2026-09-17 brainstorm. Nothing here is recalled. Where a number could not be
sourced from the vendor, that is stated rather than guessed.

| Fact | Value | Source |
|---|---|---|
| `t2va` is a supported task on FL2VA | yes, no image inputs required | model card task list |
| FL2VA folder is self-contained | own `model_index.json`, `transformer`, `text_encoder`, `tokenizer`, `video_vae`, `audio_vae`, `processor` | repo tree |
| FL2VA folder size | **144 GB** | repo tree |
| Whole repo size | 498 GB | repo tree |
| bf16 component sizes | **MEASURED from the HF API 2026-09-17**: transformer 66.28 GB, text_encoder 66.73 GB, video_vae 10.42 GB, audio_vae 0.61 GB (**≈144 GB on disk, ≈133 GB of model weights**). The earlier 61.7/48.0 figures came from a third-party listing and were BOTH too low. | HF API, `FL2VA/` |
| Native output | up to 2K, 24 fps, 15 s, 32 kHz stereo audio | model card |
| Inference entry point | `ModularPipeline.from_pretrained("MiniMaxAI/MiniMax-H3")` | operator, 2026-09-17 |
| Minimal fetch patterns | ~~`FL2VA/*`~~ **SUPERSEDED — see the CORRECTION section.** Fetch the ROOT-level modular layout: `model_index.json modular_model_index.json text_encoder/* tokenizer/* processor/* vae/* audio_vae/* transformer/* scheduler/* audio_scheduler/*` | `modular_model_index.json`, measured |
| FL2VA covers | text-to-video **and** image-to-video, with optional first/last frame inputs | operator, 2026-09-17 |
| Ref2VA covers | multi-reference: up to 9 images, 3 video clips, 3 audio clips at once | operator, 2026-09-17 |
| Modal H200 VRAM | **141 GB** (verbatim) | Modal GPU docs |
| Modal accepts gpu strings | `"H200"`, `"B200"`, `"B200+"`, `"B300"` | Modal GPU docs |
| Modal H200 price | $0.001261/sec ≈ **$4.54/hr** | Modal pricing |

**Stated VRAM requirement: none.** The model card gives no hardware floor, so
every hardware claim here is arithmetic on measured weight sizes, not a vendor
number. Measured 2026-09-17: **~133 GB of weights** (text_encoder 66.73 +
transformer 66.28), plus ~11 GB of VAEs. That does not fit 80 GB, and does not
fit a 141 GB H200 alongside activations either — which is why
`enable_model_cpu_offload` is mandatory rather than optional. The remaining
assumption is that ~77 GB peak under offload leaves enough headroom at
1344x768x73; that is untested until the first live run.

## RESOLVED 2026-09-17 — engine route is DIFFUSERS, and the memory math changed

Both probes ran ($0, offline). Results, then the ruling.

### Probe A — per-file sizes, measured from the HF API (not a third-party listing)

**ComfyUI route (`Comfy-Org/MiniMax-H3`), the reference workflow's exact files:**

| File | Size |
|---|---|
| `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 20.97 GB |
| `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 15.69 GB |
| `vae/minimax_h3_video_vae_fp16.safetensors` | 5.21 GB |
| `vae/minimax_h3_audio_vae_fp32.safetensors` | 0.61 GB |
| **total** | **42.48 GB** (+1.96 GB for `loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16`) |

**Diffusers route (`MiniMaxAI/MiniMax-H3`, `FL2VA/`): 144.1 GB across 81 files** —
text_encoder 66.73, transformer 66.28, video_vae 10.42, audio_vae 0.61.
**No fp8/int8/nvfp4 variant exists anywhere under official `FL2VA/`.** The
quantised weights are Comfy-Org repackages and have no official counterpart.

### Probe B — ComfyUI node support

**`MiniMaxH3ImageToVideo` IS in ComfyUI core**, at `comfy_extras/nodes_minimax_h3.py`
(~30 KB) on `master`. kinoforge clones ComfyUI from `master` **unpinned**
(`engines/comfyui/__init__.py:1165`), so a fresh pod would carry it. The rest of
the template is core too: `UNETLoader`, `CLIPLoader` (type `minimax`), `VAELoader`,
`VAEDecode`, `VAEDecodeAudio`, `CreateVideo`, `SamplerCustomAdvanced`,
`BasicGuider`, `BasicScheduler`, `KSamplerSelect`, `RandomNoise`,
`LoraLoaderModelOnly`.

### Ruling: DIFFUSERS, despite ComfyUI being 3.4x smaller and on a cheaper card

The ComfyUI route needs **three** unproven links to all hold, and each is unbounded:

1. **Modal + ComfyUI has never existed.** Every shipped ComfyUI config targets
   RunPod or SkyPilot-Lambda. `("comfyui", "modal")` is `True` in
   `EPHEMERAL_CAPABILITIES`, but no config, no golden, no live run.
2. **The ComfyUI engine is unproven for three months.** Last live ComfyUI
   generation: **2026-06-18** (log entries #4/#5/#7). Since then
   `provision_script` was deleted (`4856a85a`), setup-steps were reworked
   (`8e584fe2`), and `compute.requirements` became the placement block
   (`13316c99`) — all touching this engine. Offline tests are green and all four
   configs are in the golden ratchet, so it is **unproven, not broken** — but
   unproven is what costs money to discover.
3. **The reference template is a SUBGRAPH workflow.** Its real nodes are nested
   under `definitions.subgraphs`, with `ComfySwitchNode` / `ComfyMathExpression`
   driving the turbo toggle. `tools/comfyui_ui_to_api.py` vendors a third-party
   converter that predates subgraphs, and its captured-`/object_info` input would
   itself have to come from a live H3-capable pod first.

Diffusers on Modal, by contrast, is the **only** Modal path ever proven, and was
proven again on 2026-09-17 with three green Wan 2.2 runs. One new server module
against three unbounded unknowns is the cheaper bet.

**This is not a verdict on ComfyUI.** The route is viable and materially cheaper,
and the node support is real. It is a sequencing call: it should be opened by a
deliberate Modal+ComfyUI project with its own budget, not discovered mid-H3.

### The memory math CHANGED, and it is the most important finding here

The spec previously estimated ~115 GB of bf16 weights from a third-party listing.
**Measured, it is ~133 GB** (text_encoder 66.73 + transformer 66.28), plus 11 GB
of VAEs. On H200's 141 GB that leaves **~8 GB for activations at 1344x768x73
frames, which will OOM.**

**Therefore `enable_model_cpu_offload` is MANDATORY, not an optimisation.** With
the text encoder offloaded after prompt encoding, peak residency is
transformer 66.28 + video_vae 10.42 + audio_vae 0.61 = **~77 GB**, leaving ~64 GB
of H200 headroom for activations. H200 remains the right card and Sub-project A
stands; what changes is that the naive `device_map="cuda"` from the model card
**must not** be used as-is.

## CORRECTION 2026-09-17 — the FL2VA layout is the WRONG one, and `diffusers==0.40.0` is the floor

Measured after Sub-project B's first fetch. **This spec was wrong about which
half of the repo to load, and the operator's `ModularPipeline.from_pretrained`
snippet was right.**

### The repo ships the same FL2VA weights TWICE

| Path | Declares | Size |
|---|---|---|
| `FL2VA/` (nested) | `_class_name: MiniMaxH3Pipeline`, `_diffusers_version: 0.32.2` | 144.05 GB |
| root-level `text_encoder/ transformer/ vae/ audio_vae/ …` | `_class_name: MiniMaxH3ModularPipeline`, `_diffusers_version: 0.36.0.dev0` | 144.04 GB |
| `Ref2VA/` + `transformer_ref/` | the ref2va task, not needed here | 144.05 + 66.28 GB |

### Only the modular path is loadable

Probed against the diffusers source:

- **`MiniMaxH3Pipeline` is not exported by diffusers at all**, and
  `pipelines/minimax_h3/pipeline_minimax_h3.py` does not exist. The
  `_class_name` in `FL2VA/model_index.json` is **not loadable from a stock
  install** — so the nested layout is a dead end without `trust_remote_code`.
- **`MiniMaxH3ModularPipeline` IS exported**, alongside
  `modular_pipelines/minimax_h3/` and `models/autoencoders/autoencoder_kl_minimax_h3.py`.

**Minimum release: `diffusers==0.40.0`.** Tag-by-tag: 0.40.0 exports
`MiniMaxH3ModularPipeline`; **0.39.0 and 0.38.0 do not**. The manifest's
`0.36.0.dev0` is a dev build that predates the release. This retires risk 3 — the
pin is a real released version, and it is nowhere near the `diffusers>=0.32` the
Wan configs use, so **the H3 image must pin its own diffusers**.

### What Sub-project C must actually load

```python
ModularPipeline.from_pretrained("MiniMaxAI/MiniMax-H3")  # repo ROOT
```

Components and their subfolders, read from `modular_model_index.json`:

| Component | Class | Subfolder |
|---|---|---|
| text_encoder | `Qwen3VLForConditionalGeneration` | `text_encoder` |
| tokenizer | `Qwen2TokenizerFast` | `tokenizer` |
| processor | `Qwen3VLProcessor` | `processor` |
| vae | `AutoencoderKLMiniMaxH3` | `vae` |
| audio_vae | `AutoencoderKLMiniMaxH3Audio` | `audio_vae` |
| transformer | `MiniMaxH3Transformer3DModel` | `transformer` |
| transformer_ref | `MiniMaxH3Transformer3DModel` | `transformer_ref` |
| scheduler | `MiniMaxH3Scheduler` | `scheduler` |
| audio_scheduler | `MiniMaxH3Scheduler` | `audio_scheduler` |

**`transformer_ref` is ref2va-only (66.28 GB) and is deliberately NOT fetched.**
If the modular loader insists on every declared component, add it — that is a
one-line change to the fetch and ~$0.04 more, which is exactly the kind of
question Sub-project B made cheap to answer.

### Why this correction cost $0.08 instead of $4

The wrong-layout fetch ran on a **T4**, not the H200. Had C gone straight to the
expensive card, this mistake would have surfaced ~50 minutes and ~$4 in, as a
load error on a booked H200. **This is the concrete payoff for building B before
C**, and it is worth remembering the next time a prefetch step looks like
optional ceremony.

## The pipeline API, read from diffusers v0.40.0 source

All of this is quoted from `src/diffusers/modular_pipelines/minimax_h3/` at tag
`v0.40.0`, not inferred.

```python
import torch
from diffusers import ModularPipeline

pipe = ModularPipeline.from_pretrained("MiniMaxAI/MiniMax-H3", workflow="t2va")
pipe.load_components(dtype=torch.bfloat16)
```

**`workflow="t2va"` is MANDATORY, and omitting it is expensive.** The blocks
module says in terms: *"Without a `workflow=`, loading the components pulls
**both** 61.7GB transformer partitions."* We deliberately did not prefetch
`transformer_ref/`, so a missing `workflow=` would either fail the load or pull
66 GB **on the H200 at $4.54/hr**. This single kwarg is the difference between a
working run and the most expensive failure available in this project.

`_workflow_map` declares `"t2va": {"prompt": True}` — text only, no image roles,
which is what makes `MODE_ROLE_REQUIREMENTS["t2va"] = {}` correct.

**Outputs:** `videos`, `audio`, `sampling_rate` (the audio VAE reports
`sampling_rate = 32000`, matching the model card's 32 kHz stereo claim).

**Guidance-distilled: there is no guider, no `negative_prompt`, and no
`guidance_scale`,** and every step runs exactly one forward pass. A request
schema offering `guidance_scale` would be offering something the model cannot
use — leave it out rather than accept-and-ignore it.

**Two schedulers, stepped inside a single transformer call:** `shift = 12.0` for
video, `shift = 3.0` for audio — matching `sigma_shift_scales` in the manifest.
H3 denoises **one packed sequence** holding text conditioning, keyframe latents,
audio latents and video latents together, which is why the audio is inherently
in sync with the frames and why post-hoc interpolation would break it.

**"MiniMax-H3 is modular only: this pipeline and its blocks are the whole
integration, there is no `DiffusionPipeline` half."** That sentence is in the
class docstring, and it independently confirms the CORRECTION above: the
`_class_name: MiniMaxH3Pipeline` in `FL2VA/model_index.json` names a class that
does not exist.

## CORRECTION 2026-09-18 — the offload API does not exist, and the geometry is a contract

Two findings from reading `diffusers/modular_pipelines/` at v0.40.0 while
implementing Sub-project C. Both would have failed on a booked H200.

### `enable_model_cpu_offload` is not a method on `ModularPipeline`

The decision row below says it is mandatory. The *intent* is right and the
*call* does not exist. `ModularPipeline` subclasses `ConfigMixin, PushToHubMixin`
— **not** `DiffusionPipeline` — and defines no `enable_*_cpu_offload` of any
kind; `grep -n 'def enable_'` over `modular_pipelines/modular_pipeline.py`
returns nothing. The name appears in that file only inside two warning strings
in `to()`. Calling it is an `AttributeError`, at $4.54/hr.

The mechanism, and the recipe the diffusers H3 doc gives for a single card:

```py
manager = ComponentsManager()
pipe = ModularPipeline.from_pretrained(repo, workflow="t2va", components_manager=manager)
pipe.load_components(dtype=torch.bfloat16)
manager.enable_auto_cpu_offload(device="cuda", memory_reserve_margin="12GB")
```

Every model starts on CPU and is moved onto the accelerator when a block reaches
it, then evicted when another needs the room.

**This also weakens risk 1.** The "141 GB fit" worry assumed both large
components resident (61.7 GiB transformer + 62.1 GiB conditioner = 123.8 GiB on
a 131.3 GiB card, ~7 GiB left for activations). Under auto offload peak device
residency is ~62 GiB, so the canvas is not the constraint it looked like. The
binding resource moves to **host RAM**: the weights live there, ~124 GiB of it,
and Modal's default container memory *request* is 128 MiB with "can exceed if
the worker has available memory" — so that headroom is a property of the machine
we land on, not of our request. The H3 server therefore logs `/proc/meminfo`
MemTotal/MemAvailable and `torch.cuda.mem_get_info()` before and after the load,
so a shortfall is one legible line rather than an unexplained container death.

### Geometry is a hard contract, checked inside the pipeline

None of this was in the spec, and each is a `ValueError` raised from
`before_denoise` — i.e. minutes after a 124 GiB load finished:

| Rule | Source |
|---|---|
| `height`/`width` multiples of **32** | `canvas_multiple` = VAE spatial compression 16 x `patch_size[2]` 2 |
| `num_frames` snapped up to `17 * n + 5` | the video VAE's `clip_length` / `tokens_chunk_size` |
| duration in **5.0-15.0 s** at 24 fps, checked on the ALIGNED count | `min_duration` / `max_duration` |
| **15 s is unreachable; the ceiling is 345 frames = 14.375 s** | 360 aligns to 362 = 15.083 s, so 346-360 look legal and are not |
| 24 fps, fixed | `MINIMAX_H3_FPS`; everything is resampled onto it |
| `num_inference_steps` defaults to 50 | `InputParam.template`; guidance-distilled is not step-distilled |

Trained canvas is 1344x768 (`canvas_short_edge` 768 at 16:9, which is also the
`canvas_max_pixels` budget); 960x544 is measured at ~2.3x faster per step and is
the first mitigation if anything OOMs. Default `num_frames` is **124** — the
shortest legal clip, and the only `17n+5` value in the 120-126 range.

**Clip lengths are DISCRETE**, one per `17n+5`: 124 (5.167 s), 141, 158, 175,
192 (exactly 8.000 s), 209, 226, 243, 260, 277, 294, 311, 328, 345 (14.375 s).
Nothing between them exists, and nothing above 345 does either.

The server re-states all of it and refuses violations at the HTTP edge, so a bad
number costs nothing instead of costing a load.

## Decomposition

Three sub-projects. **A and B ship before C** (operator decision 2026-09-17);
both are cheap and both de-risk C.

| | Sub-project | Live spend | Gate |
|---|---|---|---|
| A | Modal catalog: add H200 | $0 | offline tests green |
| B | HF Volume prefetch path | ~$0.51 | 144 GB resident on the Volume |
| C | H3 t2va server + config | ~$5 | one frame-QA'd clip with audio |

---

## Sub-project A — Modal catalog: add H200

### Problem

`src/kinoforge/providers/modal/_catalog.py:16` holds `_MODAL_GPUS`, a 7-entry
tuple snapshotted **2026-07-08**, whose largest card is H100 at 80 GB. Modal now
sells H200/B200/B300. Because `Offer.gpu_type` is passed straight through to
Modal's `gpu=` parameter (`providers/modal/__init__.py:310` →
`providers/modal/_app.py:135`), the catalog is the *only* thing deciding what we
can book — and it currently cannot express any card large enough for H3 in bf16.

### Change

Add exactly one row:

```python
("H200", 141, 4.54),
```

Re-date the snapshot comment to 2026-09-17.

### Why H200 only, and not B200/B300

**Modal's own documentation does not state B200 or B300 VRAM.** Only NVIDIA
datasheets do. Writing a VRAM constant the provider never supplied is precisely
the **U48** defect — SkyPilot reported a CUDA version from a lookup that could
not succeed, every offer carried the same fabricated constant, and every cfg
leaving `min_cuda` at its default had its entire catalog filtered away. A wrong
`vram_gb` here fails the same way: silently, through `filter_offers`, with no
error to read.

H200's 141 GB is quoted verbatim by Modal and is sufficient for this design.
B200/B300 are deferred until their VRAM can be sourced from Modal itself.

### Tests

- A `Placement(min_vram_gb=120)` returns H200 and nothing smaller.
- A `Placement(min_vram_gb=120, accelerators=["H200"])` ranks H200 first.
- Existing catalog tests stay green (H200 must not displace A100-80GB for
  workloads that already fit, or every Wan config gets more expensive).
- The `"H200"` id string matches Modal's accepted spelling exactly — asserted as
  a literal, because a typo here surfaces only as a live create failure.

### Done when

Offline tests green, committed. No live spend.

---

## Sub-project B — HF Volume prefetch

### Problem

`kinoforge-hf-cache` (`providers/modal/_app.py:54`) is mounted at `HF_HOME` on
every Modal pod, so weights persist across runs. But **there is no way to
populate it except by running the job that needs it** — grep for
`prefetch`/`warm_cache` returns nothing. The first H3 run would therefore pull
144 GB while an H200 bills at $4.54/hr (~$3.94), and an interrupted fetch would
burn that again.

### Change

A path that mounts the same Volume on a **T4** ($0.59/hr — the cheapest card in
the catalog) and runs:

```python
snapshot_download(
    "MiniMaxAI/MiniMax-H3",
    allow_patterns=["model_index.json", "FL2VA/*"],
)
```

followed by an explicit `volume.commit()`.

**Both patterns are required.** `FL2VA/*` alone omits the repo-root
`model_index.json`, producing a 144.1 GB download that looks complete and does not
load. Operator-supplied 2026-09-17, matching the documented
`huggingface-cli download ... --include "model_index.json" "FL2VA/*"`. A test
must assert **both** patterns are present, not merely that `allow_patterns` is
non-empty — the omission is invisible until load time, and load time is on the
expensive card.

`allow_patterns` is otherwise load-bearing for cost: without it this pulls 498 GB
instead of 144 GB, tripling both time and money.

### Properties

- **Resumable.** HF's cache dedupes by blob hash, so a retry after a failure
  re-fetches only what is missing. A failed prefetch costs cents.
- **Idempotent.** Running it against an already-warm Volume is a no-op.
- **Reusable.** Nothing about it is H3-specific beyond its arguments; it serves
  every future large model.

A CPU-only container would be cheaper still, but whether the Modal provider can
compose a GPU-less request is unverified — T4 is the decided default, and
CPU-only is a later optimisation to be measured, not assumed.

### Tests

- Offline: the composed Modal request carries the Volume name, a cheap GPU, and
  `allow_patterns` containing **both** `"model_index.json"` and `"FL2VA/*"` —
  asserted without touching the network.
- Offline: a missing/empty `allow_patterns` is rejected loudly rather than
  silently fetching the whole 498 GB repo.
- Offline: `allow_patterns` carrying only `"FL2VA/*"` is rejected — the
  root-manifest omission is the failure that would otherwise be discovered on
  the H200.
- Live: one real prefetch; verify by listing the Volume and confirming the
  FL2VA tree is resident and ~144 GB.

### Done when

144 GB of `FL2VA/*` is resident on `kinoforge-hf-cache` and a second invocation
is a no-op. Spend recorded.

---

## Sub-project C — H3 t2va server and config

### Server

**New module** `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py`,
a sibling of `wan_t2v_server.py` — **not** an extension of it. That file is 2610
lines with 65 Wan-specific references; threading a second architecture through it
would put the working Wan path at risk for no benefit.

The new server reuses:
- `servers/_util_stats.py` — serves the `/util` route. **Load-bearing**: the
  CLAUDE.md live-smoke rule requires polling `gpuUtilPercent`, and a server
  without this route cannot be monitored.
- a **new** `servers/_av_io.py` (below).

The diffusers engine is already config-driven — `server_cmd`, `embed_modules`,
`embed_files` are all read from YAML (`engines/diffusers/__init__.py:1022-1081`)
— so **no engine fork is required**. The config points `server_cmd` at the new
module.

### Audio output

`servers/_video_io.py:write_mp4` takes `(T, H, W, 3)` uint8 and nothing else.
**This is the exact line at which H3's audio would be silently discarded.**

Add `servers/_av_io.py`:

```python
def write_mp4_with_audio(frames, audio, fps, sample_rate, path) -> None
```

muxing H.264/yuv420p video with AAC stereo. **`_video_io.write_mp4` is not
touched**, so the Wan/FlashVSR/RIFE paths cannot regress.

### Mode

Add to `MODE_ROLE_REQUIREMENTS` (`core/interfaces.py:799`):

```python
"t2va": {},
```

Empty dict — t2va takes no image roles, same shape as `t2v`.

### Capability profile

The H3 profile sets `supports_joint_audio=True` — **the first `True` in the
repo**. Verified 2026-09-17: `supports_joint_audio=False` appears at **11 sites
across 11 files** (video engines, image engines and the fake backbone alike) and
`supports_joint_audio=True` appears **nowhere**.

**Do not assume this does anything.** `core/strategy.py:55` reads it to write an
`_audio_mode` marker into the job spec "so a downstream stage can branch", and a
grep confirms **nothing reads `_audio_mode` anywhere**. It is a dormant
placeholder, not a working seam — the same shape as **U40** (correct logic,
unreachable) and **U24** (the fix that was one field short).

**Operator decision 2026-09-17: leave the marker inert, and say so in the code.**

The flag is set to `True` because it is *factually true* — H3 does generate joint
audio, and a capability profile that lied about that would be its own defect. But
nothing may be built on it in this project. Concretely:

- Add a comment at `core/strategy.py:55` recording that `_audio_mode` is written
  and read nowhere, that this is deliberate as of 2026-09-17, and that H3 is the
  first model for which the value is not a constant.
- Add a comment at the H3 profile site recording that `supports_joint_audio=True`
  is a capability *declaration*, and that the audio actually reaches the output
  through `_av_io.write_mp4_with_audio` — **not** through the strategy seam.
- **No test may assert behaviour through `_audio_mode`.** Audio behaviour is
  tested at `_av_io` and at the frame-QA audio arm, where it is real.

The comments are the deliverable here. An inert seam that is *documented* as
inert is fine; an inert seam that reads as working is how U40 cost a live run.
A future agent finding `supports_joint_audio=True` must be able to see, without
grepping, that the strategy marker is not the mechanism.

### Config

`examples/configs/modal-diffusers-minimax-h3-t2va.yaml`:

- `compute.provider: modal`, `placement.min_vram_gb: 120`,
  `accelerators: ["H200"]`
- `models: [{ref: "hf:MiniMaxAI/MiniMax-H3", ...}]` restricted to `FL2VA/*`
- `spec`: 24 fps, resolution and frame count chosen so the first run is the
  *cheapest* clip that proves the path, not the largest
- lifecycle `boot_timeout` generous enough for a warm-Volume load of ~115 GB

**The config must enter the golden ratchet.** `EXCLUDED_CONFIGS` is empty for
the first time since the Modal matrix opened (U35); it must stay empty.

### Frame QA — with an audio arm

The CLAUDE.md visual-QA rule applies unchanged: extract frames, eyeball a contact
sheet, record the verdict. **Plus an audio assertion**, because this is the first
model where a silent "success" is possible:

- an audio stream exists in the container
- its duration matches the video's within tolerance
- it is stereo at the expected sample rate
- it is not digital silence

The FlashVSR lesson is the precedent: 24+ runs reported green on exit code and
dimensions while every output was garbage, because nothing looked at the actual
payload. Dimensions cannot see pixels; `ffprobe` alone cannot hear silence.

### Done when

One `kinoforge generate --mode t2va` produces a 2K/24 fps clip with verified
stereo audio, frame-QA recorded, pod torn down and verified from a fresh process,
and an entry written to `successful-generations.md` (new capability axis: new
model, new mode, new provider-GPU, first joint-audio output).

---

## Spend plan

| Step | Est. | Basis |
|---|---|---|
| A | $0 | offline only |
| B | ~$0.51, ~$1 with one retry | 144 GB on a T4 at $0.59/hr |
| C | ~$0.76 per attempt, budget 6–8 | H200 $4.54/hr, ~10 min per warm-Volume attempt |
| **Total** | **~$6–7** | leaves ~$13 headroom |

Without B, C costs ~$4–5 *per attempt* and $20 buys three or four shots total.
This repo's history — FlashVSR 24+ attempts, RIFE 6 boots — says that is not
enough. **The prefetch is what makes the budget viable**, which is why it is a
prerequisite rather than an optimisation.

All live spend follows the standing rules: `pixi run preflight` first,
`--no-reuse` on every one-shot, utilisation polled during the run (not
`est_spend`), teardown verified from a fresh process after the orchestrator
exits.

## Risks

1. **The 141 GB fit is unverified.** ~115 GB of weights leaves ~26 GB for 2K
   activations. If it OOMs, the first mitigation is a smaller resolution or frame
   count — far cheaper than changing hardware, and it isolates whether the
   problem is capacity or configuration.
2. ~~**`from_pretrained` may need an explicit subfolder or variant.**~~
   **Largely resolved 2026-09-17** by operator: the entry point is
   `ModularPipeline.from_pretrained("MiniMaxAI/MiniMax-H3")` against the repo
   root, which is why the root `model_index.json` must be in the fetch. Residual:
   confirm the root manifest resolves to FL2VA when only FL2VA is cached — verify
   offline by reading the fetched `model_index.json` after Sub-project B lands,
   before spending on C.
3. ~~**diffusers version conflict.**~~ **RESOLVED 2026-09-17 by tag probe:**
   `diffusers==0.40.0` is the minimum release exporting `MiniMaxH3ModularPipeline`
   (0.39.0 and 0.38.0 do not). The H3 image pins its own diffusers; the Wan
   images keep `>=0.32` and are untouched. Residual: confirm 0.40.0 coexists with
   the torch 2.6.0+cu124 stack the other Modal configs use.
4. **Image bake cost.** The first deploy of any new Modal config pays a full
   image build (356 s observed for FlashVSR on 2026-09-17). Budget it once.
5. ~~**License gating.**~~ **RETIRED 2026-09-17 by measurement.** The HF API
   reports `gated=False, private=False` for `MiniMaxAI/MiniMax-H3`, both
   anonymously and with our token, at 4.58M downloads. The
   `minimax-h3-community-license-agreement` governs USE, not download access.
   No acceptance step is needed.
6. **H200 now makes an under-capped config launch-then-die instead of fail-free.**
   Every Modal offer is `mode="serverless"`, so `filter_offers` skips the
   `max_usd_per_hr` ceiling at selection time — but `_enforce_rate_cap`
   (`src/kinoforge/orchestrator.py`, around line 973) still destroys the
   instance *after launch* if the realized rate exceeds the cap. Before H200
   existed, a config asking for >80 GB VRAM simply got a `CapacityError` for
   free. Now it can book H200 at $4.54/hr and then get torn down mid-run if its
   `max_usd_per_hr` is below 4.54. All five shipped Modal configs sit between
   1.00 and 4.00. **The MiniMax-H3 config (Sub-project C) must set
   `max_usd_per_hr >= 4.54`**, or it will pay for a launch it cannot keep.

## Decisions log

| Date | Decision | Rationale |
|---|---|---|
| 2026-09-17 | t2va only; no FlashVSR/RIFE for H3 | H3 is natively 2K/24 fps; upscaling is a no-op and interpolation would desync joint audio |
| 2026-09-17 | H200 bf16, not A100 offload or INT8 | Fits outright with headroom; official weights; fewest unknowns |
| 2026-09-17 | Prefetch to the Volume from a T4 | Turns a ~$3.94 fetch into ~$0.51 and makes the budget viable |
| 2026-09-17 | A and B ship before C | Both cheap, both de-risk C |
| 2026-09-17 | H200 only; defer B200/B300 | Modal does not state their VRAM; inventing the constant is the U48 defect |
| 2026-09-17 | Leave `_audio_mode` inert, document it | Flag is factually true; the seam is not the mechanism, and must not read as one |
| ~~2026-09-17~~ | ~~Fetch `model_index.json` + `FL2VA/*`~~ **SUPERSEDED** — fetch the ROOT-level modular layout instead | `MiniMaxH3Pipeline` (what FL2VA declares) is not exported by diffusers at all; only `MiniMaxH3ModularPipeline` is |
| 2026-09-17 | **Pin `diffusers==0.40.0` minimum** | Tag-probed: 0.40.0 exports `MiniMaxH3ModularPipeline`, 0.39.0 and 0.38.0 do not. Far above the `>=0.32` the Wan configs use, so H3 pins its own |
| 2026-09-17 | Entry point is `ModularPipeline`, not `DiffusionPipeline` | Operator correction; loads the repo root, which is why the root manifest is required |
| 2026-09-17 | **Engine route RESOLVED: diffusers.** ComfyUI is 3.4x smaller (42.48 vs 144.1 GB) on a cheaper card and its H3 nodes are real, but needs three unproven links: Modal+ComfyUI never built, engine unproven since 2026-06-18, and the template is a subgraph the vendored converter predates | Measured both probes; one new server module beats three unbounded unknowns |
| ~~2026-09-17~~ | ~~**`enable_model_cpu_offload` is MANDATORY**~~ **SUPERSEDED 2026-09-18** — CPU offload is mandatory, but via `ComponentsManager.enable_auto_cpu_offload`; `enable_model_cpu_offload` is not a method on `ModularPipeline` at all | Weights measured at ~124 GiB against a 131.3 GiB card. See the 2026-09-18 CORRECTION: the named call would have been an `AttributeError` on the booked H200 |
| 2026-09-18 | **Sub-project B confirmed complete** — 288.10 GB durable, re-measured from a fresh container | A controller-side sum of `hub/models--<repo>/blobs` read 1.96 GB and looked like a total failure; it is the WRONG measure (Modal reports size 0 for a symlink, and xet-backed content is not under `blobs/`). A fresh container walking the snapshot sees transformer 66.28 + text_encoder 66.73 + vae 10.42 + audio_vae 0.61 GB with zero broken symlinks, matching the HF API file-for-file |
| ~~2026-09-17~~ | ~~Engine route deferred; probe both offline first~~ | The ~38 GB saving is unverified per-file, and whether our comfyui engine carries H3's node types is unknown. Both probes are $0; guessing wrong costs a 144 GB fetch or a dead-end path |

## Open questions for plan time

None blocking. Two to resolve offline, for free, before any spend:

- the exact `from_pretrained` invocation that selects FL2VA (risk 2)
- the minimum diffusers version, and whether it can coexist with the Wan pin
  (risk 3)
