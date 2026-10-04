# Configuration

(Moved from README §Configuration, §Concurrency, §HuggingFace ref grammar, §Per-job spec & params on 2026-06-27. See [../README.md](../README.md).)

## Configuration

Each kinoforge run is described by a single YAML file with three required blocks:

```yaml
engine:      # which generation backend to use + precision
models:      # ordered list of model refs (base + optional loras/vae)
compute:     # where to run (provider + image + placement + backend_options)
```

Since compute-seam S1 the `compute:` block splits hardware constraints into a portable
`placement:` sub-block and a per-provider `backend_options.<provider>:` namespace; an unknown key
in either is a `ConfigError` at load. The pre-2026-08 `compute.requirements` / `compute.cloud` /
`compute.cloud_type` shape is refused with a message naming the new path. See
[breaking-changes.md](breaking-changes.md) for the before/after and the per-field verdicts.

Optional blocks add pipeline stages and plumbing: `keyframe:`, `upscale:`, `interpolate:`,
`store:`, `output:`, `lifecycle:`, `loras:`, `spec:`, `params:`. `image:` is different in kind
from the rest — its presence opts the whole config into a separate terminal-image path
(`kinoforge image`) rather than adding a stage to the video pipeline; see below.

For hosted engines (e.g. fal.ai) the `compute:` block is omitted and a top-level `lifecycle: {budget: N}` carries the spend guard instead.

Browse ready-to-use examples in [`../examples/configs/`](../examples/configs/):

| File | Engine | Provider | Use case |
|------|--------|----------|----------|
| [`runpod-comfyui-wan-2_2-14b-t2v.yaml`](../examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml) | ComfyUI | RunPod pod | Production Wan2.2 + CivitAI LoRA |
| [`runpod-diffusers-serverless.yaml`](../examples/configs/runpod-diffusers-serverless.yaml) | Diffusers | RunPod serverless | SVD serverless |
| [`hosted.yaml`](../examples/configs/hosted.yaml) | Hosted API | fal.ai | Zero-infra hosted |
| [`local-fake.yaml`](../examples/configs/local-fake.yaml) | Fake | Local | Offline / CI smoke test |
| [`modal-diffusers-wan-2_2-14b-t2v.yaml`](../examples/configs/modal-diffusers-wan-2_2-14b-t2v.yaml) | Diffusers | Modal | Serverless GPU, Wan 2.2 14B |
| [`runpod-diffusers-flashvsr-x4-upscale.yaml`](../examples/configs/runpod-diffusers-flashvsr-x4-upscale.yaml) | Diffusers | RunPod pod | Standalone FlashVSR upscale |

## Concurrency

By default kinoforge runs one generation job at a time (sequential). Add
`max_in_flight` to your `lifecycle:` block to enable concurrent dispatch:

```yaml
compute:
  ...
  lifecycle:
    idle_timeout: 2h
    max_lifetime: 6h
    budget: 50.0
    max_in_flight: 4   # send up to 4 jobs to the backend in parallel
```

Three behaviours determined by `max_in_flight` and the model's generation
mode:

- **t2v fan-out** — text-to-video segments have no temporal dependency, so
  `GenerateClipStage` submits all N segments concurrently (up to
  `max_in_flight` at a time). First failure cancels in-flight jobs and
  re-raises immediately.
- **i2v serial** — image-to-video segments must be chained (each segment's
  tail frame seeds the next), so they are dispatched one-at-a-time
  regardless of `max_in_flight`.
- **multi-request** — a backend running on multi-GPU hardware (e.g. a
  ComfyUI server with 4 GPUs) can process multiple independent requests
  simultaneously; set `max_in_flight` to match its actual parallelism.

`max_in_flight: 1` (the default) preserves the original sequential behaviour.

## HuggingFace ref grammar

Four ref shapes are recognised:

| Ref | Meaning |
|---|---|
| `hf:<repo>` | Bare repo at `main` — every file enumerated via the HF tree API. |
| `hf:<repo>@<rev>` | Bare repo at a pinned branch / tag / commit SHA. |
| `hf:<repo>:<path>` | Single file at `main`. |
| `hf:<repo>@<rev>:<path>` | Single file at a pinned revision. |

Bare-repo resolves auto-populate per-file SHA256 from LFS metadata when
present (every weights file ships LFS-tracked, so integrity verification
runs without the operator setting `sha256:` per entry). Setting
`sha256:` on a bare-repo entry raises `ValidationError` at provision
time — use a pinned `@<commit-sha>` for tree-level reproducibility, or
split into per-file refs for per-file pinning.

## Per-job spec & params

Two top-level YAML blocks supply per-job payload to the engine:

| block | flows into | who reads it | scope |
|---|---|---|---|
| `spec:` | `GenerationJob.spec` | `engine.validate_spec(job)` + `backend.submit(job)` | engine-interpreted (engine-specific shape) |
| `params:` | `GenerationJob.params` | every engine + every `Segment.params` (segment-wins merge) | engine-neutral knobs (fps, num_frames, steps, seed, ...) |

### Required `spec.*` keys per engine

| engine | required `spec.*` keys | notes |
|---|---|---|
| `hosted` | `model`, `params` | `spec.model` is the single source of truth for model identity (Layer M: `engine.hosted.model` removed) |
| `diffusers` | `pipeline`, `scheduler` | |
| `comfyui` | `graph`, `node_overrides` | optional: `asset_node_ids`, `prompt_node_ids` |
| `fal` | — | prompt comes from `Segment.prompt` via Layer J's `resolve_prompt` |
| `replicate` | `model` | |
| `runway` | `model` | |
| `bedrock_video` | — | model identity comes from `engine.bedrock_video.model_id` |
| `fake` | — | accepts any spec; offline testing |

### Top-level `params:` vs nested `spec.params:` (gotcha)

Hosted requires a `params` key **inside** `spec:` as a wire body field. This is
structurally distinct from top-level `params:` (engine-neutral knobs that flow
into `GenerationJob.params`). There is **no merging** between the two
namespaces.

```yaml
params:                 # -> GenerationJob.params (engine-neutral, segment-wins)
  fps: 24
spec:
  model: "wan-..."
  params:               # -> GenerationJob.spec["params"] (hosted wire body)
    guidance_scale: 5.0
```

Reader takeaway: if a key matters to every engine, put it under top-level
`params:`. If it is engine-specific, put it under `spec:`.

### On `validate_spec` failure

When the orchestrator detects a `spec:` key missing for the configured engine,
it raises `ValidationError` and tears down any provisioned compute before
re-raising (mirroring the existing `CapabilityMismatch` branch). A typo in
your config will not cost idle pod time.

See `../examples/configs/hosted.yaml`, `../examples/configs/runpod-diffusers-serverless.yaml`, `../examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml`, and `../examples/configs/fal-t2v.yaml`
for working `spec:` + `params:` shapes per engine.


## `upscale:` (optional, video upscaling)

Activates the in-pipeline `UpscaleStage` after `GenerateClipStage` for
`kinoforge generate`, or stands alone for `kinoforge upscale`. The CLI
flag `--scale` overrides `upscale.scale` for one-off runs.

| Key | Type | Default | Notes |
|---|---|---|---|
| `engine` | `"flashvsr"` \| `"spandrel"` \| `"seedvr2"` | — | Required. `flashvsr` is the v1 default; `seedvr2` is extras-gated (below). |
| `scale` | string | — | `"Nx"` factor or `"Np"` height target (`"1080p"`, `"720p"`). A height target resolves to the engine's native factor followed by a lanczos downscale. A non-4x *factor* is refused for `flashvsr` at load. |
| `chunk_frames` | int \| null | `null` | Split the source temporally, upscale each chunk on the same pod, join on the controller. Clips no longer than this are never split. |
| `chunk_overlap` | int | `8` | Warm-up frames rendered before each chunk's kept range. Must be below `chunk_frames`. |
| `tile_grid` | `[cols, rows]` \| null | `null` | Crop each frame into a grid, upscale the tiles, feather-stitch them back. Combines with `chunk_frames`. |
| `tile_overlap` | int | `32` | Minimum overlap between neighbouring tiles, in source pixels. |

**`flashvsr`** — streaming diffusion VSR (Wan 2.1 1.3B backbone), native 4x, 80 GB tier.

| Key | Type | Default |
|---|---|---|
| `flashvsr.weights_bundle` | `hf:` / `http(s):` ref | — (required) |
| `flashvsr.precision` | `"bfloat16"` \| `"fp16"` \| `"fp32"` | `"bfloat16"` (`"bf16"` is rejected) |
| `flashvsr.window_size` | int, `[8, 64]` | `24` |
| `flashvsr.tile_size` | `0` \| `256` \| `384` \| `512` \| `768` | `0` (whole-frame) |
| `flashvsr.long_video_mode` | bool | `false` — `true` needs the 4-file bundle |
| `flashvsr.bsa_wheel_url` | `hf:` / `http(s):` ref | kinoforge-hosted prebuilt BSA wheel |

**`spandrel`** — per-frame architecture-agnostic SR (RealESRGAN et al.), fits 48 GB.

| Key | Type | Default |
|---|---|---|
| `spandrel.model_url` | string | — (required) |
| `spandrel.arch` | string | `"realesrgan"` |
| `spandrel.precision` | `"fp16"` \| `"fp32"` | `"fp16"` |
| `spandrel.tile_size` | int | `512` |
| `spandrel.batch_size` | int | `4` |

**`seedvr2`** — extras-gated stub. It self-registers and its config parses, so cfg-time validation
can refuse it with a structured error, but `render_provision` / `provision` / `upscale` /
`validate_spec` raise `ExtrasNotInstalled` until the Phase 2 vendoring lands (see
[roadmap.md](roadmap.md)). Keys: `seedvr2.variant` (`"3B"` \| `"7B"`, default `"3B"`),
`seedvr2.precision` (`"fp8"` \| `"fp16"`, default `"fp8"`), `seedvr2.tile_size`, `seedvr2.steps`.

See [`../examples/configs/runpod-diffusers-flashvsr-x4-upscale.yaml`](../examples/configs/runpod-diffusers-flashvsr-x4-upscale.yaml) (standalone),
[`../examples/configs/runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale.yaml`](../examples/configs/runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale.yaml) (generate + upscale on one pod),
and [`../examples/configs/extras/runpod-diffusers-seedvr2-3b-upscale.yaml`](../examples/configs/extras/runpod-diffusers-seedvr2-3b-upscale.yaml) (extras-gated).

## `interpolate:` (optional, frame interpolation)

Activates the in-pipeline `InterpolateStage`, or stands alone for `kinoforge interpolate`. The
CLI flag `--fps` overrides `interpolate.fps`.

| Key | Type | Default | Notes |
|---|---|---|---|
| `engine` | `"rife"` | — | Required. v1 supports RIFE only. |
| `fps` | float > 0 | — | Required. Target output frame rate. |
| `rife.weights_ref` | `hf:` ref | — | Required when `engine == "rife"`. |
| `rife.model` | string | `"rife49"` | Model tag selecting the arch on the pod. Note the shipped configs pin `rife426` (RIFE v4.26) — that is the tag the live runs were proven on, not the field default. |
| `rife.precision` | string | `"fp16"` | |

Interpolation is video-only. A clip carrying a soundtrack (MiniMax-H3's `t2va` mode) needs its
audio re-muxed onto the interpolated result.

See [`../examples/configs/modal-diffusers-rife-60fps-interpolate.yaml`](../examples/configs/modal-diffusers-rife-60fps-interpolate.yaml).

## `image:` (optional, standalone image generation)

Opts a config into the terminal-image path: `kinoforge image` produces one PNG from one
prompt — no pipeline, no video engine, no compute. Presence of `image:` activates this path
instead of the video one; a config with both `image:` and `engine:`/`models:` is refused at load
(see the refused-keys list below). `mode:` must be `t2i` or omitted — any other value is refused.

| Key | Type | Default | Notes |
|---|---|---|---|
| `image.engine` | `"fal"` \| `"replicate"` \| `"luma_agents"` \| `"fake"` | — | Required. Registry key resolved through `get_image_engine`. |
| `image.prompt` | string | `null` | Optional — `kinoforge image` also accepts `--prompt` (wins) or the top-level `prompt:` (fallback). Precedence: `--prompt` > `image.prompt` > `prompt`. |
| `image.spec` | mapping | `{}` | `spec.model` is the provider's model id (e.g. `"uni-1"`, `"fal-ai/flux/schnell"`). |
| `image.params` | mapping | `{}` | Opaque pass-through merged **verbatim** into the provider's request body — kinoforge does not validate its keys. `aspect_ratio` works on Luma; `image_size` works on fal. Neither is checked here; a typo'd key is only caught (or not) by the provider. |

An `image:` config is allowed to carry **only**: `mode`, `prompt`, `image`, `store`, `output`.
Every other top-level key is refused by name at load, because it would be silently inert on a
run that has no compute, no video engine and no model fetch:

- `engine`, `models`, `loras` — there is no video engine to configure and nothing to fetch.
- `compute` — every image engine declares `requires_compute = False`; no pod, no warm reuse.
- `keyframe`, `upscale`, `interpolate`, `splitter` — video-pipeline stages that never run.
- `lifecycle` (top-level) — no compute means no budget/idle-timeout surface to apply it to.
- `spec` / `params` (top-level) — the per-job equivalents live under `image.spec` / `image.params`
  instead; the bare top-level keys are a video-pipeline concept.

`--ephemeral` is refused for every image engine except the in-process `fake` engine: no image
engine implements provider-side record deletion, and two of the three hosted ones cannot today —
Luma's agents API has no DELETE endpoint (records purge only via the dashboard) and fal exposes no
delete path.

**A green `--dry-run` does not prove `image.engine` resolves.** `kinoforge image --dry-run`
deliberately returns before the registry lookup, the store, or the profile are ever constructed —
by design, so a dry run costs nothing and makes no provider or HTTP call. That also means a typo'd
engine name (`engine: lmua_agents`) previews happily and exits 0; the typo only surfaces on a real
run, when `get_image_engine` raises `UnknownAdapter`. Do not read a clean `--dry-run` as
confirmation that the engine key is valid.

See [`../examples/configs/luma-uni1-t2i.yaml`](../examples/configs/luma-uni1-t2i.yaml) and
[`../examples/configs/fal-flux-schnell-t2i.yaml`](../examples/configs/fal-flux-schnell-t2i.yaml).
